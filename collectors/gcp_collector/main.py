"""
GCP Collector — Control Tower
Hourly Cloud Run job. Pulls Cloud Monitoring metrics for all active GCP services,
polls third-party status pages, auto-discovers new Cloud Run services/jobs,
runs the data freshness checker, queries Cloud Logging for API health signals,
then pings healthchecks.io.
"""
import os, json, re, time, datetime, logging
import requests
import psycopg2
from psycopg2.extras import execute_values
from google.cloud import monitoring_v3, logging as gcp_logging, run_v2, scheduler_v1
from google.cloud.monitoring_v3.types import ListTimeSeriesRequest
import clickhouse_connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PROJECT = os.environ.get("GCP_PROJECT", "seoai-479305")
PG_CONN = os.environ["PG_CONN"]
CH_HOST = os.environ["CH_HOST"]
CH_USER = os.environ["CH_USER"]
CH_PASS = os.environ["CH_PASS"]
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

COLLECTION_WINDOW_MINUTES = 65
NOW = datetime.datetime.now(datetime.timezone.utc)
WINDOW_START = NOW - datetime.timedelta(minutes=COLLECTION_WINDOW_MINUTES)

# Statuspage.io providers — use /api/v2/status.json format
STATUSPAGE_PROVIDERS = {
    "openai":     "https://status.openai.com/api/v2/status.json",
    "anthropic":  "https://status.anthropic.com/api/v2/status.json",
    "replicate":  "https://www.replicatestatus.com/api/v2/status.json",
    "cloudflare": "https://www.cloudflarestatus.com/api/v2/status.json",
    "cohere":     "https://status.cohere.com/api/v2/status.json",
}

# Google-format providers — use /incidents.json (array of incident objects)
GOOGLE_STATUS_PROVIDERS = {
    "gcp":      "https://status.cloud.google.com/incidents.json",
    "firebase": "https://status.firebase.google.com/incidents.json",
}

STATUS_MAP = {
    "none":     "operational",
    "minor":    "degraded",
    "major":    "partial_outage",
    "critical": "major_outage",
}

# Google incidents severity_impact → our status enum
GOOGLE_IMPACT_MAP = {
    "SERVICE_INFORMATION":  "operational",
    "SERVICE_DISRUPTION":   "partial_outage",
    "SERVICE_UNAVAILABLE":  "major_outage",
}

API_LOG_FILTERS = {
    "openai":    'textPayload =~ "openai" AND (textPayload =~ "RateLimitError|APIError|Timeout")',
    "cohere":    'textPayload =~ "cohere" AND (textPayload =~ "429|TooManyRequestsError")',
    "replicate": 'textPayload =~ "replicate" AND severity>="ERROR"',
    "anthropic": 'textPayload =~ "anthropic" AND severity>="ERROR"',
}


# ─── DB helpers ──────────────────────────────────────────────────────────────

def pg_connect():
    return psycopg2.connect(PG_CONN)


def ch_connect():
    return clickhouse_connect.get_client(
        host=CH_HOST, user=CH_USER, password=CH_PASS,
        port=8443, secure=True
    )


# ─── Cloud Monitoring helpers ────────────────────────────────────────────────

def _metric_interval():
    interval = monitoring_v3.TimeInterval()
    interval.start_time = WINDOW_START
    interval.end_time = NOW
    return interval


def _aggregation(aligner, reducer, period_seconds=3600, group_by=None):
    return monitoring_v3.Aggregation(
        alignment_period={"seconds": period_seconds},
        per_series_aligner=aligner,
        cross_series_reducer=reducer,
        group_by_fields=group_by or ["resource.label.service_name"],
    )


def _list_series(client, project_name, filter_str, aggregation):
    """Wrapper that uses ListTimeSeriesRequest — correct for google-cloud-monitoring 2.x."""
    request = ListTimeSeriesRequest(
        name=project_name,
        filter=filter_str,
        interval=_metric_interval(),
        view=ListTimeSeriesRequest.TimeSeriesView.FULL,
        aggregation=aggregation,
    )
    return list(client.list_time_series(request=request))


