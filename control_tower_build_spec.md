# Control Tower — Build Specification

**Version:** 1.2  
**Scope:** All Cloud Run services, Cloud Run jobs, EC2 instances, ClickHouse pipelines, third-party API integrations  
**Stack:** Cloud SQL PostgreSQL (operational store), ClickHouse Cloud (analytics replica), Google Cloud Run, Cloud Monitoring, AWS EC2/CloudWatch

---

## 1. Overview and Goals

The control tower is an internal observability layer that sits across all deployed services and data pipelines. It is not a replacement for GCP or AWS native tooling — those remain the source of truth for raw logs and real-time debugging. The control tower aggregates derived signals into PostgreSQL for operational use, syncs to ClickHouse via PeerDB for analytics, and surfaces unified alerting across the entire stack.

**What GCP/AWS already handles (no duplication needed):**
- Raw log storage and ad-hoc log search (Logs Explorer, CloudWatch Logs)
- Real-time metric dashboards per service (Cloud Monitoring, CloudWatch)
- Infrastructure-level alerting (uptime checks, CPU threshold alerts)

**What the control tower adds:**
- Long-term metric history beyond GCP's 6-week retention, stored in PostgreSQL with no sampling
- Cross-pipeline correlation in ClickHouse — joining service health against business data
- Third-party API health tracking via Cloud Logging queries (no wrapper required yet)
- Data freshness validation — not just "did the job run" but "did the job produce output"
- Unified alerting across GCP and AWS surfaces with pipeline-aware context
- AI-assisted triage via the Anthropic API when critical alerts fire
- Cost monitoring with spend spike detection and weekly digest
- Dead man's switch via healthchecks.io to detect when the monitor itself is down

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                           SOURCES                                │
│  Cloud Run Services │ Cloud Run Jobs │ EC2 │ ClickPipes          │
│  MySQL (RDS) │ PostgreSQL │ Firebase │ External APIs (no wrapper)│
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                         COLLECTION                               │
│  GCP Collector        (Cloud Run job, hourly)                    │
│  AWS Collector        (Cloud Run job, hourly)                    │
│  API Health Poller    (Cloud Logging query, hourly)              │
│  Status Page Poller   (Cloud Run job, hourly)                    │
│  Cost Collector       (Cloud Run job, daily 06:00 IST)           │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                    POSTGRESQL STORE                               │
│  control_tower schema — operational truth                        │
│  service_health │ pipeline_events │ third_party_api_health       │
│  data_freshness │ provider_status │ cost_metrics │ alerts        │
│  registered_services │ watched_tables │ cost_budgets             │
└────────┬─────────────────────────────────────────────────────────┘
         │  PeerDB CDC (continuous)
┌────────▼─────────────────────────────────────────────────────────┐
│                    CLICKHOUSE REPLICA                             │
│  control_tower database — analytics and business correlation     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                       INTELLIGENCE                               │
│  Anomaly Detector     (Cloud Run job, hourly)                    │
│  Freshness Checker    (part of GCP collector, hourly)            │
│  Pipeline Validator   (triggered post-job)                       │
│  Cost Anomaly Check   (part of cost collector, daily)            │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                         SURFACE                                  │
│  Alerter              (Cloud Run job, hourly) → Slack            │
│  Weekly cost digest   (Cloud Run job, Monday 09:00 IST) → Slack  │
│  Dead man's switch    (healthchecks.io — external)              │
│  Control Tower UI     (React, Cloud Run)                         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Store

### 3.1 PostgreSQL — operational store

All control tower tables live in a dedicated `control_tower` schema on the existing Cloud SQL PostgreSQL instance. Standard tables with proper primary keys and update patterns — no workarounds needed.

The schema contains nine tables:

**`registered_services`** — config table listing every service and job the collector should monitor. The collector reads this at startup — adding a new service requires only inserting a row here, no redeployment.

Columns: `service_name`, `platform` (cloud_run_service / cloud_run_job / ec2), `region`, `cloud` (gcp / aws), `pipeline_id` (optional — links service to a named pipeline), `active`, `created_at`.

**`watched_tables`** — config table listing every ClickHouse table the freshness checker should verify. The freshness column and expected write cadence are stored per table.

Columns: `database_name`, `table_name`, `freshness_column`, `expected_cadence_minutes`, `active`.

**`service_health`** — one row per service per collection cycle. Covers Cloud Run services, Cloud Run jobs, and EC2 instances.

Columns: `service_name`, `platform`, `region`, `collected_at`, `request_count`, `error_count`, `error_rate_pct`, `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms`, `instance_count`, `job_exit_code`, `job_duration_ms`, `job_status`, `cpu_utilization_pct`, `memory_utilization_pct`, `disk_utilization_pct`, `ec2_instance_id`, `ec2_instance_state`.

