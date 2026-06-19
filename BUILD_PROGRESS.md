# Control Tower — Build Progress

**Spec version:** 1.2  
**Build started:** 2026-06-18  
**Last updated:** 2026-06-18

---

## Status Legend
- `[ ]` Not started
- `[~]` In progress
- `[x]` Done
- `[!]` Blocked / needs action

---

## Prerequisites Checklist

| Item | Status | Notes |
|------|--------|-------|
| GCP project access confirmed | `[x]` | Project: `seoai-479305`, region: `asia-south1` (Cloud Run) |
| Cloud SQL PostgreSQL instance | `[x]` | Instance: `agenteye-pg` (PG 16, `us-central1-b`). **Dedicated DB: `control_tower`** created. Socket: `seoai-479305:us-central1:agenteye-pg`. |
| ClickHouse Cloud credentials | `[x]` | Host: `htnicbsqm0.ap-south-1.aws.clickhouse.cloud`, user: `default`, pw: `7kF7z.TS3gBj2` |
| AWS RDS MySQL host | `[x]` | `holistique-middleware.c9wdjmzy25ra.ap-south-1.rds.amazonaws.com` — MySQL, not the PG store |
| AWS access keys (CloudWatch / Cost Explorer) | `[!]` | Not found in any .env — need IAM key pair |
| Alerting method | `[x]` | **Email via MS Graph** (no Slack). Tenant: `2cc477df-31d6-4498-bbeb-0f68fa05821f`, client: `72692f8b-6524-4ff3-8bda-8a4ad232f793` |
| Anthropic API key | `[!]` | Not found in any .env — need key |
| Replicate API token | `[!]` | Not found in any .env — need token |
| ClickHouse Cloud organisation API key | `[!]` | Not found — needed for cost collector |
| healthchecks.io account | `[ ]` | Free tier, needs signup |
| GCP Billing export to BigQuery | `[x]` | Dataset `seoai-479305.billing_export` created. Table appears ~24h after export enabled. |
| PeerDB instance | `[x]` | Already running (confirmed via existing ClickHouse schema with `_peerdb_*` columns) |
| OpenAI API key | `[x]` | Found in multiple .env files |
| Cohere API key | `[x]` | `AOiayYEeZKuedHVwM7ll3qpStFHpq6BfskL9PXck` |
| Firebase project | `[x]` | Project: `skinanalyzer-bd4da` |

---

## Phase 1 — Foundation (Days 1–3)

**Goal:** PostgreSQL schema live, GCP collector running, heartbeat pinging.

| Task | Status | Notes |
|------|--------|-------|
| Create `control_tower` schema in Cloud SQL | `[x]` | Dedicated DB `control_tower` created in `agenteye-pg` |
| Create table: `registered_services` | `[x]` | |
| Create table: `watched_tables` | `[x]` | |
| Create table: `service_health` | `[x]` | Unique constraint on `(service_name, collected_at)` |
| Create table: `pipeline_events` | `[x]` | |
| Create table: `third_party_api_health` | `[x]` | |
| Create table: `data_freshness` | `[x]` | |
| Create table: `provider_status` | `[x]` | |
| Create table: `cost_metrics` | `[x]` | |
| Create table: `alerts` | `[x]` | UUID primary key |
| Create table: `cost_budgets` | `[x]` | |
| Seed `cost_budgets` with initial estimates | `[x]` | 7 rows seeded |
| Seed `registered_services` with known GCP services | `[x]` | 35 rows seeded via migration 002 |
| Build GCP collector (Cloud Run job) | `[x]` | `collectors/gcp_collector/` |
| GCP collector: Cloud Run service metrics | `[x]` | Cloud Monitoring metrics wired |
| GCP collector: Cloud Run job metrics | `[x]` | `run.googleapis.com/job/completed_execution_count` |
| GCP collector: status page poller step | `[x]` | 7 providers — OpenAI/Anthropic/Cloudflare degraded confirmed live |
| GCP collector: healthchecks.io ping (last step) | `[~]` | Placeholder secret — update after healthchecks.io signup |
| GCP collector: auto-discovery step | `[x]` | 6 extra services auto-discovered on first run (41 total) |
| Deploy GCP collector to Cloud Run | `[x]` | `ct-gcp-collector` in `asia-south1` |
| Create Cloud Scheduler trigger (hourly :00) | `[x]` | `trigger-ct-gcp-collector` |
| Create healthchecks.io checks (GCP + AWS + cost) | `[ ]` | **TODO: sign up at healthchecks.io and update 3 secrets** |
| Store healthchecks.io ping URLs in Secret Manager | `[~]` | Placeholders created — overwrite after signup |
| Verify `service_health` rows in PostgreSQL | `[x]` | ✅ 35 rows written on first manual run |