def fetch_run_service_metrics(client, service_name, region):
    """Returns dict of metrics for a Cloud Run service."""
    project_name = f"projects/{PROJECT}"
    rf = (
        f'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service_name}" '
        f'AND resource.labels.location="{region}"'
    )
    result = {
        "request_count": None, "error_count": None,
        "p50_latency_ms": None, "p95_latency_ms": None, "p99_latency_ms": None,
        "instance_count": None,
    }

    # Request count — DELTA metric: use ALIGN_DELTA to get raw integer counts per period
    # Empty series = no traffic in window → 0, not NULL. NULL only on API failure.
    try:
        series = _list_series(
            client, project_name,
            f'{rf} AND metric.type="run.googleapis.com/request_count"',
            _aggregation(
                monitoring_v3.Aggregation.Aligner.ALIGN_DELTA,
                monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
            ),
        )
        result["request_count"] = sum(p.value.int64_value for s in series for p in s.points)

        # 5xx errors — same DELTA approach
        err_series = _list_series(
            client, project_name,
            f'{rf} AND metric.type="run.googleapis.com/request_count" '
            f'AND metric.labels.response_code_class="5xx"',
            _aggregation(
                monitoring_v3.Aggregation.Aligner.ALIGN_DELTA,
                monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
            ),
        )
        result["error_count"] = sum(p.value.int64_value for s in err_series for p in s.points)
    except Exception as e:
        log.warning("request_count fetch failed for %s: %s", service_name, e)

    # Latency percentiles
    PERCENTILE_ALIGNERS = {
        50: monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_50,
        95: monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
        99: monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
    }
    for pct, key in [(50, "p50_latency_ms"), (95, "p95_latency_ms"), (99, "p99_latency_ms")]:
        try:
            lat_series = _list_series(
                client, project_name,
                f'{rf} AND metric.type="run.googleapis.com/request_latencies"',
                monitoring_v3.Aggregation(
                    alignment_period={"seconds": 3600},
                    per_series_aligner=PERCENTILE_ALIGNERS[pct],
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
                ),
            )
            if lat_series and lat_series[0].points:
                result[key] = round(lat_series[0].points[0].value.double_value, 2)
        except Exception as e:
            log.warning("latency p%d fetch failed for %s: %s", pct, service_name, e)

    # Instance count
    try:
        inst_series = _list_series(
            client, project_name,
            f'{rf} AND metric.type="run.googleapis.com/container/instance_count"',
            _aggregation(
                monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
                monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
            ),
        )
        if inst_series and inst_series[0].points:
            # ALIGN_MEAN converts int64 gauge to double
            v = inst_series[0].points[0].value
            result["instance_count"] = int(v.double_value or v.int64_value)
        else:
            result["instance_count"] = 0  # no series = scaled to zero
    except Exception as e:
        log.warning("instance_count fetch failed for %s: %s", service_name, e)

    if result["request_count"] is not None:
        rc = result["request_count"]
        ec = result["error_count"] or 0
        result["error_rate_pct"] = round(ec / rc * 100, 3) if rc > 0 else 0.0

    return result


def fetch_run_job_metrics(executions_client, job_name, region):
    """Returns dict of metrics for a Cloud Run job — last completed execution
    via the Cloud Run Admin API. Deterministic regardless of when the job last ran
    (the old Cloud Monitoring window approach returned NULLs for any job that
    didn't complete within the past hour)."""
    result = {
        "job_exit_code": None, "job_duration_ms": None, "job_status": None,
        "job_last_execution_at": None,
    }

    try:
        parent = f"projects/{PROJECT}/locations/{region}/jobs/{job_name}"
        # API returns executions newest-first; only need the most recent completed one.
        # Empty result (0 executions) here usually means job_name/region is wrong —
        # log so mis-registered jobs (see registered_services) surface instead of
        # silently staying blank forever.
        found_any = False
        for execution in executions_client.list_executions(parent=parent):
            found_any = True
            if not execution.completion_time:
                continue  # still running
            failed = execution.failed_count or 0
            result["job_status"] = "failed" if failed > 0 else "succeeded"
            result["job_exit_code"] = 1 if failed > 0 else 0
            # Actual time the job finished running — distinct from collected_at
            # (when Control Tower scraped this), which can be much more recent
            # than the job's own last run for infrequently-scheduled jobs.
            result["job_last_execution_at"] = execution.completion_time
            start = execution.start_time or execution.create_time
            if start and execution.completion_time:
                duration = execution.completion_time - start
                result["job_duration_ms"] = int(duration.total_seconds() * 1000)
            break
        if not found_any:
            log.warning(
                "No executions found for job %s in %s — check registered_services.region "
                "matches where the job is actually deployed.", job_name, region,
            )
    except Exception as e:
        log.warning("job metrics fetch failed for %s: %s", job_name, e)

    return result