Unique constraint on `(service_name, collected_at)`. Collector uses `INSERT ... ON CONFLICT DO UPDATE`.

**`pipeline_events`** — output-level tracking for each pipeline step. Not whether the job ran, but whether it produced data.

Columns: `pipeline_id`, `step_name`, `event_type` (run_start / run_complete / run_failed / output_validated), `status` (ok / warn / error), `rows_written`, `rows_expected_min`, `duration_ms`, `error_message`, `occurred_at`.

**`third_party_api_health`** — hourly aggregated health snapshot per provider, derived from Cloud Logging. Not individual call events (no wrapper yet) — aggregated error counts and latency estimates from log entries.

Columns: `provider`, `operation`, `window_start`, `window_end`, `call_count_estimate`, `error_count`, `error_rate_pct`, `avg_latency_ms_estimate`, `source` (cloud_logging / wrapper — for future migration), `collected_at`.

**`data_freshness`** — last write timestamp and freshness status per watched ClickHouse table.

Columns: `database_name`, `table_name`, `last_write_at`, `expected_cadence_minutes`, `row_count_snapshot`, `row_count_delta`, `freshness_status` (fresh / stale / dead), `checked_at`.

**`provider_status`** — output of the status page poller.

Columns: `provider`, `status_page_url`, `overall_status` (operational / degraded / partial_outage / major_outage / unknown), `affected_components`, `polled_at`.

**`cost_metrics`** — daily cost snapshot per source and resource.

Columns: `cost_source`, `resource_name`, `period_start`, `period_end`, `cost_usd`, `units_consumed`, `unit_type`, `cost_per_unit`, `monthly_budget_usd`, `budget_pct_consumed`.

**`alerts`** — written by the anomaly detector, read by the alerter.

Columns: `alert_id` (UUID), `alert_type`, `severity` (info / warn / critical), `service_name`, `pipeline_id`, `provider`, `message`, `context_json`, `fired_at`, `acknowledged_at`, `ack_by`.

**`cost_budgets`** — config table for monthly spend limits per source.

Columns: `cost_source`, `resource_name` (use `*` as catch-all), `monthly_budget_usd`, `warn_threshold_pct` (default 70), `critical_threshold_pct` (default 90), `active`.

### 3.2 ClickHouse — analytics replica

PostgreSQL syncs to a `control_tower` database in ClickHouse Cloud via PeerDB CDC. All tables mirror the PostgreSQL schema with the standard `ReplacingMergeTree` pattern and `_peerdb_is_deleted` / `_peerdb_version` columns added automatically by PeerDB.

All queries against the ClickHouse replica must apply the standard deduplication filter:

```sql
HAVING argMax(_peerdb_is_deleted, _peerdb_version) = 0
```

The ClickHouse replica is used exclusively for analytical queries — joining control tower signals against business data in `holistique_default_database`, `recruitment_hr`, and `Performance_Marketing_*` tables. Operational writes (alert acknowledgement, config edits) always go to PostgreSQL directly.

PeerDB CDC lag of a few minutes is acceptable — there is no real-time analytical requirement on control tower data.

---

## 4. Collection Layer

### 4.1 GCP Collector (hourly)

A Cloud Run job triggered by Cloud Scheduler every hour. Responsible for: reading `registered_services` for active GCP services, pulling metrics from the Cloud Monitoring API for each, running the freshness checker against `watched_tables`, and writing results to PostgreSQL.

On successful completion, the collector pings the healthchecks.io heartbeat URL. If the ping is not received within the configured grace period (2 hours), healthchecks.io sends a Slack and email alert. This is the dead man's switch — it fires when Postgres is down, when the collector crashes, or when Cloud Scheduler stops invoking the job.

Required GCP IAM roles: `roles/monitoring.viewer`, `roles/logging.viewer`.

Key Cloud Monitoring metric paths for Cloud Run:

- Request count: `run.googleapis.com/request_count`
- Request latencies: `run.googleapis.com/request_latencies`
- Instance count: `run.googleapis.com/container/instance_count`
- Job completion: `run.googleapis.com/job/completed_execution_count`

The collection window is the past 65 minutes (1 hour + 5 min buffer) to ensure no gap between cycles.

### 4.2 AWS Collector (hourly)

A Cloud Run job (preferred) or EC2 cron that queries CloudWatch for all active EC2 instances registered in `registered_services`. AWS credentials are stored in GCP Secret Manager.

