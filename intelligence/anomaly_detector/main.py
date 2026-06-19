"""
Anomaly Detector — Control Tower
Hourly Cloud Run job (at :30, offset from collector at :00).
Reads ClickHouse replica, runs 5 anomaly checks, writes alerts to PostgreSQL.
Deduplicates: skips if identical (alert_type, service_name) alert fired < 4h ago.
"""
import os, json, uuid, datetime, logging
import psycopg2
from psycopg2.extras import execute_values
import clickhouse_connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PG_CONN = os.environ["PG_CONN"]
CH_HOST = os.environ["CH_HOST"]
CH_USER = os.environ["CH_USER"]
CH_PASS = os.environ["CH_PASS"]

NOW = datetime.datetime.now(datetime.timezone.utc)
DEDUP_WINDOW_HOURS = 4


def pg_connect():
    return psycopg2.connect(PG_CONN)


def ch_connect():
    return clickhouse_connect.get_client(
        host=CH_HOST, user=CH_USER, password=CH_PASS, port=8443, secure=True
    )


# ─── Deduplication ────────────────────────────────────────────────────────────

def load_recent_alerts(pg_cur):
    """Returns set of (alert_type, service_name) fired in the last DEDUP_WINDOW_HOURS."""
    pg_cur.execute(
        """SELECT alert_type, COALESCE(service_name, provider, '')
           FROM control_tower.alerts
           WHERE acknowledged_at IS NULL
             AND fired_at > NOW() - INTERVAL '%s hours'""",
        (DEDUP_WINDOW_HOURS,),
    )
    return {(r[0], r[1]) for r in pg_cur.fetchall()}


# ─── Anomaly checks ───────────────────────────────────────────────────────────

def check_error_rate_spike(ch):
    """Check 1: error rate > 5% AND > 2x 7-day baseline."""
    result = ch.query("""
        WITH
        recent AS (
            SELECT service_name,
                   avgIf(error_rate_pct, collected_at >= now() - INTERVAL 2 HOUR) AS recent_avg
            FROM control_tower.service_health
            WHERE collected_at >= now() - INTERVAL 2 HOUR
              AND platform IN ('cloud_run_service')
            GROUP BY service_name
            HAVING argMax(_peerdb_is_deleted, _peerdb_version) = 0
        ),
        baseline AS (
            SELECT service_name,
                   avg(error_rate_pct) AS baseline_avg
            FROM control_tower.service_health
            WHERE collected_at >= now() - INTERVAL 7 DAY
              AND collected_at < now() - INTERVAL 2 HOUR
              AND platform IN ('cloud_run_service')
            GROUP BY service_name
            HAVING argMax(_peerdb_is_deleted, _peerdb_version) = 0
        )
        SELECT r.service_name, r.recent_avg, b.baseline_avg
        FROM recent r JOIN baseline b USING (service_name)
        WHERE r.recent_avg > 5
          AND r.recent_avg > b.baseline_avg * 2
    """)
    alerts = []
    for row in result.result_rows:
        service, recent, baseline = row
        alerts.append({
            "alert_type": "error_rate_spike",
            "severity": "critical",
            "service_name": service,
            "message": f"{service}: error rate {recent:.1f}% (baseline {baseline:.1f}%)",
            "context_json": {"recent_avg": recent, "baseline_avg": baseline},
        })
    return alerts


def check_job_duration_drift(ch):
    """Check 2: job p95 duration > 1.5x 14-day baseline AND > 60s."""
    result = ch.query("""
        WITH
        recent AS (
            SELECT service_name,
                   quantile(0.95)(job_duration_ms) AS p95_recent
            FROM control_tower.service_health
            WHERE collected_at >= now() - INTERVAL 4 HOUR
              AND platform = 'cloud_run_job'
              AND job_duration_ms IS NOT NULL
            GROUP BY service_name
            HAVING argMax(_peerdb_is_deleted, _peerdb_version) = 0
        ),
        baseline AS (
            SELECT service_name,
                   quantile(0.95)(job_duration_ms) AS p95_baseline
            FROM control_tower.service_health
            WHERE collected_at >= now() - INTERVAL 14 DAY
              AND collected_at < now() - INTERVAL 4 HOUR
              AND platform = 'cloud_run_job'
              AND job_duration_ms IS NOT NULL
            GROUP BY service_name
            HAVING argMax(_peerdb_is_deleted, _peerdb_version) = 0
        )
        SELECT r.service_name, r.p95_recent, b.p95_baseline
        FROM recent r JOIN baseline b USING (service_name)
        WHERE r.p95_recent > 60000
          AND r.p95_recent > b.p95_baseline * 1.5
    """)
    alerts = []
    for row in result.result_rows:
        service, recent_ms, baseline_ms = row
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
    """Check 3: tables with stale or dead freshness status."""
    pg_cur.execute("""
        SELECT DISTINCT ON (database_name, table_name)
            database_name, table_name, freshness_status, last_write_at
        FROM control_tower.data_freshness
        ORDER BY database_name, table_name, checked_at DESC
    """)
    alerts = []
    for db, table, status, last_write in pg_cur.fetchall():
        if status in ("stale", "dead"):
            severity = "critical" if status == "dead" else "warn"
            alerts.append({
                "alert_type": "idle_sink",
                "severity": severity,
                "service_name": f"{db}.{table}",
                "message": f"Table {db}.{table} is {status}. Last write: {last_write}",
                "context_json": {"status": status, "last_write_at": str(last_write)},
            })
    return alerts