# ─── Status page polling ─────────────────────────────────────────────────────

def _poll_statuspage_io(provider, url):
    """Poll a statuspage.io /api/v2/status.json endpoint."""
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    indicator = data.get("status", {}).get("indicator", "unknown")
    overall = STATUS_MAP.get(indicator, "unknown")
    components = []
    if overall != "operational":
        try:
            comp_url = url.replace("/status.json", "/components.json")
            cr = requests.get(comp_url, timeout=10)
            cr.raise_for_status()
            comps = cr.json().get("components", [])
            components = [c["name"] for c in comps if c.get("status") != "operational"]
        except Exception:
            pass
    return overall, components


def _poll_google_incidents(provider, url):
    """Poll a Google-format /incidents.json endpoint."""
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    incidents = r.json()
    # Active incidents have end=None
    active = [i for i in incidents if i.get("end") is None]
    if not active:
        return "operational", []
    # Worst impact wins
    impacts = [i.get("most_recent_update", {}).get("status", "SERVICE_DISRUPTION") for i in active]
    worst = "SERVICE_UNAVAILABLE" if "SERVICE_UNAVAILABLE" in impacts else "SERVICE_DISRUPTION"
    overall = GOOGLE_IMPACT_MAP.get(worst, "partial_outage")
    affected = list({
        p["title"]
        for i in active
        for p in i.get("affected_products", [])
    })
    return overall, affected


def poll_status_pages(pg_cur):
    log.info("Polling status pages...")
    rows = []

    all_providers = list(STATUSPAGE_PROVIDERS.items()) + list(GOOGLE_STATUS_PROVIDERS.items())
    for provider, url in all_providers:
        try:
            if provider in GOOGLE_STATUS_PROVIDERS:
                overall, components = _poll_google_incidents(provider, url)
            else:
                overall, components = _poll_statuspage_io(provider, url)
            rows.append((provider, url, overall, json.dumps(components), NOW))
            log.info("Status %s: %s", provider, overall)
        except Exception as e:
            log.warning("Status page poll failed for %s: %s", provider, e)
            rows.append((provider, url, "unknown", json.dumps([]), NOW))

    if rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.provider_status
               (provider, status_page_url, overall_status, affected_components, polled_at)
               VALUES %s""",
            rows,
        )
        log.info("Wrote %d provider_status rows", len(rows))


# ─── Auto-discovery ──────────────────────────────────────────────────────────

MONITORED_REGIONS = ["asia-south1", "us-central1", "us-west1"]


def auto_discover(pg_cur):
    log.info("Auto-discovering Cloud Run services and jobs...")
    run_client = run_v2.ServicesClient()
    jobs_client = run_v2.JobsClient()

    # Fetch known services from DB
    pg_cur.execute("SELECT service_name FROM control_tower.registered_services")
    known = {r[0] for r in pg_cur.fetchall()}

    new_rows = []
    notifications = []

    for region in MONITORED_REGIONS:
        parent = f"projects/{PROJECT}/locations/{region}"

        # Services — auto-activated: newly deployed services should show up in
        # Control Tower immediately, not sit invisibly until someone flips a flag.
        try:
            for svc in run_client.list_services(parent=parent):
                name = svc.name.split("/")[-1]
                if name not in known and not name.startswith("ct-"):
                    new_rows.append((name, "cloud_run_service", region, "gcp", True))
                    notifications.append(f"service:{name} ({region})")
        except Exception as e:
            log.warning("Service discovery failed for %s: %s", region, e)

        # Jobs
        try:
            for job in jobs_client.list_jobs(parent=parent):
                name = job.name.split("/")[-1]
                if name not in known and not name.startswith("ct-"):
                    new_rows.append((name, "cloud_run_job", region, "gcp", True))
                    notifications.append(f"job:{name} ({region})")
        except Exception as e:
            log.warning("Job discovery failed for %s: %s", region, e)

    if new_rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.registered_services
               (service_name, platform, region, cloud, active)
               VALUES %s ON CONFLICT (service_name) DO NOTHING""",
            new_rows,
        )
        log.info("Discovered %d new services/jobs: %s", len(new_rows), notifications)


