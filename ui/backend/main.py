"""
Control Tower UI — FastAPI backend
Serves React dashboard at GET / and REST API at /api/*.
Connects directly to PostgreSQL (same Cloud SQL instance as collectors).
"""
import os, json, re
from datetime import datetime, timedelta, timezone
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
                 ) j WHERE job_status IN ('failed', 'missed', 'cancelled', 'stuck')) AS jobs_failing,
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
              AND service_name IN (
                  SELECT service_name FROM control_tower.registered_services WHERE active = true)
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
              AND service_name IN (
                  SELECT service_name FROM control_tower.registered_services WHERE active = true)
            ORDER BY service_name, collected_at DESC
        """)
        return cur.fetchall()


@app.get("/api/scheduler-jobs")
def scheduler_jobs():
    """Every Cloud Scheduler job in the project (all regions, all target types),
    auto-discovered by the GCP collector. target_type/target_name say what each
    one triggers (a Cloud Run Job, an HTTP endpoint, Pub/Sub, App Engine)."""
    with get_db() as cur:
        cur.execute("""
            SELECT scheduler_name, schedule, target_uri, region,
                   target_type, target_name, state,
                   last_attempt_at, last_attempt_status, checked_at
            FROM control_tower.scheduler_jobs
            WHERE active = true
            ORDER BY scheduler_name
        """)
        return cur.fetchall()


# ─── Logs (read live from Cloud Logging) ──────────────────────────────────────
#
# Lets developers without GCP console access read a job's / service's logs
# from the dashboard. Uses the UI's own service account (roles/logging.viewer).
# Jobs: entries of a chosen execution, defaulting to the latest (last 2 days).
# Services: last 24 hours.

LOG_PROJECT = os.environ.get("GCP_PROJECT", "seoai-479305")
_LOG_TARGETS = {
    "job":     ("cloud_run_job",      "job_name",     timedelta(days=2)),
    "service": ("cloud_run_revision", "service_name", timedelta(hours=24)),
}
# Cloud Run naming rules — also keeps the name from injecting into the filter.
_RUN_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EXEC_LABEL = "run.googleapis.com/execution_name"


def _log_line(entry):
    p = entry.payload
    raw = None
    if isinstance(p, dict):
        status = p.get("status") if isinstance(p.get("status"), dict) else None
        # Plain app logs have "message"/"msg". Cloud Run execution audit-log entries
        # (job started/failed/etc.) instead carry it at status.message — e.g.
        # "Execution my-job-abcd has failed to complete, 0/1 tasks were a success."
        # Without this, these render as one unreadable multi-hundred-field JSON dump
        # (the raw audit record) instead of the one line Cloud Console shows.
        msg = p.get("message") or p.get("msg") or (status and status.get("message"))
        if msg:
            raw = json.dumps(p, default=str)[:4000]  # kept so the full record can be inspected
        else:
            msg = json.dumps(p, default=str)
    else:
        msg = "" if p is None else str(p)
    req = entry.http_request or {}
    if req:   # Cloud Run request log: show which request, its status and latency
        msg = (f'{req.get("requestMethod", "")} {req.get("requestUrl", "")} → '
               f'{req.get("status", "")} ({req.get("latency", "")}) {msg}').strip()
    return {"timestamp": entry.timestamp, "severity": entry.severity or "DEFAULT",
            "message": msg[:4000], "raw": raw}


@app.get("/api/logs/job/{name}/executions")
def job_executions(name: str, region: str):
    """Every execution of this job from the same 2-day window the logs themselves
    use — not a fixed count. A job that runs every 10 minutes should list all of
    today's and yesterday's runs, however many that is; a job that runs weekly
    should list just the one or two that fall in that window."""
    if not _RUN_NAME.match(name) or not _RUN_NAME.match(region):
        raise HTTPException(400, "invalid job name or region")
    cutoff = datetime.now(timezone.utc) - _LOG_TARGETS["job"][2]
    SAFETY_CAP = 200  # guards only against a pathological once-a-minute job; not a normal limit
    try:
        from google.cloud import run_v2
        client = run_v2.ExecutionsClient()
        parent = f"projects/{LOG_PROJECT}/locations/{region}/jobs/{name}"
        execs = []
        for e in client.list_executions(parent=parent):  # API returns newest-first
            start = e.start_time or e.create_time
            if start and start < cutoff:
                break  # everything from here on is even older than the window
            execs.append({
                "execution": e.name.split("/")[-1],
                "started_at": start.isoformat() if start else None,
                "completed_at": e.completion_time.isoformat() if e.completion_time else None,
                "status": "running" if not e.completion_time
                          else ("failed" if e.failed_count else "succeeded"),
            })
            if len(execs) >= SAFETY_CAP:
                break
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else ""
        raise HTTPException(503, f"Executions unavailable: {type(e).__name__}: {first_line}")
    return execs


@app.get("/api/logs/{kind}/{name}")
def logs(kind: str, name: str, execution: str = None, errors_only: bool = False, limit: int = 200):
    if kind not in _LOG_TARGETS or not _RUN_NAME.match(name):
        raise HTTPException(400, "unknown log target")
    if execution is not None and not _RUN_NAME.match(execution):
        raise HTTPException(400, "invalid execution name")
    resource_type, label, window = _LOG_TARGETS[kind]
    limit = max(1, min(limit, 500))
    since = (datetime.now(timezone.utc) - window).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = (f'resource.type="{resource_type}" AND resource.labels.{label}="{name}" '
            f'AND timestamp>="{since}"')

    try:
        from google.cloud import logging as gcp_logging
        client = gcp_logging.Client(project=LOG_PROJECT)
        if kind == "job":
            if execution:
                # A specific run was picked from the execution dropdown.
                base += f' AND labels."{_EXEC_LABEL}"="{execution}"'
            else:
                # No run picked (first load) — default to whichever one logged most
                # recently, same as before the dropdown existed.
                newest = next(iter(client.list_entries(
                    filter_=base, order_by=gcp_logging.DESCENDING, max_results=1)), None)
                execution = newest.labels.get(_EXEC_LABEL) if newest and newest.labels else None
                if execution:
                    base += f' AND labels."{_EXEC_LABEL}"="{execution}"'
        flt = base + (" AND severity>=ERROR" if errors_only else "")
        entries = client.list_entries(filter_=flt, order_by=gcp_logging.DESCENDING,
                                      max_results=limit, page_size=limit)
        lines = [_log_line(e) for e in entries]
    except Exception as e:
        # Typically no GCP credentials (local run) or a Logging API quota hit.
        first_line = str(e).splitlines()[0] if str(e) else ""
        raise HTTPException(503, f"Logs unavailable: {type(e).__name__}: {first_line}")

    return {"kind": kind, "name": name, "execution": execution,
            "since": since, "lines": lines}


# Same normalization rule the collector uses (see gcp_collector's
# _normalize_path) — kept here too since this endpoint reads Cloud Logging
# live rather than the collector's stored per-run snapshot, to give a fuller
# window (24h) when someone clicks in to investigate a service.
_ENDPOINT_ID_SEGMENT = re.compile(
    r'^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\d+|[0-9a-zA-Z]{20,})$'
)


def _normalize_endpoint_path(path):
    return '/'.join('{id}' if _ENDPOINT_ID_SEGMENT.match(seg) else seg for seg in path.split('/'))


def _is_timeout(status, text):
    """A request Cloud Run cut off at the container/server timeout (504, or its own
    "maximum request timeout" line) — shown as a timeout, not as an app error."""
    return status == 504 or "maximum request timeout" in (text or "")


SUMMARY_MODEL = os.environ.get("OPENAI_SUMMARY_MODEL", "gpt-4o-mini")
_DESC_CACHE = {}   # (service, path, error text) -> description; keeps AI calls rare


def _describe_errors(name, samples):
    """{path: short plain-English reason} for endpoints with app errors. Only the
    error text is sent to the AI (never successes), in one cheap gpt-4o-mini call,
    and cached. Without OPENAI_API_KEY (or on failure) falls back to the raw error line."""
    import urllib.request
    out, todo = {}, {}
    for path, msgs in samples.items():
        key = (name, path, "\n".join(msgs)[:2000])
        if key in _DESC_CACHE:
            out[path] = _DESC_CACHE[key]
        elif not msgs:
            out[path] = "Server error (500) — no app error line found"
        else:
            todo[path] = key
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    fallback = lambda path: samples[path][0][:140]
    if todo and api_key:
        try:
            body = json.dumps({
                "model": SUMMARY_MODEL, "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You explain server errors to a non-expert. For each "
                     "endpoint, write ONE short sentence (max 15 words) in very simple English saying what "
                     "went wrong. Use only the error text given. Reply as JSON: {\"<endpoint>\": \"<sentence>\"}."},
                    {"role": "user", "content": json.dumps({p: samples[p][:3] for p in todo})[:12000]}],
                "max_tokens": 600, "temperature": 0.2}).encode()
            req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body, headers={
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                got = json.loads(json.load(r)["choices"][0]["message"]["content"])
            for path, key in todo.items():
                if isinstance(got.get(path), str) and got[path].strip():
                    out[path] = _DESC_CACHE[key] = got[path].strip()
        except Exception:
            pass
    for path in todo:
        out.setdefault(path, fallback(path))
    return out


@app.get("/api/services/{name}/endpoints")
def service_endpoints(name: str, hours: int = 24):
    """Per-URL request/error breakdown for a Cloud Run service, read live from
    Cloud Logging — Cloud Run logs every request's URL and status on its own,
    no app change needed. An aggregate error rate can hide one broken endpoint
    inside otherwise-healthy traffic; this is how that's found."""
    if not _RUN_NAME.match(name):
        raise HTTPException(400, "invalid service name")
    hours = max(1, min(hours, 168))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    flt = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{name}" '
           f'AND httpRequest.status>0 AND timestamp>="{since}"')

    try:
        from google.cloud import logging as gcp_logging
        from urllib.parse import urlparse
        client = gcp_logging.Client(project=LOG_PROJECT)
        by_path = {}
        for entry in client.list_entries(filter_=flt, order_by=gcp_logging.DESCENDING,
                                          max_results=2000, page_size=1000):
            req = entry.http_request or {}
            status = req.get("status") or 0
            path = _normalize_endpoint_path(urlparse(req.get("requestUrl") or "").path or "/")
            d = by_path.setdefault(path, {"requests": 0, "errors": 0, "timeouts": 0, "traces": []})
            d["requests"] += 1
            if status >= 500:
                d["errors"] += 1
                if _is_timeout(status, ""):
                    d["timeouts"] += 1
                elif entry.trace and len(d["traces"]) < 5:
                    d["traces"].append(entry.trace)
        # The app's own ERROR line (matched by trace) says *why* a request failed.
        wanted = {t for d in by_path.values() for t in d["traces"]}
        app_msgs = {}
        if wanted:
            for entry in client.list_entries(
                    filter_=flt.replace("httpRequest.status>0", "severity>=ERROR AND NOT httpRequest.status>0"),
                    order_by=gcp_logging.DESCENDING, max_results=500, page_size=500):
                if entry.trace in wanted and entry.trace not in app_msgs:
                    app_msgs[entry.trace] = _log_line(entry)["message"]
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else ""
        raise HTTPException(503, f"Endpoint breakdown unavailable: {type(e).__name__}: {first_line}")

    descriptions = _describe_errors(name, {
        p: [app_msgs[t] for t in d["traces"] if t in app_msgs] for p, d in by_path.items()
        if d["errors"] > d["timeouts"]})
    endpoints = [
        {"path": p, "requests": d["requests"], "errors": d["errors"], "timeouts": d["timeouts"],
         "error_rate_pct": round(100 * d["errors"] / d["requests"], 1),
         "description": descriptions.get(p)}
        for p, d in by_path.items()
    ]
    endpoints.sort(key=lambda r: (-r["errors"], -r["requests"]))
    return {"since_hours": hours, "endpoints": endpoints}