---

## Phase 2 — AWS and EC2 (Days 4–5)

**Goal:** EC2 metrics in `service_health`, cron jobs writing `pipeline_events`.

| Task | Status | Notes |
|------|--------|-------|
| Confirm CloudWatch Agent installed on all EC2s | `[!]` | Without it: CPU only, no memory/disk |
| Store AWS credentials in GCP Secret Manager | `[~]` | Placeholders — update `ct-aws-access-key-id` + `ct-aws-secret-access-key` |
| Build AWS collector (Cloud Run job) | `[x]` | `collectors/aws_collector/` |
| AWS collector: EC2 auto-discovery step | `[x]` | Queries EC2 API by Name tag |
| Deploy AWS collector to Cloud Run | `[x]` | `ct-aws-collector` deployed |
| Create Cloud Scheduler trigger (hourly :00, same as GCP) | `[x]` | `trigger-ct-aws-collector` |
| Add `pipeline_events` write to each EC2 cron script | `[!]` | Needs list of EC2 cron jobs from user |
| Verify EC2 rows in `service_health` | `[ ]` | After AWS keys provided |
| Verify EC2 job rows in `pipeline_events` | `[ ]` | After EC2 cron scripts updated |

---

## Phase 3 — PeerDB Sync to ClickHouse (Day 6)

**Goal:** All 9 control tower tables replicating to ClickHouse `control_tower` database.

| Task | Status | Notes |
|------|--------|-------|
| Create `control_tower` database in ClickHouse Cloud | `[~]` | Script: `scripts/setup_clickhouse.py` |
| Configure PeerDB CDC: PostgreSQL `control_tower` schema → ClickHouse | `[!]` | Follow `docs/peerdb_setup_guide.md` — needs PeerDB UI access |
| Verify `_peerdb_is_deleted` and `_peerdb_version` columns present | `[ ]` | Standard PeerDB pattern |
| Verify deduplication filter works on sample query | `[ ]` | Anomaly detector checks ClickHouse on startup |

---

## Phase 4 — Freshness, Validation, Cost (Days 7–9)

**Goal:** Freshness monitoring live, cost data flowing, budgets set.

| Task | Status | Notes |
|------|--------|-------|
| Add freshness checker step to GCP collector | `[x]` | Reads `watched_tables`, writes `data_freshness`. Fixed PeerDB HAVING clause (migration 005). |
| Populate `watched_tables` with critical ClickHouse tables | `[x]` | 6 tables seeded (Finance, Holistique, recruitment_hr) — verified via `system.tables` |
| Enable GCP Billing export to BigQuery | `[!]` | One-time console action if not done |
| Build cost collector (Cloud Run job) | `[x]` | `collectors/cost_collector/` — GCP BQ, AWS CE, ClickHouse, OpenAI, Anthropic |
| Deploy cost collector to Cloud Run | `[x]` | `ct-cost-collector` deployed |
| Create Cloud Scheduler trigger (daily 06:00 IST) | `[x]` | `trigger-ct-cost-collector` |
| Add RecruitBot vector index validator | `[ ]` | Can add freshness alert threshold to watched_tables once PeerDB live |
| Add Meta ad performance validator | `[x]` | `Performance_Marketing_meta_insight` in watched_tables, confirmed **fresh** today |
| Tune budget thresholds after first week of actuals | `[ ]` | Schedule for Day 14 |

---

## Phase 5 — Intelligence and Alerting (Days 10–12)

**Goal:** Anomaly detector and alerter deployed, end-to-end alert verified.

