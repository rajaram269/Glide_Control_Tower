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
from fastapi.staticfiles import StaticFiles

PG_CONN = os.environ["PG_CONN"]

app = FastAPI(title="Control Tower", docs_url=None, redoc_url=None)

# Preact/htm vendored locally — no external CDN dependency at runtime
# (esm.sh downtime/rate-limiting/ad-blockers previously caused blank pages).
app.mount("/vendor", StaticFiles(directory=Path(__file__).parent / "vendor"), name="vendor")

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
                (SELECT COUNT(DISTINCT service_name) FROM control_tower.registered_services
                 WHERE active = true AND platform = 'cloud_run_service') AS active_services,
                (SELECT COUNT(DISTINCT service_name) FROM control_tower.registered_services
                 WHERE active = true AND platform = 'cloud_run_job') AS active_jobs,
                (SELECT COUNT(*) FROM (
                    SELECT DISTINCT ON (service_name) service_name, job_status
                    FROM control_tower.service_health
                    WHERE platform = 'cloud_run_job'
                    ORDER BY service_name, collected_at DESC
                 ) j WHERE job_status = 'failed') AS jobs_failing,
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
    """Cloud Run services only — request-driven, has traffic/latency metrics."""
    with get_db() as cur:
        cur.execute("""
            SELECT DISTINCT ON (service_name)
                service_name, platform, region,
                request_count, error_count, error_rate_pct,
                instance_count, p50_latency_ms, p95_latency_ms,
                collected_at
            FROM control_tower.service_health
            WHERE platform = 'cloud_run_service'
            ORDER BY service_name, collected_at DESC
        """)
        return cur.fetchall()


@app.get("/api/jobs")
def jobs():
    """Cloud Run jobs — batch/cron work, no traffic metrics.
    job_last_execution_at is when the job itself last finished running;
    collected_at is only when Control Tower last scraped it (can be much more
    recent than the job's actual last run for infrequent schedules)."""
    with get_db() as cur:
        cur.execute("""
            SELECT DISTINCT ON (service_name)
                service_name, region, job_status, job_duration_ms,
                job_last_execution_at, collected_at
            FROM control_tower.service_health
            WHERE platform = 'cloud_run_job'
            ORDER BY service_name, collected_at DESC
        """)
        return cur.fetchall()