EC2 instances live entirely within AWS. Cloud Run has no visibility into them — Cloud Monitoring and Cloud Logging are GCP-only and receive nothing from EC2. All signals from EC2 come from CloudWatch (metrics) and CloudWatch Logs (application logs), not from GCP tooling.

Required AWS IAM actions: `cloudwatch:GetMetricData`, `cloudwatch:GetMetricStatistics`, `cloudwatch:FilterLogEvents`, `ec2:DescribeInstances`, `ec2:DescribeInstanceStatus`.

**Metrics — CloudWatch (native, no agent needed for CPU):**
- CPU: `AWS/EC2 CPUUtilization` (available by default on all instances)
- Memory: `CWAgent mem_used_percent` (requires CloudWatch Agent)
- Disk: `CWAgent disk_used_percent` (requires CloudWatch Agent)

CloudWatch Agent must be installed on all monitored EC2 instances before this collector is deployed. Without it, only CPU is available. Memory and disk — the most useful pressure signals — are invisible.

**Application logs — CloudWatch Logs:**
EC2 application logs do not flow to Cloud Logging. If the CloudWatch Agent is configured to ship application logs to CloudWatch Logs (a separate configuration from the metrics agent), the AWS collector can query CloudWatch Logs for error patterns using the `FilterLogEvents` API — the exact equivalent of what the GCP collector does against Cloud Logging for GCP services.

If the CloudWatch Agent is not shipping logs, this signal is unavailable entirely. In that case the minimum viable approach is covered below.

**EC2 cron job completion — direct PostgreSQL write:**
For EC2-hosted cron jobs, the job script writes a pipeline event row directly to PostgreSQL at the end of each run, including exit code, duration, and any error message. This is the minimum viable instrumentation and requires no CloudWatch involvement — it works regardless of whether the CloudWatch Agent is installed. The Postgres connection string is stored in the instance's environment or AWS Secrets Manager.

This direct write approach is preferred over shipping logs to CloudWatch Logs and querying them, because it produces the same structured row the rest of the control tower expects, with no intermediate log parsing step.

**API calls made from EC2 — additional blind spot:**
Any third-party API calls (OpenAI, Replicate, etc.) made by EC2-hosted processes are invisible to both Cloud Logging and the GCP API health poller. Cloud Logging only receives logs from GCP services. Until wrappers are added to EC2 services, the only coverage for EC2 API errors is explicit error logging within the job script itself, captured either via CloudWatch Logs or written directly to the `pipeline_events` table as part of the job's error handling.

### 4.3 Third-Party API Health Poller (hourly) — current approach, no wrapper

Since no wrapper exists on API calls today, the control tower derives third-party API health from logs rather than from instrumented call events. The approach differs by platform — Cloud Logging for GCP-hosted services, CloudWatch Logs for EC2-hosted services.

**GCP services (Cloud Run) — Cloud Logging:**
The GCP collector queries Cloud Logging for log entries in the past hour that match known error patterns for each provider. Cloud Logging automatically captures all stdout and stderr from Cloud Run containers, so unhandled exceptions and SDK error messages surface here without any instrumentation.

Cloud Logging filters used per provider (GCP services only):
- OpenAI: `textPayload =~ "openai" AND (textPayload =~ "RateLimitError|APIError|Timeout")`
- Cohere: `textPayload =~ "cohere" AND (textPayload =~ "429|TooManyRequestsError")`
- Replicate: `textPayload =~ "replicate" AND severity>=ERROR`
- Anthropic: `textPayload =~ "anthropic" AND severity>=ERROR`

What Cloud Logging surfaces without a wrapper: HTTP 429 errors from SDK exception messages, unhandled exceptions with provider names in the stack trace, timeout errors from Replicate jobs that exceed the Cloud Run request timeout, and any error explicitly logged to stdout before re-raising.

**EC2 services — CloudWatch Logs:**
Cloud Logging has zero visibility into EC2. For EC2-hosted services that make third-party API calls, the equivalent query runs against CloudWatch Logs using the same error pattern filters, via the `FilterLogEvents` API. This requires the CloudWatch Agent to be configured to ship application logs — not just metrics — to a CloudWatch Log Group.

If CloudWatch Logs are not available for a given EC2 service, API call errors from that service are invisible to the control tower until wrappers are added. This is a known gap. The workaround is for the EC2 job script to explicitly catch and write API errors to the `pipeline_events` table as part of its own error handling.

**Shared limitations across both platforms:**
This approach misses successful-but-slow calls entirely and only catches errors that surface in logs. Error counts are derived from text pattern matching, not structured call records. Call volume estimates are approximate. It is an interim solution until wrappers are added.