| Task | Status | Notes |
|------|--------|-------|
| Build anomaly detector (Cloud Run job) | `[x]` | `intelligence/anomaly_detector/` |
| Anomaly detector: Check 1 — error rate spike | `[x]` | 2h avg vs 7d baseline, >5% AND >2x |
| Anomaly detector: Check 2 — job duration drift | `[x]` | p95 4h vs 14d baseline, >50% slower AND >60s |
| Anomaly detector: Check 3 — idle sink (data freshness) | `[x]` | Reads `data_freshness` stale/dead rows |
| Anomaly detector: Check 4 — third-party API degradation | `[x]` | >10% error rate or >2x latency baseline |
| Anomaly detector: Check 5 — EC2 resource pressure | `[x]` | CPU>85%, mem>90%, disk>85% over 2h |
| Anomaly detector: dedup check before write | `[x]` | 4h dedup window |
| Deploy anomaly detector to Cloud Run | `[x]` | `ct-anomaly-detector` deployed |
| Create Cloud Scheduler trigger (hourly :30) | `[x]` | `trigger-ct-anomaly-detector` |
| Build alerter (Cloud Run job) | `[x]` | `intelligence/alerter/` — MS Graph email + AI triage |
| Alerter: provider status cross-check | `[x]` | Checks `provider_status` before sending |
| Alerter: email format (HTML) | `[x]` | Severity colour, context table, AI triage block |
| Alerter: alert acknowledgement write | `[x]` | UPDATE `acknowledged_at` in PostgreSQL |
| Alerter: weekly cost digest (Monday 09:00 IST) | `[x]` | `trigger-ct-alerter-weekly` scheduler trigger |
| Deploy alerter to Cloud Run | `[x]` | `ct-alerter` deployed |
| Create Cloud Scheduler trigger (hourly :45) | `[x]` | `trigger-ct-alerter` |
| End-to-end test: trigger known failure → alert fires ≤2h | `[ ]` | Do this after PeerDB sync live (Phase 3) |

---

## Phase 6 — AI Triage and Dashboard (Days 13–18)

**Goal:** AI triage on critical alerts, React UI deployed.

| Task | Status | Notes |
|------|--------|-------|
| Add AI triage call to alerter (critical alerts only) | `[x]` | Anthropic `claude-sonnet-4-6`, 2–3 sentence triage in alert email |
| AI triage: best-effort (no failure on timeout) | `[x]` | try/except wraps AI call — alert sends regardless |
| Build control tower React UI | `[x]` | `ui/backend/` — FastAPI + Preact/htm (no npm build) |
| UI: service health table | `[x]` | request count, instances, error%, latency, job status |
| UI: third-party provider status cards | `[x]` | Live data confirmed (GCP/OpenAI/Anthropic/Cloudflare degraded shown) |
| UI: data freshness table | `[x]` | 6 tables live, freshness status + last_write_at + row count |
| UI: pipeline metrics table (CT_METRICS) | `[x]` | Shows per-job records_processed / failed + 24h history |
| UI: cost trend table (this week vs last) | `[x]` | By source with budget burn % bar |
| UI: alert history with acknowledge button | `[x]` | One-click → PostgreSQL UPDATE → page refresh |
| Deploy UI to Cloud Run | `[x]` | `ct-ui` at `https://ct-ui-254146383960.asia-south1.run.app` (IAM auth required) |

---

## Open Questions / Blockers

| # | Question | Owner | Status |
|---|----------|-------|--------|
| 1 | ~~Cloud SQL PG not found~~ — RESOLVED. `agenteye-pg` instance confirmed. | — | `[x]` Closed |
| 2 | AWS IAM access key + secret for CloudWatch and Cost Explorer — not in any env file | User | `[!]` Open |
| 3 | Anthropic API key — not found | User | `[!]` Open |
| 4 | Replicate API token — not found | User | `[!]` Open |
| 5 | ClickHouse Cloud organisation API key — needed for cost collector | User | `[!]` Open |
| 6 | Is CloudWatch Agent installed on EC2 instances? Which instances are monitored? | User | `[!]` Open |
| 7 | List of existing EC2 cron jobs that need `pipeline_events` writes added | User | `[!]` Open |
| 8 | Which `Performance_Marketing_*` tables need freshness monitoring? | User | `[!]` Open |
| 9 | Alert recipient email address(es) for MS Graph sender | User | `[!]` Open |
| 10 | GCP Billing BigQuery export — setup guide in section below | User → Claude | `[~]` In progress |