def check_api_degradation(ch):
    """Check 4: third-party API error rate > 10% or latency > 2x baseline."""
    result = ch.query("""
        WITH
        latest AS (
            SELECT provider,
                   argMax(error_rate_pct, collected_at) AS latest_error_rate,
                   argMax(avg_latency_ms_estimate, collected_at) AS latest_latency
            FROM control_tower.third_party_api_health
            WHERE collected_at >= now() - INTERVAL 2 HOUR
            GROUP BY provider
            HAVING argMax(_peerdb_is_deleted, _peerdb_version) = 0
        )
        SELECT provider, latest_error_rate, latest_latency
        FROM latest
        WHERE latest_error_rate > 10
    """)
    alerts = []
    for row in result.result_rows:
        provider, error_rate, latency = row
        alerts.append({
            "alert_type": "api_degradation",
            "severity": "warn",
            "provider": provider,
            "message": f"{provider} API: {error_rate:.1f}% error rate (past 2h)",
            "context_json": {"error_rate_pct": error_rate, "avg_latency_ms": latency},
        })
    return alerts


def check_ec2_pressure(ch):
    """Check 5: EC2 CPU > 85%, memory > 90%, disk > 85% over past 2h."""
    result = ch.query("""
        SELECT service_name,
               avg(cpu_utilization_pct) AS avg_cpu,
               avg(memory_utilization_pct) AS avg_mem,
               avg(disk_utilization_pct) AS avg_disk
        FROM control_tower.service_health
        WHERE platform IN ('ec2', 'ec2_job')
          AND collected_at >= now() - INTERVAL 2 HOUR
        GROUP BY service_name
        HAVING argMax(_peerdb_is_deleted, _peerdb_version) = 0
          AND (avg_cpu > 85 OR avg_mem > 90 OR avg_disk > 85)
    """)
    alerts = []
    for row in result.result_rows:
        service, cpu, mem, disk = row
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

    # ClickHouse connection — optional until Phase 3 (PeerDB) is configured.
    # If control_tower tables don't exist yet, checks that use CH will return empty results.
    try:
        ch = ch_connect()
        # Verify control_tower tables exist in ClickHouse
        ch.query("SELECT 1 FROM control_tower.service_health LIMIT 0")
        ch_ready = True
        log.info("ClickHouse control_tower replica: ready.")
    except Exception as e:
        log.warning(
            "ClickHouse control_tower tables not yet available (Phase 3 PeerDB not configured?): %s. "
            "Skipping metric-based anomaly checks — only PostgreSQL checks will run.",
            e,
        )
        ch_ready = False
        ch = None

    try:
        all_alerts = []
        with pg.cursor() as cur:
            recent = load_recent_alerts(cur)

            # Run checks — CH-dependent checks skipped until Phase 3 (PeerDB) is live
            checks = [
                (lambda: check_error_rate_spike(ch),    True),
                (lambda: check_job_duration_drift(ch),  True),
                (lambda: check_idle_sinks(cur),         False),
                (lambda: check_api_degradation(ch),     True),
                (lambda: check_ec2_pressure(ch),        True),
            ]
            for check_fn, needs_ch in checks:
                if needs_ch and not ch_ready:
                    continue
                try:
                    all_alerts.extend(check_fn())
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
        if ch:
            ch.close()

    log.info("Anomaly Detector complete.")


if __name__ == "__main__":
    main()