# ─── Cloud Scheduler jobs (HTTP-direct targets only) ──────────────────────────
#
# Schedulers that call a Cloud Run Job's :run endpoint are already covered —
# their execution status shows up via job_last_execution_at in service_health.
# This covers the other kind: schedulers that hit a plain HTTP endpoint on a
# Cloud Run *service* (cron logic living inside the service's own code, e.g.
# agenteye's /api/cron/* routes). Those have no Cloud Run Job execution to
# inspect — the scheduler's own last-attempt status is the only signal.

_RUN_JOB_TARGET = re.compile(r"run\.googleapis\.com.*?/jobs/[^/:]+:run$")


def collect_scheduler_status(pg_cur):
    log.info("Checking Cloud Scheduler jobs (HTTP-direct targets)...")
    client = scheduler_v1.CloudSchedulerClient()
    rows = []

    for region in MONITORED_REGIONS:
        parent = f"projects/{PROJECT}/locations/{region}"
        try:
            for job in client.list_jobs(parent=parent):
                uri = job.http_target.uri if job.http_target else ""
                if not uri or _RUN_JOB_TARGET.search(uri):
                    continue  # targets a Cloud Run Job — already covered elsewhere
                name = job.name.split("/")[-1]
                last_attempt = job.last_attempt_time if job.last_attempt_time else None
                if last_attempt is None:
                    status = "never run"
                elif job.status.code == 0:
                    status = "ok"
                else:
                    status = f"error (code {job.status.code})"
                rows.append((name, job.schedule, uri, region, True, last_attempt, status, NOW))
        except Exception as e:
            log.warning("Scheduler job listing failed for %s: %s", region, e)

    if rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.scheduler_jobs
               (scheduler_name, schedule, target_uri, region, active,
                last_attempt_at, last_attempt_status, checked_at)
               VALUES %s
               ON CONFLICT (scheduler_name) DO UPDATE SET
                   schedule = EXCLUDED.schedule,
                   last_attempt_at = EXCLUDED.last_attempt_at,
                   last_attempt_status = EXCLUDED.last_attempt_status,
                   checked_at = EXCLUDED.checked_at""",
            rows,
        )
        log.info("Upserted %d HTTP-direct scheduler jobs", len(rows))


# ─── Data freshness checker ──────────────────────────────────────────────────

def auto_discover_watched_tables(pg_cur, ch_client):
    """Register every PeerDB-mirrored ClickHouse table (has _peerdb_synced_at)
    for freshness monitoring. Skips raw-mirror staging tables, backups, and
    empty tables. Existing rows keep their cadence (ON CONFLICT DO NOTHING),
    so manually tuned tables are never overwritten."""
    log.info("Auto-discovering ClickHouse tables for freshness monitoring...")
    try:
        result = ch_client.query("""
            SELECT c.database, c.table
            FROM system.columns c
            INNER JOIN system.tables t ON t.database = c.database AND t.name = c.table
            WHERE c.name = '_peerdb_synced_at'
              AND c.table NOT LIKE '\\_peerdb\\_raw\\_mirror%'
              AND c.table NOT LIKE '%\\_backup\\_2%'
              AND t.total_rows > 0
            ORDER BY c.database, c.table
        """)
    except Exception as e:
        log.warning("ClickHouse auto-discovery query failed: %s", e)
        return

    # 26h default cadence: most PeerDB syncs are daily; stale kicks in at 3x (78h).
    rows = [(db, table, "_peerdb_synced_at", 1560, True, True)
            for db, table in result.result_rows]
    if not rows:
        log.info("No PeerDB-mirrored tables found.")
        return

    execute_values(
        pg_cur,
        """INSERT INTO control_tower.watched_tables
           (database_name, table_name, freshness_column, expected_cadence_minutes,
            active, auto_discovered)
           VALUES %s
           ON CONFLICT (database_name, table_name) DO NOTHING""",
        rows,
    )
    log.info("Auto-discovery: %d PeerDB-mirrored tables registered/confirmed.", len(rows))


FRESHNESS_BATCH_SIZE = 40  # tables per UNION ALL query — 328 watched tables in ~9 queries


def _freshness_status(last_write, cadence):
    if last_write is None or last_write.year <= 1970:  # CH max() on empty set = epoch
        return "dead", None
    last_write = last_write.replace(tzinfo=datetime.timezone.utc)
    age_minutes = (NOW - last_write).total_seconds() / 60
    if age_minutes <= cadence:
        return "fresh", last_write
    elif age_minutes <= cadence * 3:
        return "stale", last_write
    return "dead", last_write


def _effective_cadence(configured, active_days_21d, auto_discovered):
    """Adaptive cadence for auto-discovered tables: weekly syncs (hector, shopflo,
    nykaa run Mondays) must not be flagged dead on a 26h default. Manually
    configured tables always keep their cadence."""
    if not auto_discovered:
        return configured
    if active_days_21d >= 10:          # syncs ~daily
        return configured
    if active_days_21d >= 2:           # periodic (weekly-ish)
        return max(configured, 10080)  # 7 days → stale after 21d
    return configured                  # inactive — dead is dead


def check_data_freshness(pg_cur, ch_client):
    log.info("Checking data freshness...")
    pg_cur.execute(
        "SELECT database_name, table_name, freshness_column, expected_cadence_minutes, "
        "       auto_discovered "
        "FROM control_tower.watched_tables WHERE active = true"
    )
    tables = pg_cur.fetchall()
    if not tables:
        log.info("No watched tables configured yet.")
        return

    rows = []
    for i in range(0, len(tables), FRESHNESS_BATCH_SIZE):
        batch = tables[i:i + FRESHNESS_BATCH_SIZE]
        meta = {f"{db}.{table}": (cadence, auto) for db, table, _, cadence, auto in batch}
        union_sql = " UNION ALL ".join(
            f"SELECT '{db}.{table}' AS k, toDateTime64(max(`{col}`), 3) AS mx, count() AS cnt, "
            f"uniqExactIf(toDate(`{col}`), `{col}` >= now() - INTERVAL 21 DAY) AS days21 "
            f"FROM `{db}`.`{table}`"
            for db, table, col, _, _ in batch
        )
        try:
            result = ch_client.query(union_sql)
            for key, last_write, row_count, days21 in result.result_rows:
                db, table = key.split(".", 1)
                configured, auto = meta[key]
                cadence = _effective_cadence(configured, days21, auto)
                status, last_write = _freshness_status(last_write, cadence)
                rows.append((db, table, last_write, cadence, row_count, None, status, NOW))
        except Exception as e:
            log.warning("Freshness batch query failed (%d tables), falling back per-table: %s",
                        len(batch), e)
            for db, table, col, configured, auto in batch:
                try:
                    result = ch_client.query(
                        f"SELECT toDateTime64(max(`{col}`), 3), count(), "
                        f"uniqExactIf(toDate(`{col}`), `{col}` >= now() - INTERVAL 21 DAY) "
                        f"FROM `{db}`.`{table}`"
                    )
                    last_write, row_count, days21 = result.result_rows[0]
                    cadence = _effective_cadence(configured, days21, auto)
                    status, last_write = _freshness_status(last_write, cadence)
                    rows.append((db, table, last_write, cadence, row_count, None, status, NOW))
                except Exception as e2:
                    log.warning("Freshness check failed for %s.%s: %s", db, table, e2)
                    rows.append((db, table, None, configured, None, None, "dead", NOW))

    if rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.data_freshness
               (database_name, table_name, last_write_at, expected_cadence_minutes,
                row_count_snapshot, row_count_delta, freshness_status, checked_at)
               VALUES %s""",
            rows,
        )
        log.info("Wrote %d data_freshness rows", len(rows))