---

## Secret Manager Secrets (GCP project: `seoai-479305`)

| Secret name | Status | Value |
|-------------|--------|-------|
| `ct-pg-connection-string` | `[x]` | `postgresql://agenteye_app:***@/agenteye?host=/cloudsql/seoai-479305:us-central1:agenteye-pg` |
| `ct-clickhouse-host` | `[x]` | `htnicbsqm0.ap-south-1.aws.clickhouse.cloud` |
| `ct-clickhouse-user` | `[x]` | `default` |
| `ct-clickhouse-password` | `[x]` | `7kF7z.TS3gBj2` |
| `ct-aws-access-key-id` | `[!]` | Missing |
| `ct-aws-secret-access-key` | `[!]` | Missing |
| `ct-email-tenant-id` | `[x]` | `2cc477df-31d6-4498-bbeb-0f68fa05821f` |
| `ct-email-client-id` | `[x]` | `72692f8b-6524-4ff3-8bda-8a4ad232f793` |
| `ct-email-client-secret` | `[x]` | From assessment_service/.env MS_GRAPH_CLIENT_SECRET |
| `ct-healthchecks-gcp-ping-url` | `[ ]` | After healthchecks.io signup |
| `ct-healthchecks-aws-ping-url` | `[ ]` | After healthchecks.io signup |
| `ct-healthchecks-cost-ping-url` | `[ ]` | After healthchecks.io signup |
| `ct-openai-api-key` | `[x]` | Found in env files |
| `ct-anthropic-api-key` | `[x]` | Stored in Secret Manager |
| `ct-replicate-api-token` | `[-]` | Skipped (user: ignore for now) |
| `ct-clickhouse-cloud-api-key` | `[x]` | Stored in Secret Manager |
| `ct-clickhouse-cloud-api-secret` | `[x]` | Stored in Secret Manager |
| `ct-cohere-api-key` | `[x]` | Stored in Secret Manager |
| `ct-alert-email` | `[x]` | `rajaram.vennam@glidebrands.in` |
| `ct-bigquery-billing-dataset` | `[x]` | `seoai-479305.billing_export` |

---

## GCP Billing Export to BigQuery — Setup Guide

**One-time setup. Takes ~24h for first data to appear.**