**Migration path to wrapper-based collection:** when API call wrappers are introduced (see section 13), Cloud Run services emit structured `ct_event=api_call` JSON to stdout on every call, routed via a Cloud Logging sink to Pub/Sub. EC2 services follow a different path — see section 13 for details. The `source` column in `third_party_api_health` will read `wrapper` instead of `cloud_logging` or `cloudwatch_logs` so historical data from all approaches coexists cleanly.

### 4.4 Status Page Poller (hourly)

Checks the public status APIs of all critical third-party providers and writes to `provider_status`. Runs as a step inside the GCP collector job, not a separate deployment.

Providers monitored: OpenAI, Anthropic, Replicate, Cloudflare, Cohere, Firebase, GCP.

All status pages expose a standard `/api/v2/status.json` endpoint returning an `indicator` field (`none` / `minor` / `major` / `critical`) which maps directly to the `overall_status` enum.

### 4.5 Cost Collector (daily, 06:00 IST)

A separate Cloud Run job triggered by Cloud Scheduler once per day. Cost APIs have a 24-hour lag — running more frequently returns the same data. Collects from: GCP Billing BigQuery export, AWS Cost Explorer API, ClickHouse Cloud Organisation API, OpenAI Usage API, Anthropic Usage API, and Replicate Billing API. Cohere costs are tracked via monthly invoice only (no daily API).

After writing cost rows, the collector runs the cost anomaly checks and writes any triggered alerts to PostgreSQL.

**GCP Billing setup prerequisite:** enable the Cloud Billing export to BigQuery before deploying the cost collector. This provides hourly-granularity cost breakdowns by service and SKU. The collector queries BigQuery directly for yesterday's costs per Cloud Run service.

---

## 5. Intelligence Layer

### 5.1 Anomaly Detector (hourly)

A Cloud Run job triggered by Cloud Scheduler every hour, offset 30 minutes from the main collector (e.g. collector at :00, anomaly detector at :30) to ensure the latest collection cycle has landed before checks run.

Reads from the ClickHouse replica for all anomaly queries. Writes alert rows to PostgreSQL.

Five checks run on every cycle:

**Check 1 — Error rate spike:** compares the 2-hour rolling average error rate per service against the 7-day baseline. Fires if error rate exceeds 5% absolute AND exceeds 2x the baseline.

**Check 2 — Job duration drift:** compares the p95 job duration over the past 4 hours against the 14-day baseline p95. Fires if p95 is more than 50% slower than baseline and the absolute duration exceeds 60 seconds.

**Check 3 — Idle sink:** reads `data_freshness` for any table whose `freshness_status` is `stale` or `dead` in the latest check cycle.

**Check 4 — Third-party API degradation:** reads `third_party_api_health` for the latest hour. Fires if error rate exceeds 10% or if avg latency estimate exceeds 2x the 7-day baseline. Note: until wrappers are added, this check is based on error counts from Cloud Logging and should be treated as a minimum-confidence signal.

**Check 5 — EC2 resource pressure:** fires if average CPU exceeds 85%, memory exceeds 90%, or disk exceeds 85% over the past 2 hours.

Before writing a new alert, the detector checks if an identical `(alert_type, service_name)` alert already exists with `acknowledged_at IS NULL` and `fired_at > now() - 4 hours`. If so, it skips writing a duplicate — alerts do not re-fire until the previous instance is acknowledged or ages out.

### 5.2 Data Freshness Checker (hourly, part of GCP collector)

Runs as the final step of the GCP collector job. Reads all active rows from `watched_tables`, queries `max(freshness_column)` on each ClickHouse table, and writes a freshness row to PostgreSQL. Freshness status:

- **Fresh:** last write within the expected cadence
- **Stale:** last write between 1x and 3x the expected cadence
- **Dead:** last write more than 3x the expected cadence ago

### 5.3 Pipeline Output Validator (triggered post-job)

Runs at the end of specific critical jobs as a final validation step. Writes a `pipeline_events` row with `event_type = output_validated` and `status = ok` or `error`.

Two validators are defined at launch:

**RecruitBot vector index validator:** after the nightly OPTIMIZE run on `recruitment_hr.applicant_vectors`, asserts that the indexed vector count is within 5% of the total applicant count. A gap larger than 5% indicates an unmerged HNSW index issue.

**Meta ad performance validator:** after the daily Meta sync, asserts that at least one row exists in `Performance_Marketing_meta_insights` for today's date. A zero count indicates the sync silently produced no output.

Additional validators are added when new critical jobs are onboarded (see section 8).

### 5.4 Cost Anomaly Detection (daily, part of cost collector)

Three checks run as part of the daily cost collector pass:

