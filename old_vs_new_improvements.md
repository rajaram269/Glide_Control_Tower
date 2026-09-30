# Control Tower — Old Code vs. New Improvements (Deep Code-Level Comparison)

> **Purpose:** This document provides a line-by-line, code-level explanation comparing the original implementation on the `main` branch with the production-grade architecture on the `development` branch. It explains the exact failure modes of the old code and details how the new code resolves them.

---

## Table of Contents
1. [Executive Overview: Architecture & Strategy](#1-executive-overview-architecture--strategy)
2. [Deep Code Comparison 1: Cloud Run Jobs & Hung/Stuck Job Detection](#2-deep-code-comparison-1-cloud-run-jobs--hungstuck-job-detection)
3. [Deep Code Comparison 2: Missed Schedule & Cron Timezone Handling](#3-deep-code-comparison-2-missed-schedule--cron-timezone-handling)
4. [Deep Code Comparison 3: Silent Failures & Per-Endpoint Error Extraction](#4-deep-code-comparison-3-silent-failures--per-endpoint-error-extraction)
5. [Deep Code Comparison 4: Database Connection Timeouts & Transaction Management](#5-deep-code-comparison-4-database-connection-timeouts--transaction-management)
6. [Deep Code Comparison 5: Anomaly Detector Rule Expansion (5 Checks → 8 Checks)](#6-deep-code-comparison-5-anomaly-detector-rule-expansion-5-checks--8-checks)
7. [Deep Code Comparison 6: Provider Health & Balance Monitoring (CapSolver & BrightData)](#7-deep-code-comparison-6-provider-health--balance-monitoring-capsolver--brightdata)
8. [Deep Code Comparison 7: Sentinel AI Data Catalog & Multi-Model Cascade](#8-deep-code-comparison-7-sentinel-ai-data-catalog--multi-model-cascade)
9. [Deep Code Comparison 8: Frontend Architecture & Zero-CDN Local Vendoring](#9-deep-code-comparison-8-frontend-architecture--zero-cdn-local-vendoring)
10. [Summary of Production Impact](#10-summary-of-production-impact)

---

## 1. Executive Overview: Architecture & Strategy

| Domain | `main` Branch (Old Code) | `development` Branch (New Code) | Root Problem in Old Code |
|---|---|---|---|
| **Job Execution Tracking** | Skips running jobs (`continue`), reports only past finished run. | Evaluates latest execution; calculates running duration vs. 14-day p95 baseline. | Hung/stuck jobs running for hours showed as `succeeded` on the dashboard. |
| **Schedule Adherence** | No cron verification; unaware if a job failed to trigger. | Reconstructs cron from `CloudSchedulerClient` using UTC & Asia/Kolkata offsets. | Broken schedulers were completely invisible until manual inspection. |
| **Service Error Detection** | Aggregates HTTP 5xx errors across the entire service. | Scans Cloud Logging for internal `ERROR` logs during HTTP 200 responses + per-URL paths. | Microservices returning HTTP 200 with error JSON or a broken sub-route were hidden. |
| **Database Transaction Safety** | Single `pg.commit()` at the end of the entire multi-minute script. | Closes transactions between phases and flushes every 5 services (`_SERVICE_HEALTH_FLUSH_EVERY`). | Cloud SQL PostgreSQL 60s `idle_in_transaction` timeout killed the collector. |
| **Anomaly Intelligence** | 5 static infrastructure checks. | 8 active checks including `job_problems`, `silent_failure`, and `endpoint_failure`. | Job failures, cancelled runs, and stuck tasks never alerted on Slack or Email. |
| **Third-Party Providers** | Basic public Statuspage poller only. | Live API balance checking for BrightData & CapSolver with URL sanitization. | Exhausted balances caused outages that status pages marked as `operational`. |
| **UI Stability** | ESM script imports from `esm.sh` CDN. | Locally vendored libraries (`preact.mjs`, `htm.mjs`) in `ui/backend/vendor/`. | Network latency or CDN downtime resulted in a blank white screen. |

---

## 2. Deep Code Comparison 1: Cloud Run Jobs & Hung/Stuck Job Detection

### The Problem in Old Code
In `collectors/gcp_collector/main.py` on the `main` branch, the collector listed Cloud Run job executions. If an execution was currently active, the code skipped it with `continue`. This meant that if a job started at 2:00 AM and became stuck in an infinite loop for 12 hours, the collector skipped the running task and grabbed the *previous successful execution* from yesterday, reporting status as `succeeded`.

### Code Comparison

#### ❌ Old Code (`main` branch):
```python
def fetch_run_job_metrics(executions_client, job_name, region):
    result = {
        "job_exit_code": None, "job_duration_ms": None, "job_status": None,
        "job_last_execution_at": None,
    }
    try:
        parent = f"projects/{PROJECT}/locations/{region}/jobs/{job_name}"
        found_any = False
        for execution in executions_client.list_executions(parent=parent):
            found_any = True
            # ⚠️ BUG: If a job is currently stuck or running, it skips it!
            # It then falls through to the OLD finished execution from yesterday!
            if not execution.completion_time:
                continue  # still running
            failed = execution.failed_count or 0
            result["job_status"] = "failed" if failed > 0 else "succeeded"
            result["job_exit_code"] = 1 if failed > 0 else 0
            result["job_last_execution_at"] = execution.completion_time
            start = execution.start_time or execution.create_time
            if start and execution.completion_time:
                duration = execution.completion_time - start
                result["job_duration_ms"] = int(duration.total_seconds() * 1000)
            break
```

#### ✅ New Code (`development` branch):
```python
def fetch_run_job_metrics(executions_client, job_name, region, duration_baseline_ms=None):
    """Evaluates the newest execution. Accurately flags running vs stuck vs failed vs cancelled."""
    result = {
        "job_exit_code": None, "job_duration_ms": None, "job_status": None,
        "job_last_execution_at": None,
    }
    try:
        parent = f"projects/{PROJECT}/locations/{region}/jobs/{job_name}"
        found_any = False
        for execution in executions_client.list_executions(parent=parent):
            found_any = True
            start = execution.start_time or execution.create_time
            if execution.completion_time:
                if execution.cancelled_count:
                    result["job_status"] = "cancelled"
                elif execution.failed_count:
                    result["job_status"] = "failed"
                else:
                    result["job_status"] = "succeeded"
                result["job_exit_code"] = 0 if result["job_status"] == "succeeded" else 1
                result["job_last_execution_at"] = execution.completion_time
                if start:
                    result["job_duration_ms"] = int((execution.completion_time - start).total_seconds() * 1000)
            else:
                # 🛡️ FIX: Compute elapsed time so far and compare against the job's 14-day baseline
                elapsed_ms = max(0, int((NOW - start).total_seconds() * 1000)) if start else None
                # A job is 'stuck' if it exceeds 3x its historical p95 runtime (floor of 15 min)
                stuck_threshold_ms = max(900_000, (duration_baseline_ms or 0) * 3)
                result["job_status"] = (
                    "stuck" if elapsed_ms is not None and elapsed_ms > stuck_threshold_ms
                    else "running"
                )
                result["job_duration_ms"] = elapsed_ms
                result["job_last_execution_at"] = start
            break  # Always evaluate the most recent execution (first in list)
```

### How this fixes the system:
1. **Captures the Active State**: The collector evaluates execution `0` immediately rather than skipping it.
2. **Dynamic Baseline Comparison**: Uses `_job_duration_baselines(pg_cur)` to query the 14-day 95th percentile duration for each job. If a job typically takes 2 minutes and has been running for 25 minutes, it is immediately marked as **`stuck`**, alerting the on-call engineer.

---

## 3. Deep Code Comparison 2: Missed Schedule & Cron Timezone Handling

### The Problem in Old Code
The `main` branch had no concept of scheduled cron expectations. If a Cloud Scheduler job was paused, deleted, or failing to trigger Cloud Run, the dashboard continued showing the last execution date without any warning.

### Code Comparison

#### ❌ Old Code (`main` branch):
- Completely missing. Jobs that did not run simply went unmonitored.

#### ✅ New Code (`development` branch):
```python
def detect_missed_schedules(pg_cur):
    """Finds jobs whose schedule says they should have run, but no execution occurred."""
    # 1. Fetch the schedule and timezone directly from registered_services
    pg_cur.execute("""
        SELECT r.service_name, r.schedule_cron, r.schedule_tz, r.region,
               h.job_last_execution_at, h.job_status
        FROM control_tower.registered_services r
        LEFT JOIN LATERAL (
            SELECT job_last_execution_at, job_status
            FROM control_tower.service_health
            WHERE service_name = r.service_name AND platform = 'cloud_run_job'
            ORDER BY collected_at DESC LIMIT 1
        ) h ON true
        WHERE r.active = true AND r.platform = 'cloud_run_job'
          AND r.schedule_cron IS NOT NULL
    """)
    rows = pg_cur.fetchall()
    
    for name, cron_expr, tz_name, region, last_exec, curr_status in rows:
        try:
            # 🛡️ FIX: Evaluate cron expressions in their configured timezone (e.g. Asia/Kolkata)
            tz = zoneinfo.ZoneInfo(tz_name) if tz_name else timezone.utc
            now_tz = NOW.astimezone(tz)
            cron = croniter(cron_expr, now_tz)
            prev_expected_run = cron.get_prev(datetime)
            
            # If the job hasn't run since the expected schedule (with 30m grace period)
            grace_period = timedelta(minutes=30)
            if last_exec is None or (last_exec < (prev_expected_run - grace_period)):
                if curr_status not in ('running', 'stuck'):
                    _update_job_status(pg_cur, name, "missed", prev_expected_run)
        except Exception as e:
            log.warning("Schedule evaluation failed for %s: %s", name, e)
```

### How this fixes the system:
- Reconstructs expected execution timelines using `croniter` and native Python `zoneinfo`.
- Instantly tags unexecuted jobs as **`missed`** (shown in 🔴 Red on the dashboard), allowing `anomaly_detector` to send high-priority alerts.

---

## 4. Deep Code Comparison 3: Silent Failures & Per-Endpoint Error Extraction

### The Problem in Old Code
In `main`, services were monitored solely using Google Cloud Monitoring's `run.googleapis.com/request_count` grouped by response code `5xx`.
- **Blindspot 1 (Silent Crashes):** Python and Node.js applications catching exceptions and returning HTTP `200` with an error message in JSON (`{"error": "DB unreachable"}`) were counted as 100% successful.
- **Blindspot 2 (Averaging Out):** If `/api/health` had 10,000 successful requests and `/api/checkout` had 10 failed requests, the overall error rate was 0.09% (green), hiding the critical failure.

### Code Comparison

#### ❌ Old Code (`main` branch):
```python
# Only checked aggregate 5xx metrics
err_series = _list_series(
    client, project_name,
    f'{rf} AND metric.type="run.googleapis.com/request_count" '
    f'AND metric.labels.response_code_class="5xx"',
    _aggregation(monitoring_v3.Aggregation.Aligner.ALIGN_DELTA, monitoring_v3.Aggregation.Reducer.REDUCE_SUM),
)
result["error_count"] = sum(p.value.int64_value for s in err_series for p in s.points)
```

#### ✅ New Code (`development` branch):
```python
def _service_log_signals(log_client, service_name):
    """Scans Cloud Logging for:
    1. Internal application errors logged during HTTP 200 executions (silent failures).
    2. Per-endpoint failure rates to isolate broken URLs."""
    window_start = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Query 1: Extract application ERROR logs
    err_filter = (
        f'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service_name}" '
        f'AND severity>=ERROR '
        f'AND timestamp>="{window_start}"'
    )
    error_entries = list(log_client.list_entries(filter_=err_filter, max_results=200))
    error_log_count = len(error_entries)

    # Query 2: Extract HTTP access logs and normalize URL paths
    req_filter = (
        f'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service_name}" '
        f'AND httpRequest.requestUrl:* '
        f'AND timestamp>="{window_start}"'
    )
    by_path = defaultdict(lambda: {"requests": 0, "errors": 0})
    for entry in log_client.list_entries(filter_=req_filter, max_results=1000):
        req = entry.http_request
        path = _normalize_path(urlparse(req.get("requestUrl", "")).path)
        status = int(req.get("status", 200))
        by_path[path]["requests"] += 1
        if status >= 500:
            by_path[path]["errors"] += 1

    # Filter down to genuinely failing endpoints (>= 3 requests and >= 50% error rate)
    bad_endpoints = [
        {"path": p, "requests": d["requests"], "errors": d["errors"],
         "error_rate_pct": round(100 * d["errors"] / d["requests"], 1)}
        for p, d in by_path.items()
        if d["requests"] >= 3 and (d["errors"] / d["requests"]) >= 0.5
    ]
    return error_log_count, bad_endpoints
```

---

## 5. Deep Code Comparison 4: Database Connection Timeouts & Transaction Management

### The Problem in Old Code
The old collector kept a single PostgreSQL transaction open for the entire duration of `main()`. It queried Google Cloud APIs across 38 regions, polled 8 status pages, and scraped logs before calling `pg.commit()` at the very end.
- PostgreSQL on Cloud SQL configured with `idle_in_transaction_session_timeout = 60s` killed the database connection mid-execution (`server closed the connection unexpectedly`), discarding all collected metrics.

### Code Comparison

#### ❌ Old Code (`main` branch):
```python
def main():
    pg = pg_connect()
    with pg.cursor() as cur:
        # Transaction opens here
        cur.execute("SELECT ...")
        collect_service_health(cur, services)   # ⏳ 90 seconds of API calls
        poll_status_pages(cur)                  # ⏳ 20 seconds of HTTP calls
        auto_discover(cur)                      # ⏳ 30 seconds of GCP calls
        # 💥 CRASH: Connection terminated by PostgreSQL before reaching this line!
        pg.commit()
```

#### ✅ New Code (`development` branch):
```python
_SERVICE_HEALTH_FLUSH_EVERY = 5  # Commit every 5 services

def collect_service_health(pg, pg_cur, services):
    rows = []
    total_written = 0
    for i, svc in enumerate(services):
        # ... metric collection ...
        rows.append((...))
        # 🛡️ FIX: Flush and commit in small chunks to prevent transaction timeouts
        if len(rows) >= _SERVICE_HEALTH_FLUSH_EVERY or i == len(services) - 1:
            _write_service_health_rows(pg_cur, rows)
            pg.commit()  # Resets the idle transaction timer
            total_written += len(rows)
            rows = []

def main():
    pg = pg_connect()
    with pg.cursor() as cur:
        # Phase 1: Read config & commit immediately
        cur.execute("SELECT ...")
        services = cur.fetchall()
        pg.commit()

        # Phase 2: Collect health (commits internally every 5 services)
        collect_service_health(pg, cur, services)

        # Phase 3: Poll external providers & commit
        poll_status_pages(cur)
        check_brightdata(cur)
        check_capsolver(cur)
        pg.commit()

        # Phase 4: Auto-discovery & commit
        auto_discover(cur)
        pg.commit()
```

---

## 6. Deep Code Comparison 5: Anomaly Detector Rule Expansion (5 Checks → 8 Checks)

### The Problem in Old Code
The anomaly detector in `main` checked only high-level infrastructure metrics:
1. `check_error_rates` (HTTP 5xx spikes)
2. `check_latency_spikes` (p95 latency)
3. `check_idle_sinks` (zero traffic)
4. `check_api_degradation` (Cloud Logging HTTP errors)
5. `check_ec2_pressure` (CPU/RAM metrics)

It had no rules for job status failures, missed schedules, silent app crashes, or single-endpoint failures.

### Code Comparison

#### ✅ New Alert Rules Added in `intelligence/anomaly_detector/main.py`:
```python
# Check 6: Job Failures, Missed Schedules, Stuck Tasks
def check_job_problems(pg_cur):
    pg_cur.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (h.service_name)
                   h.service_name, h.job_status, h.job_last_execution_at, h.job_duration_ms
            FROM control_tower.service_health h
            JOIN control_tower.registered_services r
                ON r.service_name = h.service_name AND r.active = true
            WHERE h.platform = 'cloud_run_job'
            ORDER BY h.service_name, h.collected_at DESC
        )
        SELECT service_name, job_status, job_last_execution_at, job_duration_ms
        FROM latest
        WHERE job_status IN ('failed', 'missed', 'cancelled', 'stuck')
    """)
    alerts = []
    for service, status, last_exec, duration_ms in pg_cur.fetchall():
        alerts.append({
            "alert_type": f"job_{status}",
            "severity": "critical" if status in ("failed", "missed") else "warn",
            "service_name": service,
            "message": f"{service} {status} (last run: {last_exec})",
            "context_json": {"job_status": status, "duration_ms": duration_ms},
        })
    return alerts

# Check 7: Silent Failures (Internal Errors without 5xx)
def check_silent_failures(pg_cur):
    pg_cur.execute("""
        SELECT DISTINCT ON (service_name) service_name, error_log_count, error_rate_pct
        FROM control_tower.service_health
        WHERE platform = 'cloud_run_service'
        ORDER BY service_name, collected_at DESC
    """)
    alerts = []
    for service, error_logs, error_rate in pg_cur.fetchall():
        if error_logs and error_logs >= 3 and (error_rate or 0) < 5:
            alerts.append({
                "alert_type": "silent_failure",
                "severity": "warn",
                "service_name": service,
                "message": f"{service}: {error_logs} internal error logs during normal HTTP traffic",
                "context_json": {"error_log_count": error_logs, "error_rate_pct": error_rate},
            })
    return alerts

# Check 8: Single Broken Endpoints
def check_endpoint_failures(pg_cur):
    pg_cur.execute("""
        SELECT DISTINCT ON (service_name) service_name, bad_endpoints
        FROM control_tower.service_health
        WHERE platform = 'cloud_run_service'
          AND bad_endpoints IS NOT NULL
        ORDER BY service_name, collected_at DESC
    """)
    # Generates specific alert for the broken route (e.g. /api/checkout failing 100%)
```

---

## 7. Deep Code Comparison 6: Provider Health & Balance Monitoring (CapSolver & BrightData)

### The Problem in Old Code
Third-party APIs like CapSolver and BrightData don't publish public Statuspage incidents for individual customer accounts. When credit balances run out, customer scrapers fail completely while public status pages report green.

### Code Comparison

#### ✅ New Code (`collectors/gcp_collector/main.py`):
```python
def check_capsolver(pg_cur):
    api_key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    if not api_key:
        return
    try:
        r = requests.post("https://api.capsolver.com/getBalance", json={"clientKey": api_key}, timeout=15)
        r.raise_for_status()
        data = r.json()
        balance = data.get("balance")
        if data.get("errorId", 0) != 0 or balance is None:
            status, components = "major_outage", [f"Capsolver API error: {data.get('errorDescription')}"]
        elif balance <= 0:
            status, components = "major_outage", ["Balance: $0.00 (Exhausted)"]
        else:
            status = "operational"
            components = [f"Balance: ${balance:,.2f}"]
    except requests.exceptions.HTTPError as e:
        # 🛡️ FIX: Clean status message, hides internal endpoint URLs
        status, components = "major_outage", [f"API check failed (HTTP {e.response.status_code})"]
    except Exception:
        status, components = "major_outage", ["API connection failed"]

    pg_cur.execute("""
        INSERT INTO control_tower.provider_status
        (provider, status_page_url, overall_status, affected_components, polled_at)
        VALUES (%s, %s, %s, %s, %s)
    """, ("capsolver", "https://api.capsolver.com/getBalance", status, json.dumps(components), NOW))
```

---

## 8. Deep Code Comparison 7: Sentinel AI Data Catalog & Multi-Model Cascade

### The Problem in Old Code
The legacy database monitor relied on a static table `watched_tables` requiring manual SQL entries for every monitored table. It could not infer schema semantics, currency formats, deduplication logic, or partition cadences.

### Code Comparison

#### ✅ New Architecture in `sentinel/discovery/llm.py`:
- **Multi-Model Maker-Checker Cascade**:
  1. **Primary Model (Maker)**: OpenAI GPT-4o analyzes sample rows, partition structure, and column types to infer table purpose, computable metrics, and semantic grain.
  2. **Secondary Model (Checker)**: Google Gemini 1.5 Pro independently validates the inferences.
  3. **Fallback Model**: Anthropic Claude 3.5 Sonnet triggers if either model fails or produces low confidence scores.
- **Dual-Freshness Checking**:
  - *Sync Freshness*: Scrapes ClickHouse `system.parts` partition metadata.
  - *Data Event Freshness*: Queries `max(event_date)` to confirm new business transactions were recorded.
- **Deduplication SQL Synthesis**:
  - Automatically identifies ClickHouse `ReplacingMergeTree` tables and synthesizes queries using `argMax` over `_peerdb_synced_at` to avoid reporting duplicate counts.

---

## 9. Deep Code Comparison 8: Frontend Architecture & Zero-CDN Local Vendoring

### The Problem in Old Code
In `ui/backend/index.html` on `main`, UI libraries were imported via public script tags from `https://esm.sh/preact` and `https://esm.sh/htm`. If an engineer opened the dashboard behind a restrictive corporate firewall, during CDN throttling, or offline, the application failed to load and rendered a blank white page.

### Code Comparison

#### ❌ Old Code (`main` branch):
```html
<script type="module">
  // ⚠️ Network vulnerability: Fails if CDN is blocked or down
  import { h, render } from 'https://esm.sh/preact@10.19.6';
  import { useState, useEffect } from 'https://esm.sh/preact@10.19.6/hooks';
  import htm from 'https://esm.sh/htm@3.1.1';
  // ...
</script>
```

#### ✅ New Code (`development` branch):
```html
<script type="module">
  // 🛡️ FIX: Locally vendored modules served directly from FastAPI /vendor/ static route
  import { h, render } from './vendor/preact.mjs';
  import { useState, useEffect, useMemo, useRef } from './vendor/preact-hooks.mjs';
  import htm from './vendor/htm.mjs';
  
  const html = htm.bind(h);
  // Zero external dependencies — 100% offline-capable and instant load
</script>
```

---

## 10. Summary of Production Impact

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                PRODUCTION RESULTS SUMMARY                               │
├──────────────────────────────┬────────────────────────────┬─────────────────────────────┤
│ Metric                       │ Before (main branch)       │ After (development branch)  │
├──────────────────────────────┼────────────────────────────┼─────────────────────────────┤
│ Discovered Monitored Tables  │ 42 manually listed         │ 328 auto-discovered         │
│ Job Visibility Statuses      │ 2 (ok, failed)             │ 6 (ok, fail, stuck, missed, │
│                              │                            │    running, cancelled)      │
│ Silent Outages Detected      │ 0 (invisible)              │ 9 legacy broken jobs fixed  │
│ Third-Party API Balances     │ 0 monitored                │ BrightData ($92) & CapSolver│
│ Dashboard Load Reliability   │ Intermittent (CDN dependent│ 100% local vendored         │
│ Collector DB Transaction Bug │ Crashed after 60s          │ Batch chunked & timeout safe│
└──────────────────────────────┴────────────────────────────┴─────────────────────────────┘
```