@app.get("/api/scheduler-jobs")
def scheduler_jobs():
    """Cloud Scheduler jobs whose target is a plain HTTP endpoint on a Cloud
    Run service (cron logic living inside the service, not a real Cloud Run
    Job) — the scheduler's own last-attempt status is the only signal for these."""
    with get_db() as cur:
        cur.execute("""
            SELECT scheduler_name, schedule, target_uri, region,
                   last_attempt_at, last_attempt_status, checked_at
            FROM control_tower.scheduler_jobs
            WHERE active = true
            ORDER BY scheduler_name
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


# ─── Sentinel API (catalog overlay + monitor data) ─────────────────────────────

@app.get("/api/sentinel/summary")
def sentinel_summary():
    with get_db() as cur:
        cur.execute("""
            SELECT
                (SELECT COUNT(*) FROM sentinel.catalog_overlay WHERE retired = false) AS tables_cataloged,
                (SELECT COUNT(*) FROM sentinel.catalog_overlay
                 WHERE retired = false AND (conflict_type <> 'none' OR review_status = 'needs_review')) AS needs_review,
                (SELECT COUNT(*) FROM sentinel.incidents WHERE resolved_at IS NULL) AS open_incidents,
                (SELECT COUNT(*) FROM sentinel.reconciliation_rules WHERE status = 'active') AS active_rules,
                (SELECT COUNT(*) FROM sentinel.monitor_targets WHERE status = 'active') AS active_targets
        """)
        return dict(cur.fetchone())


@app.get("/api/sentinel/catalog")
def sentinel_catalog():
    with get_db() as cur:
        cur.execute("""
            SELECT database_name, table_name, concept, scope, source_type, grain,
                   summary, description, authoritative, authority_confidence, use_instead,
                   requires_dedup, dedup_method, dedup_key, quirks, conflict_type,
                   review_status, is_static, updated_by, updated_at
            FROM sentinel.catalog_overlay
            WHERE retired = false
            ORDER BY concept NULLS LAST, source_type, database_name, table_name
        """)
        return cur.fetchall()


@app.get("/api/sentinel/review-queue")
def sentinel_review_queue():
    """The §7.2.5 safety gate made visible: conflicts + needs_review rows."""
    with get_db() as cur:
        cur.execute("""
            SELECT database_name, table_name, concept, scope, source_type,
                   conflict_type, review_status, review_reason, authoritative,
                   use_instead, authority_confidence, updated_by, is_static
            FROM sentinel.catalog_overlay
            WHERE retired = false
              AND (conflict_type <> 'none' OR review_status = 'needs_review')
            ORDER BY conflict_type DESC, authority_confidence NULLS FIRST
        """)
        return cur.fetchall()


@app.post("/api/sentinel/overlay/{database_name}/{table_name}/resolve")
def sentinel_resolve_review(database_name: str, table_name: str):
    """Human confirms a needs_review table — mark confirmed + human-pinned so the
    discovery loop won't re-flag or overwrite it."""
    with get_db() as cur:
        cur.execute("""
            UPDATE sentinel.catalog_overlay
            SET review_status='confirmed', review_reason=NULL,
                conflict_type='none', updated_by='human', updated_at=now()
            WHERE database_name=%s AND table_name=%s
        """, (database_name, table_name))
        return {"ok": True}


@app.post("/api/sentinel/overlay/{database_name}/{table_name}/static")
def sentinel_set_static(database_name: str, table_name: str, value: bool = True):
    """Human marks a table static (or not). Human-pinned so discovery won't override."""
    with get_db() as cur:
        cur.execute("""
            UPDATE sentinel.catalog_overlay
            SET is_static=%s, updated_by='human', updated_at=now()
            WHERE database_name=%s AND table_name=%s
        """, (value, database_name, table_name))
        cur.execute("""
            UPDATE sentinel.monitor_targets SET is_static=%s
            WHERE database_name=%s AND table_name=%s
        """, (value, database_name, table_name))
        return {"ok": True, "is_static": value}


@app.get("/api/sentinel/coverage")
def sentinel_coverage():
    with get_db() as cur:
        cur.execute("""
            SELECT v.database_name, v.table_name, v.variable,
                   COUNT(*) FILTER (WHERE v.lifecycle = 'active')  AS active_values,
                   COUNT(*) FILTER (WHERE v.lifecycle = 'retired') AS retired_values,
                   MAX(v.last_seen) AS last_seen,
                   lc.status       AS last_check_status,
                   lc.missing      AS missing_values
            FROM sentinel.variable_values v
            LEFT JOIN LATERAL (
                SELECT status, observed->'missing_values' AS missing
                FROM sentinel.check_results c
                WHERE c.check_type = 'variable_coverage'
                  AND c.database_name = v.database_name
                  AND c.table_name = v.table_name
                  AND c.variable = v.variable
                ORDER BY run_ts DESC LIMIT 1
            ) lc ON true
            GROUP BY v.database_name, v.table_name, v.variable, lc.status, lc.missing
            ORDER BY v.database_name, v.table_name, v.variable
        """)
        return cur.fetchall()


@app.get("/api/sentinel/reconciliation")
def sentinel_reconciliation():
    with get_db() as cur:
        cur.execute("""
            SELECT r.rule_id, r.concept, r.metric, r.source_a, r.source_b,
                   r.dimension, r.tolerance_pct, r.direction, r.status, r.stable_runs,
                   lc.status AS last_result, lc.run_ts AS last_run,
                   lc.a AS last_a, lc.b AS last_b, lc.diff_pct AS last_diff_pct
            FROM sentinel.reconciliation_rules r
            LEFT JOIN LATERAL (
                SELECT status, run_ts,
                       observed->>'a' AS a, observed->>'b' AS b,
                       observed->>'diff_pct' AS diff_pct
                FROM sentinel.check_results c
                WHERE c.rule_id = r.rule_id ORDER BY run_ts DESC LIMIT 1
            ) lc ON true
            ORDER BY r.status, r.concept
        """)
        return cur.fetchall()


@app.get("/api/sentinel/incidents")
def sentinel_incidents():
    with get_db() as cur:
        cur.execute("""
            SELECT id, opened_at, resolved_at, scope, scope_level, check_type,
                   severity, parent_incident_id, rolled_up_children, message, alert_id
            FROM sentinel.incidents
            ORDER BY (resolved_at IS NULL) DESC, opened_at DESC
            LIMIT 200
        """)
        return cur.fetchall()


@app.get("/api/sentinel/freshness")
def sentinel_freshness():
    """Dual-source view for the cutover (§7): Sentinel latest freshness check
    beside the legacy control_tower.data_freshness verdict, per table."""
    with get_db() as cur:
        cur.execute("""
            WITH sen AS (
                SELECT DISTINCT ON (database_name, table_name)
                    database_name, table_name,
                    observed->>'sub_status' AS sentinel_status,
                    status AS sentinel_check_status,
                    observed->>'last_write' AS sentinel_last_write,
                    observed->>'mechanism' AS mechanism,
                    (observed->>'tolerance_weeks')::numeric AS expected_cadence_weeks,
                    run_ts AS sentinel_checked_at
                FROM sentinel.check_results
                WHERE check_type = 'freshness' AND COALESCE(variable,'') = ''
                ORDER BY database_name, table_name, run_ts DESC
            ),
            vars AS (   -- tracked business-segment variables per table (clear reporting)
                SELECT database_name, table_name,
                       array_agg(variable ORDER BY variable) AS tracked_variables
                FROM sentinel.monitor_targets
                WHERE variable <> '' AND status = 'active'
                GROUP BY database_name, table_name
            ),
            stat AS (   -- static (intentionally-frozen) tables
                SELECT DISTINCT database_name, table_name, is_static
                FROM sentinel.monitor_targets WHERE variable = ''
            ),
            leg AS (
                SELECT DISTINCT ON (database_name, table_name)
                    database_name, table_name,
                    freshness_status AS legacy_status,
                    last_write_at AS legacy_last_write,
                    checked_at AS legacy_checked_at
                FROM control_tower.data_freshness
                ORDER BY database_name, table_name, checked_at DESC
            )
            SELECT COALESCE(sen.database_name, leg.database_name) AS database_name,
                   COALESCE(sen.table_name, leg.table_name)       AS table_name,
                   sen.sentinel_status, sen.mechanism, sen.sentinel_last_write,
                   sen.expected_cadence_weeks, sen.sentinel_checked_at,
                   leg.legacy_status, leg.legacy_last_write, leg.legacy_checked_at,
                   vars.tracked_variables, COALESCE(stat.is_static, false) AS is_static,
                   (sen.sentinel_status IS NOT NULL AND leg.legacy_status IS NOT NULL
                    AND sen.sentinel_status <> leg.legacy_status) AS mismatch
            FROM sen
            FULL OUTER JOIN leg  USING (database_name, table_name)
            LEFT JOIN      vars USING (database_name, table_name)
            LEFT JOIN      stat USING (database_name, table_name)
            ORDER BY mismatch DESC NULLS LAST, database_name, table_name
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
    # Always revalidate — stale cached HTML after a redeploy is confusing
    # ("blank page on refresh" reports have traced back to this before).
    return HTMLResponse(html_path.read_text(), headers={"Cache-Control": "no-cache"})