**Daily spend spike:** flags any source where yesterday's spend exceeds 2x the 30-day daily average, with an absolute floor of $5 to suppress noise on low-spend items.

**Monthly budget burn rate:** projects end-of-month spend based on the month-to-date daily average. Fires a warning at 70% projected budget consumption and critical at 90%.

**Token efficiency drift:** computes cost-per-successful-API-call per provider per day. Fires if this metric exceeds 1.5x the 7-day baseline. Catches prompt bloat and runaway re-embedding loops.

---

## 6. Alerter

A Cloud Run job triggered by Cloud Scheduler every hour, offset 45 minutes from the main collector (e.g. collector at :00, alerter at :45). Reads unacknowledged alerts from PostgreSQL fired in the past 2 hours and posts to Slack.

For each alert, the alerter: checks `provider_status` for the relevant provider to see if an external outage explains the alert, optionally calls the AI triage layer for critical severity alerts, posts a Slack Block Kit message, then marks the alert as acknowledged in PostgreSQL.

The Slack message includes: severity emoji, alert type, human-readable message, metric values and baseline for context, provider status if relevant, and AI triage summary if requested.

Alert acknowledgement is a real PostgreSQL UPDATE — no deduplication workaround needed.

---

## 7. AI Triage Layer

When a critical alert fires, the alerter calls the Anthropic API (claude-sonnet-4-6) with the alert context, recent metric history from the past 24 hours, and the current provider status for the relevant provider. The model returns a 2–3 sentence root cause hypothesis and the single most likely remediation step. This is appended to the Slack message.

The AI triage call is best-effort — if it fails or times out, the Slack alert is still sent without it. The triage result is not stored.

---

## 8. Onboarding a New Service or Job

This section defines the standard workflow for adding any new Cloud Run service, Cloud Run job, or EC2 process to control tower monitoring. The goal is to make onboarding a 10-minute config task with no code changes and no redeployment.

### 8.1 Automated discovery (Cloud Run)

Cloud Run services and jobs can be discovered automatically. The GCP collector, on each run, queries the Cloud Run Admin API for all deployed services and jobs in the project and compares the result against `registered_services`. Any service or job not present in the registry is inserted automatically with `active = false` and a Slack notification is sent to the ops channel:

```
🆕 New Cloud Run service discovered: `tfs-image-generator` (asia-south1)
   Not yet active in control tower. Insert into registered_services with active=true to begin monitoring.
```

This means any deployment to Cloud Run is visible in the control tower within one collection cycle, even if nobody remembers to register it. The ops team reviews and activates it rather than having to remember to add it.

For EC2 instances, auto-discovery queries the AWS EC2 API for all running instances in the monitored region and applies the same pattern.

### 8.2 Manual activation

Once auto-discovery has inserted the row (or for manual registration), activate monitoring by updating the row in `registered_services`:

```sql
-- Activate monitoring for a discovered service
UPDATE control_tower.registered_services
SET active = true,
    pipeline_id = 'tfs-image-pipeline'  -- optional: link to a named pipeline
WHERE service_name = 'tfs-image-generator';
```

That is the only step required for standard health monitoring (error rate, latency, instance count). The collector picks it up on its next run.

### 8.3 Optional: add data freshness monitoring

If the service writes to a ClickHouse table that should be verified, add a row to `watched_tables`:

```sql
INSERT INTO control_tower.watched_tables
    (database_name, table_name, freshness_column, expected_cadence_minutes)
VALUES
    ('holistique_default_database', 'tfs_product_images', 'created_at', 120);
```

The freshness checker picks this up on its next run. No deployment needed.

### 8.4 Optional: add a pipeline output validator

If the job has a meaningful output that should be validated beyond row count — for example, asserting that generated images are non-null, or that a score distribution falls within expected bounds — add a validator query to the pipeline validator configuration. This is the one step that requires a code change in the pipeline validator service. The validator is a SQL query that returns a single row with a `validation_status` column of `ok` or `error`.

### 8.5 Optional: add to cost budgets

If the new service involves third-party API spend (e.g. a new Replicate model), add a budget row:

```sql
INSERT INTO control_tower.cost_budgets
    (cost_source, resource_name, monthly_budget_usd, warn_threshold_pct, critical_threshold_pct)
VALUES
    ('replicate', 'tfs-flux-lora-v2', 80, 70, 90);
```

### 8.6 EC2 cron jobs — additional step

EC2 cron jobs are not visible to the Cloud Run Admin API. Auto-discovery covers the EC2 instance itself but not individual cron processes running on it. For a new EC2 cron job:

1. Manually insert a row into `registered_services` with `platform = ec2_job`
2. Add two lines to the end of the cron script: one to write a `pipeline_events` completion row to PostgreSQL, one to write the same to ClickHouse via HTTP if direct Postgres access is unavailable from that instance

