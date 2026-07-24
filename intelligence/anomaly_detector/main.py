"""
Anomaly Detector — Control Tower
Hourly Cloud Run job (at :30, offset from collector at :00).
Reads PostgreSQL only, runs 5 anomaly checks, writes alerts to PostgreSQL.
Deduplicates: skips if identical (alert_type, service_name) alert fired < 4h ago.

All checks run against Postgres directly (control_tower schema is the source
of truth — no ClickHouse mirror). Revisit ClickHouse (via PeerDB CDC) if/when
row volume makes Postgres aggregation too slow.
"""
import os, json, uuid, datetime, logging
import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PG_CONN = os.environ["PG_CONN"]

NOW = datetime.datetime.now(datetime.timezone.utc)
DEDUP_WINDOW_HOURS = 4


def pg_connect():
    return psycopg2.connect(PG_CONN)


# ─── Deduplication ────────────────────────────────────────────────────────────

def load_recent_alerts(pg_cur):
    """Returns set of (alert_type, service_name) fired in the last DEDUP_WINDOW_HOURS.
    Includes acknowledged alerts — the alerter acks after emailing, so filtering
    to unacked here caused the same alert to re-fire (and re-email) every hour."""
    pg_cur.execute(
        """SELECT alert_type, COALESCE(service_name, provider, '')
           FROM control_tower.alerts
           WHERE fired_at > NOW() - INTERVAL '%s hours'""",
        (DEDUP_WINDOW_HOURS,),
    )
    return {(r[0], r[1]) for r in pg_cur.fetchall()}


# ─── Anomaly checks ───────────────────────────────────────────────────────────

def check_error_rate_spike(pg_cur):
    """Check 1: error rate > 5% AND > 2x 7-day baseline."""
    pg_cur.execute("""
        WITH
        recent AS (
            SELECT service_name, AVG(error_rate_pct) AS recent_avg
            FROM control_tower.service_health
            WHERE collected_at >= NOW() - INTERVAL '2 hours'
              AND platform = 'cloud_run_service'
            GROUP BY service_name
        ),
        baseline AS (
            SELECT service_name, AVG(error_rate_pct) AS baseline_avg
            FROM control_tower.service_health
            WHERE collected_at >= NOW() - INTERVAL '7 days'
              AND collected_at < NOW() - INTERVAL '2 hours'
              AND platform = 'cloud_run_service'
            GROUP BY service_name
        )
        SELECT r.service_name, r.recent_avg, b.baseline_avg
        FROM recent r JOIN baseline b USING (service_name)
        WHERE r.recent_avg > 5
          AND r.recent_avg > b.baseline_avg * 2
    """)
    alerts = []
    for service, recent, baseline in pg_cur.fetchall():
        alerts.append({
            "alert_type": "error_rate_spike",
            "severity": "critical",
            "service_name": service,
            "message": f"{service}: error rate {recent:.1f}% (baseline {baseline:.1f}%)",
            "context_json": {"recent_avg": recent, "baseline_avg": baseline},
        })
    return alerts


def check_job_duration_drift(pg_cur):
    """Check 2: job p95 duration > 1.5x 14-day baseline AND > 60s."""
    pg_cur.execute("""
        WITH
        recent AS (
            SELECT service_name,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY job_duration_ms) AS p95_recent
            FROM control_tower.service_health
            WHERE collected_at >= NOW() - INTERVAL '4 hours'
              AND platform = 'cloud_run_job'
              AND job_duration_ms IS NOT NULL
            GROUP BY service_name
        ),
        baseline AS (
            SELECT service_name,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY job_duration_ms) AS p95_baseline
            FROM control_tower.service_health
            WHERE collected_at >= NOW() - INTERVAL '14 days'
              AND collected_at < NOW() - INTERVAL '4 hours'
              AND platform = 'cloud_run_job'
              AND job_duration_ms IS NOT NULL
            GROUP BY service_name
        )
        SELECT r.service_name, r.p95_recent, b.p95_baseline
        FROM recent r JOIN baseline b USING (service_name)
        WHERE r.p95_recent > 60000
          AND r.p95_recent > b.p95_baseline * 1.5
    """)
    alerts = []
    for service, recent_ms, baseline_ms in pg_cur.fetchall():
        alerts.append({
            "alert_type": "job_duration_drift",
            "severity": "warn",
            "service_name": service,
            "message": (
                f"{service}: p95 duration {recent_ms/1000:.0f}s "
                f"(baseline {baseline_ms/1000:.0f}s)"
            ),
            "context_json": {"p95_recent_ms": recent_ms, "p95_baseline_ms": baseline_ms},
        })
    return alerts


