"""
Control Tower UI — FastAPI backend
Serves React dashboard at GET / and REST API at /api/*.
Connects directly to PostgreSQL (same Cloud SQL instance as collectors).
"""
import os, json
from pathlib import Path
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse

PG_CONN = os.environ["PG_CONN"]

app = FastAPI(title="Control Tower", docs_url=None, redoc_url=None)

# ─── DB helper ────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = psycopg2.connect(PG_CONN)
    conn.autocommit = True
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    finally:
        conn.close()


# ─── API routes ───────────────────────────────────────────────────────────────

@app.get("/api/summary")
def summary():
    with get_db() as cur:
        cur.execute("""
            SELECT
                (SELECT COUNT(DISTINCT service_name) FROM control_tower.registered_services WHERE active = true) AS active_services,
                (SELECT COUNT(*) FROM control_tower.alerts WHERE acknowledged_at IS NULL) AS open_alerts,
                (SELECT COUNT(*) FROM control_tower.alerts WHERE acknowledged_at IS NULL AND severity = 'critical') AS critical_alerts,
                (SELECT COALESCE(SUM(cost_usd), 0) FROM control_tower.cost_metrics
                 WHERE period_start >= CURRENT_DATE - INTERVAL '7 days') AS weekly_spend_usd,
                (SELECT MAX(collected_at) FROM control_tower.service_health) AS last_collection
        """)
        row = cur.fetchone()
        return dict(row)


@app.get("/api/services")
def services():
    with get_db() as cur:
        cur.execute("""
            SELECT DISTINCT ON (service_name)
                service_name, platform, region,
                request_count, error_count, error_rate_pct,
                instance_count, job_status, job_duration_ms,
                p50_latency_ms, p95_latency_ms,
                collected_at
            FROM control_tower.service_health
            ORDER BY service_name, collected_at DESC
        """)
        return cur.fetchall()


@app.get("/api/providers")
def providers():
    with get_db() as cur:
        cur.execute("""
            SELECT DISTINCT ON (provider)
                provider, overall_status, affected_components, polled_at
            FROM control_tower.provider_status
            ORDER BY provider, polled_at DESC
        """)
        rows = cur.fetchall()
        for r in rows:
            if isinstance(r.get("affected_components"), str):
                try:
                    r["affected_components"] = json.loads(r["affected_components"])
                except Exception:
                    pass
        return rows


@app.get("/api/alerts")
def alerts(status: str = "unacked", limit: int = 50):
    with get_db() as cur:
        where = "WHERE acknowledged_at IS NULL" if status == "unacked" else ""
        cur.execute(f"""
            SELECT alert_id, alert_type, severity, service_name, provider,
                   message, context_json, fired_at, acknowledged_at, ack_by
            FROM control_tower.alerts
            {where}
            ORDER BY
                CASE severity WHEN 'critical' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END,
                fired_at DESC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: str):
    with get_db() as cur:
        cur.execute(
            """UPDATE control_tower.alerts
               SET acknowledged_at = NOW(), ack_by = 'dashboard'
               WHERE alert_id = %s AND acknowledged_at IS NULL
               RETURNING alert_id""",
            (alert_id,),
        )
        if not cur.fetchone():
            raise HTTPException(404, "Alert not found or already acknowledged")
    return {"ok": True}


@app.get("/api/pipeline")
def pipeline(limit: int = 100):
    with get_db() as cur:
        cur.execute("""
            SELECT DISTINCT ON (pipeline_id)
                pipeline_id, step_name, execution_id, event_type, status,
                rows_written, rows_failed, duration_ms, error_message,
                metadata_json, occurred_at
            FROM control_tower.pipeline_events
            ORDER BY pipeline_id, occurred_at DESC
        """)
        latest = cur.fetchall()

        # Last 24h history per job for trend
        cur.execute("""
            SELECT pipeline_id,
                   COUNT(*) AS runs_24h,
                   SUM(rows_written) AS total_processed_24h,
                   SUM(rows_failed) AS total_failed_24h,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_runs_24h
            FROM control_tower.pipeline_events
            WHERE occurred_at > NOW() - INTERVAL '24 hours'
              AND event_type = 'run_complete'
            GROUP BY pipeline_id
        """)
        history = {r["pipeline_id"]: dict(r) for r in cur.fetchall()}

        result = []
        for row in latest:
            d = dict(row)
            d["history_24h"] = history.get(d["pipeline_id"], {})
            result.append(d)
        return result


@app.get("/api/costs")
def costs():
    with get_db() as cur:
        cur.execute("""
            WITH this_week AS (
                SELECT cost_source, resource_name, SUM(cost_usd) AS total,
                       MAX(monthly_budget_usd) AS monthly_budget
                FROM control_tower.cost_metrics
                WHERE period_start >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY cost_source, resource_name
            ),
            last_week AS (
                SELECT cost_source, resource_name, SUM(cost_usd) AS total
                FROM control_tower.cost_metrics
                WHERE period_start >= CURRENT_DATE - INTERVAL '14 days'
                  AND period_start < CURRENT_DATE - INTERVAL '7 days'
                GROUP BY cost_source, resource_name
            )
            SELECT
                t.cost_source, t.resource_name,
                t.total AS this_week_usd,
                COALESCE(l.total, 0) AS last_week_usd,
                t.monthly_budget AS monthly_budget_usd
            FROM this_week t
            LEFT JOIN last_week l USING (cost_source, resource_name)
            ORDER BY t.total DESC
            LIMIT 50
        """)
        return cur.fetchall()


@app.get("/api/freshness")
def freshness():
    with get_db() as cur:
        cur.execute("""
            SELECT DISTINCT ON (database_name, table_name)
                database_name, table_name, freshness_status,
                last_write_at, row_count_snapshot, row_count_delta, checked_at
            FROM control_tower.data_freshness
            ORDER BY database_name, table_name, checked_at DESC
        """)
        return cur.fetchall()


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/healthz")
def health():
    return {"ok": True}


# ─── Serve React dashboard ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text())