This is the only onboarding step that requires touching the job script itself.

### 8.7 Summary checklist

| Step | Required | Automated |
|------|----------|-----------|
| Service appears in `registered_services` | Yes | Yes — auto-discovery |
| Set `active = true` | Yes | No — manual approval |
| Add `watched_tables` entry | Only if ClickHouse writes | No — manual |
| Add pipeline output validator | Only if output validation needed | No — requires code |
| Add `cost_budgets` entry | Only if new API spend | No — manual |
| Add pipeline event writes (EC2 cron only) | Yes for EC2 cron | No — edit script |

---

## 9. Dead Man's Switch (healthchecks.io)

The control tower cannot detect its own failure using only internal mechanisms — if PostgreSQL is down, the collector cannot write, the anomaly detector cannot query, and no alerts fire. The dead man's switch is an external heartbeat that detects exactly this scenario.

**Setup:**

1. Create a free account at healthchecks.io
2. Create one check per collector job (GCP collector, AWS collector, cost collector)
3. Set the check period to 1 hour and grace period to 2 hours for the main collectors; 25 hours and 2 hours for the cost collector
4. Configure the check to send alerts to the ops Slack channel and email via the healthchecks.io integration
5. Store the ping URL for each check in GCP Secret Manager
6. The collector pings the URL as the very last step of a successful run — if the run fails before reaching the ping, no ping is sent and healthchecks.io alerts after the grace period

**What it detects:**

- Cloud SQL instance down or unreachable
- Cloud Run collector job failing to deploy or start
- Cloud Scheduler stopping job invocations
- GCP regional outage affecting both Cloud SQL and Cloud Run
- Collector code exception before the ping line

**What it does not detect:** slow degradation where the collector runs but produces incorrect data. That is the job of the anomaly detector and data freshness checker — which are themselves covered by the heartbeat.

**Cost:** healthchecks.io free tier supports 20 checks, which covers the entire control tower with room to spare.

---

## 10. Database and External Service Monitoring

### 10.1 MySQL and PostgreSQL

Track at the connection and replication layer only — not individual query performance.

| Metric | Alert threshold |
|--------|----------------|
| Database connections | > 80% of `max_connections` |
| Replication lag (read replicas feeding ClickPipes) | > 60 seconds |
| Slow query count | > 10 queries over 5s in a collection window |
| Free storage | < 20% remaining |

Replication lag is the critical metric for CDC pipelines. If the RDS read replica feeding ClickPipes lags, downstream ClickHouse tables are stale even though the ClickPipe itself reports healthy.

### 10.2 Firebase

Monitor quota utilization only. Runs as part of the hourly GCP collector pass.

Metrics tracked via Firebase Management API: Firestore read/write/delete operations vs daily quota, Firebase Storage total size, Authentication daily active users vs quota.

Alert threshold: warn when any Firebase quota exceeds 70% of the daily limit.

### 10.3 ClickHouse Cloud

Track only data-layer signals — ClickHouse Cloud's own dashboard covers infrastructure health.

Two ClickHouse system table queries run as part of the GCP collector:

**ClickPipe pending parts:** counts active parts modified in the last hour per table. A high and growing count means ingestion is running but merges are not keeping up.

**Unmerged parts count:** tables with more than 300 active parts should be flagged. High part counts degrade HNSW ANN search quality and query performance. The nightly OPTIMIZE job should be extended to cover all tables with active ClickPipe ingestion, not just `applicant_vectors`.

---

## 11. Cost Monitoring

### 11.1 Data sources

| Source | API | Granularity |
|--------|-----|-------------|
| GCP (Cloud Run, Logging, Firebase) | BigQuery billing export | Daily |
| AWS (EC2, RDS) | AWS Cost Explorer API | Daily (1-day lag) |
| ClickHouse Cloud | Cloud API `/v1/organizations/{id}/usages` | Daily |
| OpenAI | Usage API `/v1/usage` | Daily |
| Anthropic | Usage API `/v1/usage` | Daily |
| Replicate | Billing API `/v1/billing` | Daily |
| Cohere | Monthly invoice only | Monthly |

GCP Billing export to BigQuery must be enabled before the cost collector is deployed. This is a one-time console action and provides far richer cost breakdown (by service, SKU, and label) than the Billing API alone.

### 11.2 Budget configuration

Monthly budgets are stored in `cost_budgets`. Initial values should be estimated conservatively and tuned after the first two weeks of actuals. Use `resource_name = *` as a catch-all for a provider where per-resource budgets are not needed.

Example budget entries:

| Source | Resource | Monthly budget |
|--------|----------|---------------|
| openai | text-embedding-3-large | $100 |
| openai | * | $200 |
| replicate | * | $150 |
| clickhouse_cloud | * | $300 |
| gcp_cloud_run | * | $80 |
| aws_ec2 | * | $120 |
| anthropic | * | $50 |

### 11.3 Cost anomaly detection

Three checks run daily as part of the cost collector:

**Daily spend spike:** fires if any source's spend yesterday exceeds 2x its 30-day daily average, with a $5 floor to suppress noise.

**Monthly budget burn rate projection:** projects end-of-month spend based on month-to-date daily average. Warn at 70% projected, critical at 90% projected.

**Token efficiency drift:** computes cost-per-successful-call per provider per day and fires if it exceeds 1.5x the 7-day baseline. Catches prompt bloat and accidental re-embedding loops.

### 11.4 Weekly cost digest

The alerter sends a weekly Slack summary every Monday at 09:00 IST to a dedicated `#cost-monitoring` channel. The digest shows this week vs last week spend per provider with percentage change, highlights any sources above 70% of monthly budget, and includes projected end-of-month totals.

---

## 12. Build Phases and Timeline

### Phase 1 — Foundation (Days 1–3)

- Create `control_tower` schema and all tables in Cloud SQL PostgreSQL
- Populate `cost_budgets` with initial estimates
- Deploy GCP collector Cloud Run job with Cloud Scheduler trigger (hourly, :00)
- Set up healthchecks.io checks and store ping URLs in Secret Manager
- Verify `service_health` rows appearing in PostgreSQL

### Phase 2 — AWS and EC2 (Days 4–5)

- Deploy AWS collector as a Cloud Run job with credentials from Secret Manager
- Install CloudWatch Agent on EC2 instances that do not have it
- Add pipeline event writes to existing EC2 cron job scripts
- Verify EC2 rows appearing in `service_health`

### Phase 3 — PeerDB Sync to ClickHouse (Day 6)

- Create `control_tower` database in ClickHouse Cloud
- Configure PeerDB CDC pipe from Cloud SQL `control_tower` schema to ClickHouse
- Verify tables replicating with `_peerdb_is_deleted` and `_peerdb_version` columns
- Confirm deduplication filter works on replicated data

### Phase 4 — Freshness, Validation, Cost (Days 7–9)

- Populate `watched_tables` with all critical ClickHouse tables
- Add pipeline output validators for RecruitBot and Meta ad sync
- Enable GCP Billing export to BigQuery if not already active
- Deploy cost collector (daily at 06:00 IST)
- Tune initial budget thresholds after first week of actuals

### Phase 5 — Intelligence and Alerting (Days 10–12)

- Deploy anomaly detector (hourly, :30)
- Deploy alerter (hourly, :45) with Slack webhook
- Configure weekly cost digest (Monday 09:00 IST)
- Test end-to-end: trigger a known failure, verify alert fires within 2 hours and heartbeat detects the monitor going silent

### Phase 6 — AI Triage and Dashboard (Days 13–18)

- Add AI triage call for critical alerts
- Build control tower React UI (Cloud Run) with:
  - Pipeline health map — all registered services as status nodes
  - 24-hour error rate sparkline per service
  - Data freshness table
  - Third-party API health table (with caveat on Cloud Logging accuracy)
  - Cost trend chart (this week vs last week per provider)
  - Alert history with one-click acknowledgement

---

## 13. Future: API Call Wrapper Migration

The current approach derives third-party API health from log pattern matching — Cloud Logging for GCP services, CloudWatch Logs for EC2 services. This is an interim solution with known limitations: it cannot measure latency on successful calls, it misses errors that are caught and swallowed silently, and call count estimates are approximate. The wrapper approach resolves all of these, but the migration path differs between Cloud Run and EC2.

**Cloud Run services:**
Each service adds a thin decorator around every third-party API call. The decorator measures wall-clock latency, captures the HTTP status code, token usage, and error type, and writes a structured JSON line to stdout on every call — success or failure. Cloud Logging automatically captures stdout and a log sink routes entries with `ct_event=api_call` to a Pub/Sub topic. The GCP collector reads from Pub/Sub and writes individual call records to a new `third_party_api_events` table. No infrastructure changes are needed — only changes to individual service code.

**EC2 services:**
stdout from EC2 processes does not flow to Cloud Logging. Two options for EC2 wrapper output:

Option A — CloudWatch Logs: if the CloudWatch Agent is already shipping application logs, the wrapper writes structured JSON to stdout the same way Cloud Run services do. The AWS collector reads matching entries from CloudWatch Logs via `FilterLogEvents` and writes to `third_party_api_events`. This is architecturally consistent with the Cloud Run approach.