# ─── Third-party API health from Cloud Logging ───────────────────────────────

def collect_api_health(pg_cur):
    log.info("Querying Cloud Logging for API health signals...")
    log_client = gcp_logging.Client(project=PROJECT)
    rows = []

    window_start_str = WINDOW_START.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    for provider, base_filter in API_LOG_FILTERS.items():
        full_filter = (
            f'{base_filter} '
            f'AND timestamp>="{window_start_str}" '
            f'AND timestamp<="{window_end_str}"'
        )
        try:
            entries = list(log_client.list_entries(
                filter_=full_filter,
                max_results=500,
                order_by=gcp_logging.DESCENDING,
            ))
            error_count = len(entries)
            rows.append((
                provider, None, WINDOW_START, NOW,
                None, error_count,
                None, None,  # rate and latency calculated below
                "cloud_logging", NOW,
            ))
        except Exception as e:
            log.warning("Cloud Logging query failed for %s: %s", provider, e)

    if rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.third_party_api_health
               (provider, operation, window_start, window_end, call_count_estimate,
                error_count, error_rate_pct, avg_latency_ms_estimate, source, collected_at)
               VALUES %s""",
            rows,
        )
        log.info("Wrote %d third_party_api_health rows", len(rows))


# ─── Pipeline processing metrics from Cloud Logging ──────────────────────────
#
# Services opt in by emitting a single log line:
#   import json, logging
#   logging.getLogger(__name__).info(
#       "CT_METRICS: " + json.dumps({
#           "records_processed": 1234,   # total attempted
#           "records_success":   1230,   # successfully written/indexed
#           "records_failed":    4,      # errors / skipped
#           "step":              "main", # optional step name
#           "duration_ms":       45000,  # optional wall-clock ms
#       })
#   )
#
# The GCP collector queries for these lines every hour per Cloud Run job.
# Results land in control_tower.pipeline_events.

def collect_pipeline_metrics(pg_cur, services):
    log.info("Collecting pipeline processing metrics from Cloud Logging...")
    log_client = gcp_logging.Client(project=PROJECT)

    job_names = [s["service_name"] for s in services if s["platform"] == "cloud_run_job"]
    if not job_names:
        log.info("No Cloud Run jobs registered, skipping pipeline metrics.")
        return

    window_start_str = WINDOW_START.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Single query covering all jobs — OR is more efficient than N round trips
    job_filter = " OR ".join(f'resource.labels.job_name="{j}"' for j in job_names)
    filter_str = (
        f'resource.type="cloud_run_job" '
        f'AND ({job_filter}) '
        f'AND textPayload =~ "CT_METRICS:" '
        f'AND timestamp>="{window_start_str}" '
        f'AND timestamp<="{window_end_str}"'
    )

    try:
        entries = list(log_client.list_entries(
            filter_=filter_str,
            max_results=500,
            order_by=gcp_logging.DESCENDING,
        ))
    except Exception as e:
        log.warning("Pipeline metrics Cloud Logging query failed: %s", e)
        return

    if not entries:
        log.info("No CT_METRICS lines found in this window.")
        return

    # Avoid re-inserting rows for executions already recorded
    execution_ids = [
        (e.labels or {}).get("run.googleapis.com/execution_name", "")
        for e in entries
    ]
    execution_ids = [eid for eid in execution_ids if eid]
    seen = set()
    if execution_ids:
        pg_cur.execute(
            "SELECT execution_id FROM control_tower.pipeline_events "
            "WHERE execution_id = ANY(%s)",
            (execution_ids,),
        )
        seen = {r[0] for r in pg_cur.fetchall()}

    rows = []
    for entry in entries:
        labels = entry.labels or {}
        execution_id = labels.get("run.googleapis.com/execution_name", "") or None
        if execution_id and execution_id in seen:
            continue

        job_name = (entry.resource.labels or {}).get("job_name", "")
        payload = str(entry.payload)
        ct_idx = payload.find("CT_METRICS:")
        if ct_idx == -1:
            continue
        try:
            metrics = json.loads(payload[ct_idx + len("CT_METRICS:"):].strip())
        except json.JSONDecodeError:
            continue

        records_processed = metrics.get("records_processed") or metrics.get("records_success")
        records_failed = metrics.get("records_failed", 0)
        extra = {k: v for k, v in metrics.items()
                 if k not in ("step", "records_processed", "records_success",
                              "records_failed", "duration_ms")}
        rows.append((
            job_name,
            metrics.get("step", "main"),
            execution_id,
            "run_complete",
            "error" if records_failed else "ok",
            records_processed,
            records_failed or None,
            None,           # rows_expected_min
            metrics.get("duration_ms"),
            None,           # error_message
            json.dumps(extra) if extra else None,
            entry.timestamp,
        ))

    if rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.pipeline_events
               (pipeline_id, step_name, execution_id, event_type, status,
                rows_written, rows_failed, rows_expected_min, duration_ms,
                error_message, metadata_json, occurred_at)
               VALUES %s""",
            rows,
        )
        log.info("Wrote %d pipeline_events rows", len(rows))