### Step 1 — Enable the Cloud Billing API
1. Go to: [console.cloud.google.com/apis/library](https://console.cloud.google.com/apis/library)
2. Search "Cloud Billing API" → Enable (project: `seoai-479305`)

### Step 2 — Create a BigQuery dataset for billing
```
Project: seoai-479305
Dataset ID: billing_export
Location: asia-south1  (match your Cloud Run region)
```
1. Go to BigQuery → your project → Create dataset
2. Dataset ID: `billing_export`
3. Location: `asia-south1`
4. Leave all defaults

### Step 3 — Enable Billing Export
1. Go to: Billing → [your billing account] → Billing export
2. Click **"Standard usage cost"** tab → Edit settings
3. Project: `seoai-479305`
4. Dataset: `billing_export`
5. Save
6. Also enable **"Detailed usage cost"** (same dataset) — gives SKU-level breakdown

### Step 4 — The table that appears
After 24h GCP creates:
```
seoai-479305.billing_export.gcp_billing_export_v1_XXXXXX
```
The cost collector queries this table with:
```sql
SELECT service.description, sku.description, SUM(cost) as cost_usd
FROM `seoai-479305.billing_export.gcp_billing_export_v1_*`
WHERE DATE(usage_start_time) = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
GROUP BY 1, 2
```

### Step 5 — IAM for the cost collector service account
The Cloud Run cost collector job needs this BigQuery role on the billing dataset:
```
roles/bigquery.dataViewer  (on dataset billing_export)
roles/bigquery.jobUser     (on project seoai-479305)
```

### What to tell me when done
Share the exact table name that appears (it has a billing account ID suffix). Format: `gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX`

---

## Build Log

| Date | Update |
|------|--------|
| 2026-06-18 | Build spec v1.2 reviewed. BUILD_PROGRESS.md created. Credential scan completed — GCP project `seoai-479305` confirmed, ClickHouse creds found, PeerDB confirmed running, MS Graph email confirmed (no Slack). **Critical gap:** no Cloud SQL PostgreSQL instance found — only AWS RDS MySQL. Blocked on blocker #1 before Phase 1 can start. BigQuery billing export setup guide written. |
| 2026-06-18 | **Phase 1 database complete.** Dedicated `control_tower` DB created in `agenteye-pg`. All 10 tables + 5 indexes created. `cost_budgets` seeded (7 rows). 14 secrets stored in Secret Manager. Alert email: `rajaram.vennam@glidebrands.in`. BigQuery billing dataset confirmed. Next: build GCP collector Cloud Run job. |
| 2026-06-18 | **Phases 1, 4 (partial), 5 complete.** All 5 Cloud Run jobs built + deployed (`ct-gcp-collector`, `ct-aws-collector`, `ct-cost-collector`, `ct-anomaly-detector`, `ct-alerter`). All 6 Cloud Scheduler triggers live. GCP collector first run verified: 35 service_health rows + 7 provider_status rows written. 6 new services auto-discovered (41 total). Alerter uses MS Graph email. Service account `ct-collector` created with 8 IAM roles. **Remaining:** AWS keys, healthchecks.io signup, PeerDB sync (Phase 3), watched_tables seed (Phase 4). |
| 2026-06-18 | **Phase 1+2 debug complete.** Bug audit across all 5 Python files. Fixed: (1) Cloud Monitoring API 2.x `ListTimeSeriesRequest` wrapper, (2) DELTA metrics use `ALIGN_DELTA + int64_value`, (3) instance_count `ALIGN_MEAN` returns double → `double_value or int64_value`, (4) GCP/Firebase status pages use `/incidents.json` not statuspage.io format, (5) Cohere URL corrected, (6) `SENDER_EMAIL` default fixed to `hr@holistique.in`, (7) month calculation uses `calendar.monthrange`, (8) anomaly detector gracefully skips CH-dependent checks when ClickHouse `control_tower` tables not yet configured (Phase 3). All 4 affected images rebuilt and redeployed. Latest collector run confirmed: `request_count` populated for active services, `instance_count` now showing 1 for running services, 7 provider_status rows with real live outage data (GCP/Firebase partial_outage, OpenAI/Anthropic/Cloudflare degraded). **Next:** Phase 3 PeerDB CDC setup to unblock anomaly detector ClickHouse checks. |
| 2026-06-18 | **Phases 3–6 built and deployed (Phase 3 pending PeerDB UI config).** Added: (1) `collect_pipeline_metrics` step in GCP collector — queries Cloud Logging for `CT_METRICS:` JSON lines from Cloud Run jobs, writes to `pipeline_events` with records_processed/failed/duration; (2) migration 003 adds `rows_failed`, `metadata_json`, `execution_id` to `pipeline_events`; (3) migration 004/005 — seeded `watched_tables` with real ClickHouse table names verified from `system.tables` (Finance_Data, holistique_default_database, recruitment_hr); (4) freshness checker fixed — removed PeerDB HAVING clause (crashes on non-replicated tables), now works on all native ClickHouse tables; (5) 5 tables confirmed **fresh** (Finance invoices 9.1M, MW Orders 9.5M, Meta insights 224K, Meta insight region 5.5M, Web scrapers 4.4K); (6) `recruitment_hr.applicant_vectors` — **dead** (last ingest 2026-06-15, 3 days stale — real finding, RecruitBot ingest job likely failed); (7) Phase 3 scripts: `scripts/setup_clickhouse.py` + `docs/peerdb_setup_guide.md`; (8) Phase 6 UI: FastAPI backend + React/Preact/htm dashboard deployed to `https://ct-ui-254146383960.asia-south1.run.app` (authenticated — needs IAM access grant or `gcloud auth print-identity-token`). UI has 7 views: Overview, Services, Providers, Pipeline, Freshness, Alerts, Costs. **Next:** Phase 3 PeerDB config (follow `docs/peerdb_setup_guide.md`), then add CT_METRICS to RecruitBot/crawler jobs, then set up healthchecks.io. |
