# Control Tower — Build Progress

**Spec version:** 1.2  
**Build started:** 2026-06-18  
**Last updated:** 2026-07-13

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
| 2026-07-12 | **Full re-audit + redo after 24 days of silent failure.** Root cause: all 6 `trigger-ct-*` schedulers failed PERMISSION_DENIED since day 1 — `ct-collector` SA had `roles/run.viewer` (view-only), needed `roles/run.invoker`. No data collected since Jun 18; 4 of 5 jobs never executed once. Fixes deployed: (1) `bootstrap.sh` role list now includes `run.invoker` — **actual IAM grant pending user** (auto-mode blocks IAM changes); (2) job metrics rewritten via Cloud Run Admin API (`run_v2.ExecutionsClient`) — last execution status/duration always populated (was: NULL unless job completed within past hour); (3) NULL→0 for scaled-to-zero services (request/error/instance counts); (4) **freshness auto-discovery** — collector scans ClickHouse `system.columns` for `_peerdb_synced_at`, auto-registers 328 mirrored tables (migration 006 adds `auto_discovered` flag; manual rows keep cadence); (5) freshness checks batched via UNION ALL (9 queries not 328) with adaptive cadence — tables syncing <10 of last 21 days treated as weekly (7d cadence), so Monday-scheduled syncs (hector/shopflo/nykaa) no longer flag dead; (6) `idle_sink` alerts batched into ONE summary email (was: per-table flood), filtered to actionable tables (active within 30d or manually watched); (7) sender fixed to `ai@holistique.in` — tenant AppOnly AccessPolicy blocks `hr@` (June fix was backwards); SENDER_EMAIL now plain env var on ct-alerter. **E2E verified:** collector run → 329 freshness rows (172 fresh / 41 stale / 116 dead) + 32 service_health rows (28 with metrics) → anomaly detector wrote 1 summary alert → alerter sent email via MS Graph with AI triage → alert acked. **Real infra findings:** `recruitment_hr.applicant_vectors` dead since Jun 15 — `resume-ingest-job` fails daily: pymysql timeout to `holistique-middleware...rds.amazonaws.com` (AWS RDS security group likely blocks Cloud Run egress). **User actions:** (a) run the `run.invoker` grant command, (b) fix RDS access for resume-ingest-job, (c) Phase 3 PeerDB config, (d) healthchecks.io signup. |
| 2026-07-13 | **Schedulers live + cost pipeline fixed.** IAM `run.invoker` granted (user-approved) — all hourly triggers verified green overnight: 19 collector runs, 608 service_health rows, 6,251 freshness checks. UI made public per user request: `https://ct-ui-254146383960.asia-south1.run.app`. Bugs found in overnight audit + fixed: (1) **alert spam** — dedup filtered to unacked alerts, but alerter acks after emailing → same idle_sink email every hour (18 in 24h); dedup now includes acked alerts → max 1 email per alert type per 4h; (2) **cost collector wrote 0 rows** — four sub-bugs: BigQuery table is `gcp_billing_export_resource_v1_*` (detailed export, not standard); billing currency is **INR** → now divides by `currency_conversion_rate` for USD; ClickHouse endpoint is `/usageCost` with `from_date/to_date` (was 404ing on `/usages`); Anthropic + OpenAI usage APIs need **Admin keys** — skipped with clear log until user creates them; (3) migration 007 widens `units_consumed` to NUMERIC(30,4) (GCP byte counts overflowed 16,4); (4) **weekly digest trigger 404'd** — targeted nonexistent job; created `ct-alerter-weekly` (alerter image + WEEKLY_DIGEST=true), added to bootstrap.sh, force-fired: digest email sent ✅. Verified cost data: ~$25/day total (Cloud Run $11, ClickHouse 7 CHC, Gemini API $5, Cloud SQL $1.2). **Remaining user actions:** (a) AWS RDS security group fix for resume-ingest-job, (b) Phase 3 PeerDB config, (c) healthchecks.io signup, (d) optional: Anthropic/OpenAI Admin API keys for LLM cost tracking. |
| 2026-07-24 | **Post-deploy UX + data-quality fixes (needs_review flood, variable coverage, freshness UX).** User review of live catalog found issues. (1) **needs_review flood (75% of tables)** — root cause: the source_type name heuristic (BC_*→erp) overrode the maker's source_type BEFORE the checker ran, so the checker's free guess always "disagreed" → 100% of heuristic-matched tables flagged; plus intra-ERP-family label differences (erp/finance/warehouse_ops) counted as conflicts. Fixed: skip source_type in checker when a heuristic set it + treat ERP-family source types as selection-equivalent. Verified clean run: **0% needs_review (was 75%)**. Migration 015 adds `review_reason` (stored + shown in UI so a flag explains itself). (2) **Variable coverage loose on Finance/ERP** — LLM under-labelled ERP dimension codes (document_type, currency_code, posting_group, source_code, account_type, etc.) as non-segments → many tables got 0 variables. Fixed: broadened `segment` prompt guidance with ERP examples + "label generously". Verified: **10/13 tables now get variables (was ~40%)**. (3) **Freshness UI** redesigned 9→6 cols (was overflowing off-screen), status-first plain English (Fresh/Stale/Dead/Static/Not-checked-yet), summary counts, legacy collapsed to a ≠legacy flag. (4) **needs_review made actionable** — Needs Review tab (renamed from Authority) shows why-flagged + Confirm/Mark-static row actions. (5) **Manual is_static toggle** added to Catalog drawer (any table) + resolve/static POST endpoints — humans fix what the LLM misses. Commits d2d5695, 8db0c9f on `sentinel`. Discovery + UI rebuilt/redeployed; verified on live prod data. |
| 2026-07-24 | **Post-deploy prod fixes: LLM proxies bug + progressive scanning.** First prod discovery run surfaced 2 issues. (1) **`Client.__init__() got unexpected kwarg 'proxies'`** — httpx 0.28 dropped the `proxies` kwarg the OpenAI/Anthropic SDKs pass → both dead, ran Gemini-only, no maker-checker. Fixed: pin `httpx==0.27.2` in discovery requirements (verified local + prod: `inferred by openai (checker=gemini)` — full cascade + maker-checker restored, agree=False→needs_review working). (2) **Catalog empty + timeout risk** — 450 tables, single end-commit meant nothing visible until a full pass that would exceed the 3600s task timeout (~12 tables/min Gemini-only). Fixed: **per-table `pg.commit()`** (catalog populates live, timeout-safe) + **batch cap `SENTINEL_MAX_TABLES_PER_RUN=80`** (progressive scanning — incremental gate skips done tables so consecutive runs advance the frontier; ~6 runs to fill 450, then steady-state cheap). Guarded dropped-table retirement behind `full_pass` (a capped break must not retire unvisited tables). Also: past Control Tower `registered_services`/`watched_tables` are a separate system, untouched — Sentinel builds its own `sentinel.catalog_overlay` from scratch. Rebuilt+redeployed ct-sentinel-discovery; per-table commits confirmed landing live in prod. **NOTE: fixes committed to working tree, need commit to `sentinel` branch.** |
| 2026-07-24 | **Sentinel DEPLOYED to production + committed.** Committed on branch `sentinel` (69e025c). Security: scrubbed `scripts/setup_secrets.sh` — removed hardcoded live keys (Anthropic/OpenAI/ClickHouse/Cohere/MS-Graph), now env/`.env.secrets`-driven (gitignored); old keys still in git history at `mvp` commit → user to rotate. `scripts/deploy.sh` written (6-step incremental) — fixed a real bug on first run (proxy port 5432 collided with a local Postgres → psql fell through to wrong server; now defaults PROXY_PORT=6543, asserts proxy bound + connected to control_tower before mutating). **Live in seoai-479305/asia-south1:** migrations 011–014 applied (7 sentinel tables), `ct-sentinel-discovery` + `ct-sentinel-check` jobs deployed, `trigger-ct-sentinel-discovery` (15 1 * * * = 06:45 IST) + `trigger-ct-sentinel-check` (0 2 * * * = 07:30 IST) schedulers ENABLED, ct-ui redeployed (Atlas restyle + Sentinel tabs, rev 00004, 200). SES absent → alerter stays MS Graph (as designed). First discovery run fired async (exec ct-sentinel-discovery-7vt4v) to bootstrap catalog. Autonomy confirmed: covers ALL ClickHouse tables (no allowlist), incremental gate keeps daily cheap. Deploy is additive — legacy freshness path untouched; parallel-verify window before any cutover. **Pending: (a) rotate leaked keys + scrub git history, (b) merge sentinel→main, (c) ≥1wk parallel verify then cutover migration.** |
| 2026-07-24 | **Sentinel table description made actionable (structured, consumer-facing) — real-data verified.** The single `summary` ("Order records for Holistique brands") was too thin for a consumer (RBAC MCP / query-author LLM) to select+query correctly. Replaced with a structured `description` JSONB (migration 013): `what_it_is`, `purpose`, `key_facts[]`, `when_to_use`, `when_not_to_use`. LLM prompt heavily instructed for specificity (ground in actual columns/engine/samples, name the dedup key, state units, call out traps); `_validate` enforces all subfields non-empty. `summary` kept as a one-line derivative for list views. UI CatalogView rows now expand to a description panel (what/purpose/grain/key-facts + green when-to-use / red when-NOT-to-use boxes + dedup key + quirks). **Real LLM output (Finance_AP_Summary_Details) — genuinely actionable:** what_it_is="ERP accounts-payable ledger from Business Central, one row per posted AP entry", grain="one row per AP entry, keyed by (id,entryNo)", key_facts include "Requires dedup on (id,entryNo) via ReplacingMergeTree or rows double-count" + "Amounts in currency per currencyCode", when_not_to_use="not without deduplication; reversed entries complicate reporting". Verified: migration 013 idempotent, validation rejects thin descriptions, catalog endpoint + expandable UI render the structure, page 200. |
| 2026-07-24 | **Sentinel variable selection reworked — business-dimensions-only, capped at 3 (real-data verified).** Prior logic had two mismatched gates: sampling was type-based (LowCardinality/Enum/Bool/UInt8) so it MISSED plain-String business dims (Brand, Channel_name) and only caught booleans/CDC cols; role filter trusted a coarse LLM 'dimension' label. Net on a real sales table: tracked `_peerdb_is_deleted` (garbage), missed every real dimension. Fix (user: business-dims-only, LLM-ranks + code-caps): (1) **llm.py** sharpened role vocab — `segment` = business-partitioning dimension a stakeholder groups by (brand/channel/region), explicitly NOT ids/skus/free-text/flags/measures/CDC; variables returned in priority order. (2) **main.py** `populate_variable_values` rewritten — hard-exclude CDC prefixes, live `uniqExact` cardinality probe (keep only 3..MAX_DISTINCT distinct → drops constants + binary flags + runaway), fetch values for plain-String segments the type-gate missed, cap `MAX_TRACKED_VARIABLES=3` in LLM priority order. (3) **Freshness reporting** — endpoint + UI now show a `tracked_variables` column per table (badges; "table-level only" when none). **Real-data proof (Holistique_All_website_weekly_sales):** LLM now labels Brand/Channel_name/customer_type=segment, variant_sku/customer_id=key, sales=measure, _peerdb_*=excluded; code gate then keeps only **Brand** (4 values: Belif/DHC/The Face Shop/Better Flour) — correctly drops Channel_name (cardinality=1, constant) and customer_type (cardinality=2, binary). Coverage clean, freshness endpoint surfaces tracked_variables=['Brand'], also flagged this table dead (8d stale vs daily-expected — real finding). |
| 2026-07-24 | **Sentinel E2E tested against REAL ClickHouse + REAL LLM + local PG (2 real bugs caught + fixed).** Prior tests were mock-CH/mock-LLM; this ran the actual external wiring. Setup: cloud-sql-proxy blocked by auto mode for writing to prod control_tower → tested against local throwaway PG (001+011+012) with real CH (457 tables, 4 DBs) + real OpenAI/Gemini/Anthropic. Discovery on Finance_AP_Summary_Details → correct LLM inference (concept=spend, source_type=erp, dedup=[id,entryNo], conf 0.90, cadence 0.1428 daily), overlay+6 targets+variable_values+5 recon rules written. Check engine on 6 targets against real CH → freshness fresh (system.parts, 4.1h age), **reconciliation deduped count 37553=37553 diff 0%** (real argMax dedup query ran), schema_drift none, volume 41699 first-obs. **Bug 1 (would break ALL discovery in prod):** LLM prompt described fields in prose but never gave exact JSON keys → all 3 providers returned their own shape (short_description vs summary) → every table failed validation → skipped. Fixed: added explicit `_OUTPUT_SPEC` with exact key names/types to prompt; re-verified all 3 providers return valid schema. **Bug 2 (would false-alert every bool/enum in prod):** discovery sampled `str(value)` (Python `True/False`) but coverage check reads `toString(col)` (CH `true/false`) → mismatch → every boolean column flagged all-values-missing (4 false warns + 4 false alerts observed). Fixed: sampling now uses `toString()` too; re-ran clean — all coverage OK, 0 false incidents, stored values `false/true`. UI verified against real data (catalog/recon/coverage all render, 200). **Operational note surfaced:** a hard-killed discovery run left an advisory lock via idle-in-transaction backend, blocking next run until backend died — Cloud Run self-heals on container kill (connection closes) but task-timeout=1800 must exceed real runtime. **Real-wiring now proven end to end.** Minor noise noted (not fixed): LLM tagged `_peerdb_is_deleted` CDC control col as a dimension → got a coverage target; harmless, could filter later. |
| 2026-07-24 | **Sentinel UI caught up to S2b/S5b backend (3 gaps closed).** After S2b/S5b the backend produced data the 5 Sentinel tabs didn't show. Fixed: (1) **Coverage** now surfaces `missing_values` + last check status per variable (LATERAL join to latest variable_coverage check_result; amber row when values missing) — was active/retired counts only; (2) **Reconciliation** now shows last-check `a vs b (diff%)` colored against tolerance + observe promotion progress `n/3` — was rule+status only; (3) **Incidents** now renders rollup parents (badge + child count) with ↳-indented children — flood-suppression was invisible. E2E verified: missing_values=['GONE'], recon a=500/b=1000/diff=50%, rollup parent+3 children all render; page 200, no errors. **Sentinel now feature-complete front to back.** |
| 2026-07-24 | **Sentinel S2b + S5b built — now feature-complete vs spec (all 5 checks + 4 recon kinds + coverage + rollup).** Closed the 6 staged/pending pieces. **S2b (discovery):** `populate_variable_values` records low-card dimension values into `variable_values` (reuses PII-safe samples — no extra CH query), retires unseen values, registers per-variable targets; `generate_recon_rules` now emits all **4 kinds** (segment_vs_total, referential_integrity, duplicate_concept_divergence, cross_source_agreement) not just 1. **S5b (check engine):** check 3 `check_variable_coverage` (set-diff active values vs live DISTINCT, warn/fail by missing ratio); check 5 `run_reconciliation` (deduped counts via overlay `argMax...HAVING` pattern, per-rule verdict vs tolerance/direction) + `apply_recon_promotion` (observe→active after 3 stable runs, reset on breach); `check_volume` gained a real baseline/band (row-count DROP >5% warn, >30% fail vs last check); `roll_up_incidents` groups ≥3 open table incidents per database under a synthetic parent (flood suppression). **Bug caught + fixed in E2E:** reconciliation ran once per due target → duplicated results/alerts when a table had multiple due variable targets; now runs once per table (recon_done set). **Verified:** all compile+import; unit tests (volume band 5 cases, coverage set-diff, promotion state machine, dedup-SQL builder); 18 new SQL stmts valid vs real 001+011+012; **full check-engine pass with mock ClickHouse** wrote all 5 check types, opened incidents, bridged an alert per finding, created rollup parent; recon-dedup fix confirmed (1 not 2); UI coverage+reconciliation endpoints render live data. **Sentinel code now feature-complete.** Still deferred by design (not lapses): "self connector" auto-remediation (§9, user hasn't picked). **Next: deploy via bootstrap → S9 parallel-verify cutover.** |
| 2026-07-24 | **Sentinel freshness-tolerance lapse fixed + S1 secrets created.** Review caught `monitor_frequency_weeks` doing two jobs — check cadence AND freshness tolerance — so a daily-updating table wouldn't flag stale for a week (regression vs legacy adaptive cadence), and the design's `freshness_tolerance` field was written/read nowhere. Fix: **migration 012** adds `sentinel.monitor_targets.expected_cadence_weeks NUMERIC(8,4)` (decimal weeks → sub-week cadences: daily=0.1428, 2h=0.0119); LLM now infers it (`expected_cadence_weeks` in llm.py schema+prompt+validation, bounds 0.001–8); discovery `upsert_target` writes it; check engine `check_freshness` uses it for the fresh/stale/dead verdict (falls back to `monitor_frequency_weeks` when NULL); `monitor_frequency_weeks` is now check-cadence-only. UI Freshness gains an "Expected" column (`cadenceLabel` humanizes decimal weeks). **Verified:** migration 012 applies after 011 + idempotent, decimal round-trips (0.1428), updated SQL valid, unit test confirms daily-cadence table 3d-dead now correctly FAILS (was fresh-for-a-week), fallback + LLM bounds enforced, UI E2E returns expected_cadence_weeks + renders 200. **S1 DONE** — user created ct-sentinel-{openai,gemini,anthropic}-key from Atlas keys + granted ct-collector accessor (Secret Manager writes blocked in auto mode). SES secrets still pending user creds (non-blocking). **Next: deploy via bootstrap → S9 cutover.** |
| 2026-07-24 | **Sentinel S2–S8 built + locally verified (code complete, not deployed).** All resolving the 4 review lapses in-build. (1) **Discovery loop** `sentinel/discovery/{main.py,llm.py}` — introspect `system.tables/columns`, structure_hash incremental gate (skip LLM when unchanged), PII-safe sampling (low-card DISTINCT only; `recruitment_hr` schema+stats only, asserted), LLM cascade **OpenAI→Gemini→Claude + maker-checker** (`llm.py`, structured JSON, confidence gate 0.80), source_type heuristics, authority reconciliation I1/I2 (global each run, honors `updated_by='human'` pins), conflict flags I3 unresolved_dedup, dropped-table retire, monitor_targets + observe recon rules. (2) **Check engine** `sentinel/check_engine/main.py` — due-target tick, freshness (system.parts **primary + max(col) fallback**, Lapse 4; `SENTINEL_FRESHNESS` env override), volume, schema_drift; incidents open/resolve with scope-dedup; **alert bridge** — incident open inserts `control_tower.alerts` row (`incidents.alert_id` FK) so live ct-alerter delivers. Checks 3 (coverage) + 5 (reconciliation execution) staged — activate once variable_values/rules populate. (3) **SES sender** added to `intelligence/alerter/main.py` alongside MS Graph, `EMAIL_TRANSPORT=graph|ses` (graph default); boto3 added; both transports import-verified. (4) **UI** — 5 new Sentinel tabs (Catalog / Authority&Conflicts / Coverage / Reconcile / Incidents) + Sentinel summary strip on Overview + **Freshness reworked to dual-source** (Sentinel vs legacy, mismatches highlighted) for cutover; 7 `/api/sentinel/*` endpoints. (5) **bootstrap.sh** — deploys `ct-sentinel-discovery` (06:45 IST) + `ct-sentinel-check` (07:30 IST) with LLM secrets + 2 schedulers; SES-switch documented. **Verification (throwaway PG16):** all 27 embedded SQL statements (discovery writes, check-engine writes+bridge, 7 UI queries) valid against real 001+011 schema; discovery+check pure logic unit-tested (heuristics, structure_hash order-independence, CDC detection, needs_review gate, freshness fresh/stale/dead classification); UI booted against seeded DB — all 7 endpoints 200, page renders, dedup_key TEXT[]→JSON array clean. Keys copied from `/Users/rajaram/atlas/.env.local` into gitignored local `.env` for testing (never committed). **Next: S1 (user adds 6 secrets to Secret Manager) → deploy via bootstrap → S9 cutover after ≥1wk parallel verify.** |
| 2026-07-24 | **Sentinel (data freshness/consistency monitor + catalog overlay) spec + schema, architecture-reviewed.** New subsystem built INTO Control Tower per `DATA_MONITOR_SPEC.md` v1.1. (1) **Migration 011** — `sentinel` schema, 8 tables (`catalog_overlay`, `monitor_targets`, `variable_values`, `reconciliation_rules`, `check_results`, `incidents`, `source_type_vocab`) in the same `agenteye-pg` control_tower DB. Verified on throwaway PG16: applies after 001, idempotent, FK `incidents.alert_id`→`control_tower.alerts` present. (2) **`SENTINEL_BUILD_SPEC.md`** — impl spec, 2 Cloud Run Jobs (`ct-sentinel-discovery` daily, `ct-sentinel-check` daily tick), LLM cascade OpenAI→Gemini→Claude w/ maker-checker (keys from `/Users/rajaram/atlas/.env.local` → new `ct-sentinel-*` secrets, user to add). (3) **Architecture review (2 parallel Explore agents) found 4 lapses vs live Control Tower — ALL resolved in spec, not deferred:** L1 duplicate freshness/volume over the same ~329 ClickHouse tables (existing `watched_tables`→`check_data_freshness`→`data_freshness`→anomaly `idle_sink`) → **Sentinel supersedes via parallel-then-switch** (migration 012 + code removal only after ≥1wk ≥99% match; legacy retained for rollback); L2 `incidents` had no email path → **alert bridge**: incident open inserts `control_tower.alerts` row (via `incidents.alert_id` FK), live `ct-alerter` delivers, no new job; L3 schema drift → renamed all `db`→`database_name` (verified 0 stray/4 correct), status vocab `ok/warn/fail` w/ freshness sub-status in `observed` JSONB, BIGSERIAL-vs-UUID PK documented intentional; L4 `system.parts` unproven here (existing code only `max(col)`) → primary+`max(col)` fallback, validate on real cluster in S4. (4) **User decisions:** SES creds coming → wire AWS SES sender alongside MS Graph in alerter (`ct-ses-*` secrets); full UI Sentinel section (Catalog / Authority&Conflicts / Coverage / Reconciliation / Incidents tabs) + reworked Freshness (dual-source during cutover). Build phased S0–S9; S0 done. **Nothing deployed, nothing committed.** Next: S1 (user adds 6 secrets), then S2 discovery loop code. |
| 2026-07-13 | **Anomaly detector decoupled from ClickHouse (user decision: Postgres-only until scale demands PeerDB), UI redesigned Services/Jobs split, job-region bug fixed, 19 discovered resources activated, Cloud Scheduler HTTP-target monitoring added, CDN dependency removed.** (1) Rewrote all 5 anomaly checks to query Postgres `service_health`/`third_party_api_health` directly (`percentile_cont` for p95, plain `AVG`/`DISTINCT ON`) — no ClickHouse client, no CH_HOST/USER/PASS, `clickhouse-connect` dropped from requirements. **Code complete, not yet deployed** (held per explicit user instruction — needs separate go-ahead). (2) **Root cause found for blank Jobs data**: `crawler-indexer/processor/sync` + `ad-url-updater-sync-job` registered with `region=asia-south1` in DB but actually deployed in `us-central1` — Cloud Run Admin API silently returns 0 executions for wrong region (no error), fields stayed NULL forever. Migration 008 fixes region + adds `job_last_execution_at` column (the job's own last-completion time, distinct from `collected_at` which is only when Control Tower scraped it — critical distinction the old UI conflated). (3) **Coverage gap found**: `auto_discover()` defaults new finds to `active=false` as a safety net; 23 real services/jobs accumulated there unreviewed, invisible to collection (matches user's "GCP shows 30/34, my data doesn't" report). Migration 010 activates the 19 that are legit unreviewed pipelines (creative-analysis-job, google-ads-sync-job, hector-sync, nykaa-ads-sync, 4x process-*/zepto-*, shopflo-sync, vinculum-sync, bharat-trends-scanner, hol-ai-skin-analyser, holistique-library-manager, seo-geo-content-automator, skin-analyzer-dashboard, report-platform-api/frontend); leaves 4 deliberately-excluded ones inactive (librechat, open-webui, youtube-desc-gen, my-google-ai-studio-applet — shared tools/managed services, not Glide pipelines). service_health rows jumped 32→51 immediately. (4) **UI redesign**: split single mixed Services/Jobs table (dashes meant two different things — N/A-by-type vs actually-broken, indistinguishable) into `/api/services` (traffic/latency) and `/api/jobs` (status/last-run/duration) with a new Jobs nav tab; `agoColor()` helper colors staleness (green ≤2h, gray ≤26h, amber older); Overview card now shows jobs-failing count. (5) **New: Cloud Scheduler HTTP-target monitoring** — 18 schedulers call a Cloud Run *service's* HTTP endpoint directly (cron logic inside the service, e.g. `agenteye`'s `/api/cron/*`, `glide-atlas-*`) rather than a real Cloud Run Job — these had zero visibility since there's no Job execution to inspect. New `scheduler_jobs` table (migration 009) + `collect_scheduler_status()` classifies scheduler targets via regex (job `:run` URL vs plain HTTP) and stores the scheduler's own last-attempt status/time for the HTTP-direct ones only (job-targeting schedulers already covered via job_last_execution_at). Already surfaced real failures on first local test: `yt-dashboard-daily-sync`, `report-schedule-dispatch`, `glide-atlas-ig-account-sync` all failing. **Needs `roles/cloudscheduler.viewer` grant on ct-collector SA — blocked pending user approval** (same category as run.invoker); collector runs clean without it (403 logged as warning, doesn't break other steps). (6) **Fixed likely cause of "blank page on refresh"**: dashboard imported Preact/htm/hooks from `esm.sh` CDN on every page load — no local fallback, so any CDN blip/ad-blocker/network hiccup = blank page with no error shown. Vendored all 3 libraries into `ui/backend/vendor/`, served via FastAPI StaticFiles mount, zero external runtime dependency now. Also added `Cache-Control: no-cache` on the HTML shell to prevent stale-page issues after redeploys. **User actions:** (a) grant `cloudscheduler.viewer` to activate scheduler monitoring, (b) approve anomaly-detector deploy when ready, (c) still-pending from before: AWS RDS fix, PeerDB config, healthchecks.io. |