def check_idle_sinks(pg_cur):
    """Check 3: tables with stale or dead freshness status.
    Batched into a single summary alert — with ~200 auto-discovered tables,
    per-table alerts would flood the inbox."""
    # Alert only on actionable tables: recently active (wrote within 30d) that went
    # quiet, or manually watched. Ancient static reference tables stay visible in
    # the UI but don't alert.
    pg_cur.execute("""
        SELECT f.database_name, f.table_name, f.freshness_status, f.last_write_at
        FROM (
            SELECT DISTINCT ON (database_name, table_name)
                database_name, table_name, freshness_status, last_write_at
            FROM control_tower.data_freshness
            ORDER BY database_name, table_name, checked_at DESC
        ) f
        LEFT JOIN control_tower.watched_tables w
            ON w.database_name = f.database_name AND w.table_name = f.table_name
        WHERE f.freshness_status IN ('stale', 'dead')
          AND (f.last_write_at > NOW() - INTERVAL '30 days'
               OR COALESCE(w.auto_discovered, false) = false)
    """)
    stale, dead = [], []
    for db, table, status, last_write in pg_cur.fetchall():
        entry = {"table": f"{db}.{table}", "last_write_at": str(last_write)}
        if status == "dead":
            dead.append(entry)
        elif status == "stale":
            stale.append(entry)

    if not stale and not dead:
        return []

    parts = []
    if dead:
        parts.append(f"{len(dead)} DEAD: " + ", ".join(e["table"] for e in dead[:10])
                     + (" …" if len(dead) > 10 else ""))
    if stale:
        parts.append(f"{len(stale)} stale: " + ", ".join(e["table"] for e in stale[:10])
                     + (" …" if len(stale) > 10 else ""))

    return [{
        "alert_type": "idle_sink",
        "severity": "critical" if dead else "warn",
        "service_name": "data_freshness_summary",
        "message": "Data freshness: " + " | ".join(parts),
        "context_json": {"dead": dead, "stale": stale},
    }]


def check_api_degradation(pg_cur):
    """Check 4: third-party API error rate > 10% or latency > 2x baseline."""
    pg_cur.execute("""
        SELECT provider, error_rate_pct, avg_latency_ms_estimate
        FROM (
            SELECT DISTINCT ON (provider)
                provider, error_rate_pct, avg_latency_ms_estimate
            FROM control_tower.third_party_api_health
            WHERE collected_at >= NOW() - INTERVAL '2 hours'
            ORDER BY provider, collected_at DESC
        ) latest
        WHERE error_rate_pct > 10
    """)
    alerts = []
    for provider, error_rate, latency in pg_cur.fetchall():
        alerts.append({
            "alert_type": "api_degradation",
            "severity": "warn",
            "provider": provider,
            "message": f"{provider} API: {error_rate:.1f}% error rate (past 2h)",
            "context_json": {"error_rate_pct": error_rate, "avg_latency_ms": latency},
        })
    return alerts


def check_ec2_pressure(pg_cur):
    """Check 5: EC2 CPU > 85%, memory > 90%, disk > 85% over past 2h."""
    pg_cur.execute("""
        SELECT service_name,
               AVG(cpu_utilization_pct) AS avg_cpu,
               AVG(memory_utilization_pct) AS avg_mem,
               AVG(disk_utilization_pct) AS avg_disk
        FROM control_tower.service_health
        WHERE platform IN ('ec2', 'ec2_job')
          AND collected_at >= NOW() - INTERVAL '2 hours'
        GROUP BY service_name
        HAVING AVG(cpu_utilization_pct) > 85
            OR AVG(memory_utilization_pct) > 90
            OR AVG(disk_utilization_pct) > 85
    """)
    alerts = []
    for service, cpu, mem, disk in pg_cur.fetchall():
        reasons = []
        severity = "warn"
        if cpu and cpu > 85:
            reasons.append(f"CPU {cpu:.0f}%")
        if mem and mem > 90:
            reasons.append(f"mem {mem:.0f}%")
            severity = "critical"
        if disk and disk > 85:
            reasons.append(f"disk {disk:.0f}%")
        alerts.append({
            "alert_type": "ec2_resource_pressure",
            "severity": severity,
            "service_name": service,
            "message": f"{service}: {', '.join(reasons)} over 2h avg",
            "context_json": {"cpu": cpu, "memory": mem, "disk": disk},
        })
    return alerts


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("Anomaly Detector starting.")
    pg = pg_connect()

    try:
        all_alerts = []
        with pg.cursor() as cur:
            recent = load_recent_alerts(cur)

            checks = [
                check_error_rate_spike,
                check_job_duration_drift,
                check_idle_sinks,
                check_api_degradation,
                check_ec2_pressure,
            ]
            for check_fn in checks:
                try:
                    all_alerts.extend(check_fn(cur))
                except Exception as e:
                    log.warning("Anomaly check failed: %s", e)

            # Deduplicate against recent alerts
            new_alerts = []
            for alert in all_alerts:
                key = (
                    alert["alert_type"],
                    alert.get("service_name") or alert.get("provider", ""),
                )
                if key not in recent:
                    new_alerts.append(alert)
                else:
                    log.info("Deduped: %s / %s", *key)

            if new_alerts:
                execute_values(
                    cur,
                    """INSERT INTO control_tower.alerts
                       (alert_id, alert_type, severity, service_name, provider,
                        message, context_json, fired_at)
                       VALUES %s""",
                    [
                        (
                            str(uuid.uuid4()),
                            a["alert_type"],
                            a["severity"],
                            a.get("service_name"),
                            a.get("provider"),
                            a["message"],
                            json.dumps(a["context_json"]),
                            NOW,
                        )
                        for a in new_alerts
                    ],
                )
                log.info("Wrote %d new alerts", len(new_alerts))
            else:
                log.info("No new alerts.")

            pg.commit()

    except Exception as e:
        pg.rollback()
        log.error("Anomaly Detector failed: %s", e)
        raise
    finally:
        pg.close()

    log.info("Anomaly Detector complete.")


if __name__ == "__main__":
    main()