def _endpoint_error_logs(name, path, hours, limit=50):
    """5xx request logs for one (normalized) endpoint path, each paired with the
    app's own ERROR log line from the same request (matched by trace) when there
    is one — the request log alone only says "500", the app line says why."""
    from google.cloud import logging as gcp_logging
    from urllib.parse import urlparse
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{name}" '
            f'AND timestamp>="{since}"')
    client = gcp_logging.Client(project=LOG_PROJECT)
    requests_ = []
    for entry in client.list_entries(filter_=base + " AND httpRequest.status>=500",
                                     order_by=gcp_logging.DESCENDING, max_results=500, page_size=500):
        req = entry.http_request or {}
        if _normalize_endpoint_path(urlparse(req.get("requestUrl") or "").path or "/") == path:
            requests_.append(entry)
            if len(requests_) >= limit:
                break
    traces = {e.trace for e in requests_ if e.trace}
    app_msgs = {}
    if traces:
        for entry in client.list_entries(filter_=base + " AND severity>=ERROR AND NOT httpRequest.status>0",
                                         order_by=gcp_logging.DESCENDING, max_results=500, page_size=500):
            if entry.trace in traces and entry.trace not in app_msgs:
                app_msgs[entry.trace] = _log_line(entry)["message"]
    lines = []
    for e in requests_:
        line = _log_line(e)
        line["detail"] = app_msgs.get(e.trace)
        line["timeout"] = _is_timeout((e.http_request or {}).get("status"), line["detail"])
        lines.append(line)
    return lines