# ─── Main collection loop ────────────────────────────────────────────────────

def collect_service_health(pg_cur, services):
    monitoring_client = monitoring_v3.MetricServiceClient()
    executions_client = run_v2.ExecutionsClient()
    rows = []

    for svc in services:
        service_name = svc["service_name"]
        platform = svc["platform"]
        region = svc["region"]
        log.info("Collecting %s (%s, %s)", service_name, platform, region)

        if platform == "cloud_run_service":
            metrics = fetch_run_service_metrics(monitoring_client, service_name, region)
        elif platform == "cloud_run_job":
            metrics = fetch_run_job_metrics(executions_client, service_name, region)
        else:
            continue

        rows.append((
            service_name, platform, region, NOW,
            metrics.get("request_count"),
            metrics.get("error_count"),
            metrics.get("error_rate_pct"),
            metrics.get("p50_latency_ms"),
            metrics.get("p95_latency_ms"),
            metrics.get("p99_latency_ms"),
            metrics.get("instance_count"),
            metrics.get("job_exit_code"),
            metrics.get("job_duration_ms"),
            metrics.get("job_status"),
            metrics.get("job_last_execution_at"),
            None, None, None, None, None,  # cpu/mem/disk/ec2 fields (GCP-only)
        ))

    if rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.service_health (
                service_name, platform, region, collected_at,
                request_count, error_count, error_rate_pct,
                p50_latency_ms, p95_latency_ms, p99_latency_ms,
                instance_count, job_exit_code, job_duration_ms, job_status,
                job_last_execution_at,
                cpu_utilization_pct, memory_utilization_pct, disk_utilization_pct,
                ec2_instance_id, ec2_instance_state
            ) VALUES %s
            ON CONFLICT (service_name, collected_at) DO UPDATE SET
                request_count = EXCLUDED.request_count,
                error_count = EXCLUDED.error_count,
                error_rate_pct = EXCLUDED.error_rate_pct,
                p50_latency_ms = EXCLUDED.p50_latency_ms,
                p95_latency_ms = EXCLUDED.p95_latency_ms,
                p99_latency_ms = EXCLUDED.p99_latency_ms,
                instance_count = EXCLUDED.instance_count,
                job_exit_code = EXCLUDED.job_exit_code,
                job_status = EXCLUDED.job_status,
                job_last_execution_at = EXCLUDED.job_last_execution_at""",
            rows,
        )
        log.info("Upserted %d service_health rows", len(rows))


def main():
    log.info("GCP Collector starting. Window: %s → %s", WINDOW_START, NOW)

    pg = pg_connect()
    ch = ch_connect()

    try:
        with pg.cursor() as cur:
            # 1. Read active GCP services
            cur.execute(
                "SELECT service_name, platform, region FROM control_tower.registered_services "
                "WHERE active = true AND cloud = 'gcp'"
            )
            services = [
                {"service_name": r[0], "platform": r[1], "region": r[2]}
                for r in cur.fetchall()
            ]
            log.info("Monitoring %d active GCP services/jobs", len(services))

            # 2. Collect service health metrics
            collect_service_health(cur, services)

            # 3. Poll status pages
            poll_status_pages(cur)

            # 4. Auto-discover new services
            auto_discover(cur)

            # 4b. Cloud Scheduler jobs with HTTP-direct targets (cron logic inside services)
            collect_scheduler_status(cur)

            # 5. Data freshness: auto-discover PeerDB tables, then check all watched
            auto_discover_watched_tables(cur, ch)
            check_data_freshness(cur, ch)

            # 6. API health from Cloud Logging
            collect_api_health(cur)

            # 7. Pipeline processing metrics (CT_METRICS convention)
            collect_pipeline_metrics(cur, services)

            pg.commit()
            log.info("All data committed to PostgreSQL.")

    except Exception as e:
        pg.rollback()
        log.error("Collector failed: %s", e)
        raise
    finally:
        pg.close()
        ch.close()

    # 7. Ping healthchecks.io (only on success — must be last)
    if HEALTHCHECK_URL:
        try:
            requests.get(HEALTHCHECK_URL, timeout=10)
            log.info("Healthcheck ping sent.")
        except Exception as e:
            log.warning("Healthcheck ping failed: %s", e)

    log.info("GCP Collector complete.")


if __name__ == "__main__":
    main()