Option B — direct PostgreSQL write: the wrapper writes the call record directly to the `third_party_api_events` table in PostgreSQL via the connection string already available in the EC2 environment. Simpler and requires no CloudWatch Agent configuration, but adds a database write on every API call. Acceptable for low-volume EC2 jobs; may add latency for high-frequency callers.

**Migration order and backwards compatibility:**
The migration can be done service by service — starting with the highest-volume Cloud Run caller (RecruitBot embedding pipeline) and working outward, then tackling EC2 services. The `source` column in `third_party_api_health` distinguishes `cloud_logging`, `cloudwatch_logs`, and `wrapper` rows so historical data from all approaches coexists. The anomaly detector switches to using `third_party_api_events` when `wrapper` rows are available for a given provider and service, falling back to `third_party_api_health` for services not yet wrapped.

---

## 14. Key Design Decisions

**PostgreSQL as the operational store, not ClickHouse:** control tower data is small (a few hundred rows per hour), has genuine update patterns (alert acknowledgement, config edits, budget updates), and does not require the aggregation capabilities ClickHouse is designed for. PostgreSQL handles all of this natively without any workarounds. ClickHouse is used only for analytics, where its strengths — fast aggregations, joins against large business tables — are actually needed.

**Sync to ClickHouse via PeerDB:** the same CDC pattern already used for MySQL and Postgres operational databases. Gives the control tower data the same analytics capabilities as all other business data, with no additional infrastructure. CDC lag of a few minutes is acceptable.

**Cloud Logging for API health (interim):** adding wrappers to every service before the control tower is built adds scope and risk to the rollout. Cloud Logging already captures enough signal to detect clear failures and provider outages. Wrappers are a Phase 2 improvement, not a prerequisite.

**Hourly collection:** this is not a safety-critical or real-time system. Hourly collection means a failure is detected within 1–2 hours — acceptable with no uptime SLA. It dramatically reduces Cloud Scheduler invocation cost, Cloud Monitoring API call volume, and PostgreSQL write frequency compared to 2-minute collection.

**Auto-discovery with manual activation:** automatically discovering new services prevents the common failure mode where a deployment happens and nobody remembers to register it. Manual activation prevents untested or transient services from generating noise before the team is ready to monitor them.

**Dead man's switch via healthchecks.io:** the monitor cannot monitor itself. An external service that alerts on silence rather than failure is the cleanest solution. Two lines added to the collector — one ping URL fetch — and the entire self-monitoring problem is solved without any additional infrastructure.

**Daily cost collection:** cost APIs have a 24-hour lag by design. Running the collector more frequently returns the same data and wastes API quota.

---

## 15. Alerting Thresholds Reference

### Health alerts

| Alert type | Condition | Severity |
|-----------|-----------|----------|
| Error rate spike | `error_rate > 5%` AND `> 2x 7-day baseline` | Critical |
| Job failure | `exit_code != 0` on any registered job | Critical |
| Job duration drift | `p95 > 1.5x 14-day baseline` AND `> 60s` | Warn |
| Data freshness — stale | Table not written in `> 1x expected cadence` | Warn |
| Data freshness — dead | Table not written in `> 3x expected cadence` | Critical |
| API error rate | `> 10%` over last 2 hours | Warn |
| API latency spike | `> 2x baseline` over last 2 hours (wrapper only) | Warn |
| Provider outage | Status page shows `partial_outage` or worse | Info |
| Idle ClickPipe sink | Pipe active but table not receiving writes | Critical |
| EC2 CPU pressure | `avg CPU > 85%` over 2 hours | Warn |
| EC2 memory pressure | `avg memory > 90%` over 2 hours | Critical |
| EC2 disk pressure | `avg disk > 85%` | Warn |
| RDS replication lag | `> 60 seconds` | Warn |
| Firebase quota | `> 70%` of daily quota | Warn |
| Unmerged ClickHouse parts | `> 300 active parts` on any watched table | Warn |
| Collector heartbeat missed | healthchecks.io grace period exceeded | Critical (external) |

### Cost alerts

| Alert type | Condition | Severity |
|-----------|-----------|----------|
| Daily spend spike | Yesterday `> 2x` 30-day average | Warn |
| Budget overrun risk | Projected monthly `> 90%` of budget | Critical |
| Budget warning | Projected monthly `> 70%` of budget | Warn |
| Token cost drift | Cost-per-call `> 1.5x` 7-day baseline | Warn |
| Zero spend anomaly | Source with usual spend shows `$0` for 2+ days | Info |