@app.get("/api/services/{name}/endpoints/logs")
def service_endpoint_logs(name: str, path: str, hours: int = 24):
    if not _RUN_NAME.match(name):
        raise HTTPException(400, "invalid service name")
    hours = max(1, min(hours, 168))
    try:
        return {"path": path, "since_hours": hours, "lines": _endpoint_error_logs(name, path, hours)}
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else ""
        raise HTTPException(503, f"Endpoint logs unavailable: {type(e).__name__}: {first_line}")


# Optional AI summary of an endpoint's errors. Off unless OPENAI_API_KEY is set on
# the UI service; triggered only by a button click so it costs nothing otherwise.
@app.post("/api/services/{name}/endpoints/summary")
def service_endpoint_summary(name: str, path: str, hours: int = 24):
    import urllib.request, urllib.error
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise HTTPException(503, "AI summary not configured (OPENAI_API_KEY not set on the UI service)")
    if not _RUN_NAME.match(name):
        raise HTTPException(400, "invalid service name")
    hours = max(1, min(hours, 168))
    try:
        lines = _endpoint_error_logs(name, path, hours, limit=30)
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else ""
        raise HTTPException(503, f"Endpoint logs unavailable: {type(e).__name__}: {first_line}")
    if not lines:
        return {"summary": "No 5xx errors for this endpoint in this window."}
    text = "\n".join(f'{l["timestamp"]} {l["message"]}' + (f' | app log: {l["detail"]}' if l["detail"] else "")
                     for l in lines)[:12000]
    body = json.dumps({
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": "You summarise server error logs for a developer. In 2-3 short "
             "sentences say what is failing on this endpoint, the likely cause, and what to check first. "
             "Use only the logs given; if the cause is unclear, say so."},
            {"role": "user", "content": f"Service {name}, endpoint {path}, last {hours}h. "
             f"{len(lines)} recent 5xx errors:\n{text}"}],
        "max_tokens": 200, "temperature": 0.2}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"summary": json.load(r)["choices"][0]["message"]["content"].strip()}
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"OpenAI returned HTTP {e.code}")
    except Exception as e:
        raise HTTPException(502, f"OpenAI request failed: {type(e).__name__}")


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
                 WHERE retired = false AND review_status = 'needs_review') AS needs_review,
                (SELECT COUNT(*) FROM sentinel.incidents WHERE resolved_at IS NULL) AS open_incidents,
                (SELECT COUNT(*) FROM sentinel.reconciliation_rules WHERE status = 'active') AS active_rules,
                (SELECT COUNT(*) FROM sentinel.monitor_targets WHERE status = 'active') AS active_targets
        """)
        return dict(cur.fetchone())


@app.get("/api/sentinel/health")
def sentinel_health():
    """Plain-language data-health summary for the Overview — counts of tables by
    the latest freshness verdict (sync + data), needs-review, open incidents."""
    with get_db() as cur:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (database_name, table_name) database_name, table_name, observed
                FROM sentinel.check_results
                WHERE check_type='freshness' AND COALESCE(variable,'')=''
                ORDER BY database_name, table_name, run_ts DESC
            )
            SELECT
              (SELECT count(*) FROM sentinel.catalog_overlay WHERE retired=false) AS tables_total,
              (SELECT count(*) FROM latest) AS tables_checked,
              (SELECT count(*) FROM latest WHERE observed->>'sub_status'='fresh') AS fresh,
              (SELECT count(*) FROM latest WHERE observed->'sync'->>'sub_status' IN ('stale','dead')) AS sync_problem,
              (SELECT count(*) FROM latest WHERE observed->'data'->>'sub_status' IN ('stale','dead')) AS data_problem,
              (SELECT count(*) FROM sentinel.catalog_overlay WHERE retired=false AND review_status='needs_review') AS needs_review,
              (SELECT count(*) FROM sentinel.incidents WHERE resolved_at IS NULL AND check_type<>'rollup') AS open_incidents
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
    """Rows that still need a human decision. A CONFIRMED row is resolved — even if it
    carries an informational conflict_type — so it is NOT in the queue. Only unresolved
    reviews appear: review_status='needs_review', plus genuinely broken conflicts that
    make a table unusable (dangling/broken pointer, unresolved dedup) regardless of
    review_status (those are correctness blockers, not just ambiguity)."""
    with get_db() as cur:
        cur.execute("""
            SELECT database_name, table_name, concept, scope, source_type,
                   conflict_type, review_status, review_reason, authoritative,
                   use_instead, authority_confidence, updated_by, is_static
            FROM sentinel.catalog_overlay
            WHERE retired = false
              AND (review_status = 'needs_review'
                   OR conflict_type IN ('dangling_pointer','broken_pointer',
                                        'unresolved_dedup','broken_join'))
            ORDER BY (review_status='needs_review') DESC, authority_confidence NULLS FIRST
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
                    -- sync vs data freshness (migration 016)
                    observed->'sync'->>'sub_status' AS sync_status,
                    observed->'sync'->>'last_write' AS sync_last_write,
                    observed->'data'->>'sub_status' AS data_status,
                    observed->'data'->>'column'     AS data_column,
                    observed->'data'->>'max_event_date' AS data_last,
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
                   sen.sync_status, sen.sync_last_write,
                   sen.data_status, sen.data_column, sen.data_last,
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
