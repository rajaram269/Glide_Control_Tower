# Control Tower — Page-by-Page Feature & Background Flow Guide

> **Audience:** Anyone who wants to understand exactly what each page on the Control Tower dashboard does, how the data gets there, what background scripts run, which database tables are used, and what happens when you click something.
> **Language:** Plain English with clear technical diagrams. No code changes are made by this document.

---

## 1. High-Level Architecture Overview

Control Tower is an **end-to-end monitoring and automated data-quality system**. It operates across 4 key stages:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 1. DATA COLLECTION (Background Cloud Run Jobs & Schedulers)                     │
│    • GCP Collector (hourly)     • AWS Collector (hourly)                        │
│    • Cost Collector (daily)     • Sentinel Discovery & Check Engine (daily)     │
└──────────────────────────────────────┬──────────────────────────────────────────┘
                                       │ (Writes metrics, logs, checks)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 2. CENTRAL REPOSITORY (PostgreSQL: `agenteye-pg` -> `control_tower` DB)         │
│    • Schema `control_tower` (Services, Jobs, Schedulers, Alerts, Costs)         │
│    • Schema `sentinel` (Catalog, Targets, Variables, Rules, Incidents)          │
└──────────────────────┬───────────────────────────────────┬──────────────────────┘
                       │                                   │
                       ▼                                   ▼
┌─────────────────────────────────────────┐ ┌─────────────────────────────────────┐
│ 3. INTELLIGENCE & ALERTING ENGINE       │ │ 4. FASTAPI + REACT/PREACT DASHBOARD │
│    • Anomaly Detector (hourly :30)      │ │    • Serves 12 interactive views    │
│    • Alerter (hourly :45 via MS Graph)  │ │    • Live Cloud Logging viewer      │
│    • Auto-Resolver (LLM triage)         │ │    • 1-Click Acknowledge & Resolve  │
└─────────────────────────────────────────┘ └─────────────────────────────────────┘
```

---

## 2. The 12 Dashboard Pages Explained

The dashboard is organized into two main sections in the sidebar:
1. **Observability** (Infrastructure & Cloud Operations)
2. **Sentinel** (AI Business Data Quality & Catalog)

---

### GROUP 1: OBSERVABILITY (Infrastructure Monitoring)

---

### 1. Overview Page (`#overview`)

#### What it shows:
- **Plain-English Health Banner:** An instant color-coded status message ("*Everything looks healthy*" in green, or "*Some things need attention*" in red/amber) with clickable shortcut chips (e.g. `2 jobs failing →`, `1 table not syncing →`, `3 alerts to review →`).
- **4 Key Executive Stat Cards:**
  1. *Data tables monitored:* Total ClickHouse tables cataloged + how many are verified fresh.
  2. *Live services:* Count of running Cloud Run API services handling user traffic.
  3. *Scheduled jobs:* Count of background batch jobs and whether any are currently failing.
  4. *Spend this week:* Total USD spent across all cloud infrastructure and AI APIs over the last 7 days.
- **Third-Party Provider Status Grid:** Live status cards for external dependencies (OpenAI, Anthropic, GCP, Cloudflare, BrightData balance, etc.).

#### Step-by-Step Background Flow:
```
[User opens Dashboard]
       │
       ├──► GET /api/summary ────────► Queries `control_tower.registered_services`,
       │                               `service_health`, `alerts`, `cost_metrics`
       │
       ├──► GET /api/sentinel/health ─► Queries latest `sentinel.check_results` (sync + data),
       │                               `sentinel.catalog_overlay`, and open `incidents`
       │
       └──► GET /api/providers ───────► Queries `control_tower.provider_status`
```

#### What happens on user interaction:
- Clicking any **attention chip** (e.g. `2 critical alerts →`) immediately navigates to that specific sub-page.
- Clicking the **Theme toggle** switches between light and dark modes (persisted in browser `localStorage`).

---

### 2. Services Page (`#services`)

#### What it shows:
- Monitors all **always-on, request-driven Cloud Run services** (e.g., backend APIs, web dashboards).
- Displays: Service Name, Region, Request Count (last hour), Error Count & Error Rate %, Active Container Instances, Latency (p50 and p95 in milliseconds), and Last Scraped Time.
- **Search bar:** Filter services instantly by name.
- **Click-to-Expand Deep Dive:** Clicking any service row opens two interactive panels directly under the row:
  1. **Endpoint Breakdown Panel:** Exact URL paths (e.g. `/api/v1/orders`), request counts, and error rates to identify if a single broken endpoint is hiding behind a normal overall average.
  2. **Live Logs Panel:** Live Cloud Logging stream for that service with an "errors only" filter checkbox.

#### Step-by-Step Background Flow:
```
[GCP Collector (ct-gcp-collector) runs hourly]
       │
       ├─► 1. Auto-discovers Cloud Run services across all regions (locations/-)
       ├─► 2. Queries Google Cloud Monitoring API for traffic, 5xx errors, latency (p50/p95), instances
       ├─► 3. Inspects Cloud Logging for internal error logs & bad endpoints (silent failure checks)
       └─► 4. Writes snapshot into `control_tower.service_health` (platform = 'cloud_run_service')

[Frontend UI]
       │
       ├─► GET /api/services ─────────────────► Returns latest distinct row per active service
       │
       ├─► [User clicks service row]
       │     ├─► GET /api/services/{name}/endpoints ─► Queries Cloud Logging live for URL path breakdown (24h)
       │     └─► GET /api/logs/service/{name} ────────► Queries Cloud Logging live for container log lines
```

---

### 3. Jobs Page (`#jobs`)

#### What it shows:
- Monitors all **scheduled batch jobs and cron tasks** (Cloud Run Jobs and Cloud Schedulers).
- Displays: Job Name, Region, Status badge (`succeeded`, `running`, `stuck`, `cancelled`, `missed`, `failed`), Last Run Duration, When the job actually completed (`job_last_execution_at`), and Last Scraped time.
- **Sortable Columns:** 3-way sort (Ascending / Descending / Default) by Outcome, Duration, or Last Run time.
- **Cloud Scheduler Section:** Shows all Cloud Schedulers, their cron schedule (e.g. `0 6 * * *`), target URI, and last attempt result.
- **Click-to-Expand Live Logs with Execution Selector:** Clicking any job row opens its logs, including a dropdown selector to inspect specific past executions from the last 2 days.

#### Step-by-Step Background Flow:
```
[GCP Collector (ct-gcp-collector) runs hourly]
       │
       ├─► 1. Discovers Cloud Run Jobs across all 38 Google Cloud regions
       ├─► 2. Calls Cloud Run Executions API (run_v2.ExecutionsClient) for exact status & completion times
       ├─► 3. Discovers all Cloud Schedulers (CloudSchedulerClient) & maps schedules to jobs
       ├─► 4. Runs `detect_missed_schedules`: compares cron expression vs last run time (catches silent missed jobs)
       ├─► 5. Writes to `control_tower.service_health` (platform='cloud_run_job') & `control_tower.scheduler_jobs`
       └─► 6. Cleans up: Marks deleted jobs/services as inactive so they disappear from UI

[Frontend UI]
       │
       ├─► GET /api/jobs & GET /api/scheduler-jobs
       │
       └─► [User clicks job row]
             ├─► GET /api/logs/job/{name}/executions ─► Lists all execution IDs in the last 2 days
             └─► GET /api/logs/job/{name}?execution=XYZ ─► Streams logs of that exact execution from Cloud Logging
```

---

### 4. Providers Page (`#providers`)

#### What it shows:
- Tracks the health of **third-party platforms and APIs** that your applications rely on.
- Monitored providers include: **OpenAI, Anthropic, Google Cloud Platform, Cloudflare, Firebase, Cohere, BrightData**.
- Real Balance Monitoring: For **BrightData**, it displays live account balance and pending costs, turning red if balance is exhausted.
- Displays overall status (`operational`, `degraded`, `major_outage`) and specific affected sub-components.

#### Step-by-Step Background Flow:
```
[GCP Collector runs hourly]
       │
       ├─► 1. Polls public status pages (status.openai.com, status.anthropic.com, status.cloud.google.com, etc.)
       ├─► 2. Calls BrightData API (/customer/balance) using Secret Manager API token
       └─► 3. Writes results into `control_tower.provider_status`

[Frontend UI]
       │
       └─► GET /api/providers ──► Fetches latest status record per provider and displays cards
```

---

### 5. Alerts Page (`#alerts`)

#### What it shows:
- Central command center for all operational and infrastructure problems detected by the system.
- Displays: Severity badge (`critical`, `warn`, `info`), Alert Type, Affected Service/Table, Plain-English Title, Fired Time, Status (`unacknowledged` / `acknowledged`).
- **Side Drawer on Click:** Clicking any alert opens a detailed right-hand drawer showing:
  - Full diagnostic message & context data (e.g. error rate %, latency change, missed schedule details).
  - **Specific Actionable Advice:** Clear guidance on how to fix the problem (e.g., check Cloud Scheduler permissions, inspect out-of-memory logs, cancel stuck tasks).
- **1-Click Acknowledge Button:** Allows engineers to mark an alert as handled directly from the UI.

#### Step-by-Step Background Flow:
```
[Anomaly Detector (ct-anomaly-detector) runs hourly at :30]
       │
       ├─► Reads `control_tower.service_health` (PostgreSQL)
       ├─► Evaluates 6 core rules:
       │     1. Error rate spike (>5% and >2x baseline)
       │     2. Job duration drift (>50% slower and >60s)
       │     3. Job problem (failed, missed, cancelled, or stuck)
       │     4. Silent failure (error logs >=3 with HTTP error <5%)
       │     5. Broken endpoint (>=3 requests and >=50% error rate)
       │     6. EC2 resource pressure (CPU>85%, Mem>90%, Disk>85%)
       ├─► Deduplicates alerts against a 4-hour cooldown window
       └─► Writes new problems to `control_tower.alerts`

[Alerter (ct-alerter) runs hourly at :45]
       │
       ├─► Reads unacknowledged alerts from `control_tower.alerts`
       ├─► Cross-checks `provider_status` (suppresses false alarms if upstream provider is down)
       ├─► Calls AI (Anthropic Claude) to generate a concise 2-3 sentence root-cause triage
       ├─► Sends rich HTML email via Microsoft Graph API (hr@ / ai@holistique.in) or AWS SES
       └─► Marks `acknowledged_at = NOW()` once emailed

[User on Dashboard]
       │
       ├─► GET /api/alerts?status=unacked ──► Lists active alerts
       └─► POST /api/alerts/{id}/ack ────────► Updates `acknowledged_at` in DB with `ack_by='dashboard'`
```

---

### 6. Costs Page (`#costs`)

#### What it shows:
- Tracks cloud and AI infrastructure spending compared against monthly budgets.
- Displays: Cost Source (Google Cloud, AWS, ClickHouse Cloud, OpenAI, Anthropic), Resource Name / SKU, Spend This Week ($), Spend Last Week ($), Monthly Budget ($), and Budget Burn % bar with trend indicators (up/down/flat).

#### Step-by-Step Background Flow:
```
[Cost Collector (ct-cost-collector) runs daily at 06:00 IST]
       │
       ├─► 1. Queries GCP BigQuery detailed billing export (`billing_export.gcp_billing_export_resource_v1_*`)
       │      Converts INR billing to USD via currency conversion rates
       ├─► 2. Calls ClickHouse Cloud /usageCost API for compute/storage costs
       ├─► 3. Calls AWS Cost Explorer API for EC2/RDS spend
       ├─► 4. Queries OpenAI & Anthropic Admin Usage APIs for token expenses
       ├─► 5. Compares actuals against `control_tower.cost_budgets`
       └─► 6. Writes daily summary into `control_tower.cost_metrics`

[Frontend UI]
       │
       └─► GET /api/costs ──► Aggregates 7-day vs previous 7-day spend per source and calculates burn %
```

---

### GROUP 2: SENTINEL (AI Business Data Quality & Catalog)

---

### 7. Catalog Page (`#catalog`)

#### What it shows:
- An **AI-generated semantic dictionary** for every business data table in ClickHouse (Finance, Sales, Marketing, Logistics, HR).
- Displays: Database & Table Name, Business Concept (e.g. `sales_revenue`, `inventory_level`, `spend`), Source Type (ERP, CRM, Marketing), Grain (what 1 row represents), Deduplication Key, and Static status.
- **Expandable Description Panel:** Clicking a table expands a rich card showing:
  - *What It Is & Business Purpose*
  - *Key Facts & Units of Measure*
  - *Deduplication logic (e.g. ReplacingMergeTree argMax rules)*
  - *Green "When to use" vs Red "When NOT to use" guidance*
  - *Manual "Mark as Static / Dynamic" button* for human override.

#### Step-by-Step Background Flow:
```
[Sentinel Discovery (ct-sentinel-discovery) runs daily/weekly]
       │
       ├─► 1. Introspects ClickHouse `system.tables` & `system.columns` across all databases
       ├─► 2. Computes `structure_hash` — skips LLM analysis if table structure hasn't changed
       ├─► 3. PII-Safe Sampling: Samples distinct values for low-cardinality columns (no customer PII)
       ├─► 4. Multi-LLM Cascade: OpenAI GPT-4o -> Gemini 1.5 Pro -> Claude 3.5 Sonnet
       ├─► 5. Maker-Checker Verification: 2nd LLM verifies the 1st LLM's classification
       ├─► 6. Generates structured JSON (grain, dedup rules, usage traps, cadence)
       └─► 7. Writes to `sentinel.catalog_overlay` with per-table commit (safe from timeouts)

[Frontend UI]
       │
       ├─► GET /api/sentinel/catalog ──► Returns cataloged tables
       └─► POST /api/sentinel/overlay/{db}/{tbl}/static ──► Toggles `is_static` with human pin (`updated_by='human'`)
```

---

### 8. Needs Review Page (`#review`)

#### What it shows:
- An **AI triage queue** showing data tables where the AI encountered ambiguity or disagreement.
- Examples of review flags:
  - *Maker-Checker Disagreement:* The primary AI and validator AI disagreed on business concept or source type.
  - *Low Authority Confidence:* Confidence score fell below 0.80.
  - *Dangling / Broken Pointer:* Table references a source that no longer exists.
  - *Unresolved Deduplication:* AI could not verify the exact primary key for deduplication.
- **Action Buttons:**
  - `Confirm` button: Approves the table's classification and pins it as human-verified.
  - `Mark Static` button: Confirms the table is an archived/historical dataset that will not receive new updates.

#### Step-by-Step Background Flow:
```
[Sentinel Discovery flags a table with review_status='needs_review']
       │
       ├─► [Optional: Auto-Resolver (sentinel/resolver) runs]
       │     └─► Second-stage adjudicator LLM attempts automated resolution using deeper context
       │
       └─► [Remaining tables surface on Needs Review page]
             │
             ├─► GET /api/sentinel/review-queue
             │
             ├─► [Human clicks "Confirm"]
             │     └─► POST /api/sentinel/overlay/{db}/{tbl}/resolve
             │           └─► Sets `review_status='confirmed'`, `conflict_type='none'`, `updated_by='human'`
             │                 (Permanent human pin: future discovery runs will never overwrite it)
             │
             └─► [Human clicks "Mark Static"]
                   └─► POST /api/sentinel/overlay/{db}/{tbl}/static
                         └─► Sets `is_static=true`, `updated_by='human'` across catalog & monitor targets
```

---

### 9. Coverage Page (`#coverage`)

#### What it shows:
- Tracks whether **key business partition dimensions** (e.g. brand names, sales channels, geographic regions) are still actively present in your data tables.
- Displays: Table Name, Tracked Variable (e.g. `Brand`, `Channel_name`), Active Values count, Retired Values count, Last Seen timestamp, Last Check Status (`ok`, `warn`, `fail`), and List of Missing Values (highlighted in amber/red if a brand or channel stopped reporting).

#### Step-by-Step Background Flow:
```
[Discovery Phase]
       │
       ├─► Discovers business segment variables (capped at top 3 per table)
       ├─► Samples known values and records them in `sentinel.variable_values` (lifecycle='active')
       └─► Registers a monitor target in `sentinel.monitor_targets`

[Sentinel Check Engine (ct-sentinel-check) runs daily]
       │
       ├─► Runs `check_variable_coverage`: queries live DISTINCT values in ClickHouse
       ├─► Compares live values against expected active values in `sentinel.variable_values`
       ├─► If an expected value is missing:
       │     └─► Flags `warn` or `fail` depending on % missing
       │     └─► Opens an incident in `sentinel.incidents`
       └─► Writes check verdict into `sentinel.check_results`

[Frontend UI]
       │
       └─► GET /api/sentinel/coverage ──► LATERAL join between variable_values and latest check_results
```

---

### 10. Reconcile Page (`#recon`)

#### What it shows:
- Monitors **cross-table consistency and mathematical reconciliation rules** generated by AI.
- Rule types:
  1. *Segment vs Total:* (e.g. Sum of sales across individual brand tables equals the master revenue total).
  2. *Referential Integrity:* (e.g. Every order ID in shipment records exists in the master orders table).
  3. *Cross-Source Agreement:* (e.g. Shopify orders count matches ERP invoices count).
  4. *Duplicate Concept Divergence:* (e.g. Detecting discrepancies between two tables claiming to hold the same metric).
- Displays: Concept, Metric, Source A vs Source B, Tolerance %, Status (`observe` vs `active`), Stable Runs count (`n/3`), Last Run values (`A`, `B`, `Diff %`), and Status badge.

#### Step-by-Step Background Flow:
```
[Discovery Phase]
       │
       └─► LLM identifies complementary tables and inserts rules into `sentinel.reconciliation_rules` (status='observe')

[Sentinel Check Engine runs daily]
       │
       ├─► Executes deduplicated ClickHouse queries (using `argMax` ReplacingMergeTree logic)
       ├─► Compares Metric(Source A) vs Metric(Source B) -> computes `diff_pct`
       ├─► Promotion Engine (`apply_recon_promotion`):
       │     • If diff_pct <= tolerance for 3 consecutive runs -> Promotes rule to `active`
       │     • If diff_pct breaches tolerance -> Resets stable_runs counter to 0
       ├─► If an `active` rule breaches tolerance:
       │     └─► Opens an incident in `sentinel.incidents`
       │     └─► Inserts an alert into `control_tower.alerts` via Alert Bridge
       └─► Writes execution data into `sentinel.check_results`

[Frontend UI]
       │
       └─► GET /api/sentinel/reconciliation ──► Fetches rules and latest A vs B values
```

---

### 11. Freshness Page (`#freshness`)

#### What it shows:
- Tracks whether each ClickHouse business table is **being updated on time or has gone stale/dead**.
- **Dual-Freshness Technology:**
  1. **Sync Freshness:** Checks when the underlying data pipe (e.g. PeerDB CDC or batch ingestion) last inserted rows (using `system.parts` partition modification times).
  2. **Data / Business Date Freshness:** Checks the most recent real business transaction date inside the data (e.g. `max(order_date)`), catching cases where sync is running but the source data is frozen in the past.
- Displays: Database & Table Name, Status badge (`fresh`, `stale`, `dead`, `static`), Check Mechanism, Expected Cadence (e.g. `daily`, `weekly`), Tracked Variables, and Sync vs Data details.
- **Side Drawer:** Clicking any table row opens a deep drawer explaining whether the sync pipeline or the business source is responsible for any delay.

#### Step-by-Step Background Flow:
```
[Sentinel Check Engine runs daily]
       │
       ├─► Queries ClickHouse `system.parts` for partition `max_modification_time` (Sync Freshness)
       ├─► Queries `max(event_date_col)` for real business date (Data Freshness)
       ├─► Compares elapsed time against `expected_cadence_weeks`:
       │     • Age <= 1.5x expected -> `fresh`
       │     • Age > 1.5x and <= 3x -> `stale` (warn)
       │     • Age > 3x -> `dead` (fail)
       │     • If `is_static=true` -> `static` (never alerts)
       ├─► If stale or dead:
       │     └─► Opens incident in `sentinel.incidents`
       │     └─► Triggers alert bridge to `control_tower.alerts`
       └─► Writes result to `sentinel.check_results`

[Frontend UI]
       │
       └─► GET /api/sentinel/freshness ──► Returns dual-source status (Sentinel check vs Legacy table)
```

---

### 12. Incidents Page (`#incidents`)

#### What it shows:
- Complete ledger of all **data quality incidents** detected across ClickHouse tables by Sentinel.
- Displays: Severity (`critical`, `warn`, `info`), What Happened (Plain-English title), Affected Database & Table, Opened Time, and Status (`open` vs `resolved`).
- **Synthetic Rollup Parents:** If ≥3 incidents occur in the same database at once, Sentinel groups them under a single "group" parent row to prevent notification spam.
- **Side Drawer on Click:** Clicking an incident opens a drawer with:
  - Exact failure cause and diagnosis.
  - Recommended fix / remediation advice.
  - Notification status (shows whether an email was dispatched).

#### Step-by-Step Background Flow:
```
[Sentinel Check Engine runs checks (Freshness, Volume, Coverage, Recon, Schema Drift)]
       │
       ├─► Detects check failure
       ├─► Checks `sentinel.incidents` for existing open incident for same scope
       ├─► If new:
       │     ├─► Opens row in `sentinel.incidents` (status: open)
       │     ├─► Groups under synthetic parent if >=3 issues in same database (Rollup Engine)
       │     └─► Alert Bridge: Inserts row into `control_tower.alerts` with FK `alert_id`
       │           └─► Live `ct-alerter` sends email to engineering team
       └─► If previously failed check now passes:
             └─► Sets `resolved_at = NOW()` on the incident in `sentinel.incidents`

[Frontend UI]
       │
       └─► GET /api/sentinel/incidents ──► Renders list of open and historical incidents
```

---

## 3. Complete Database Schema Summary

All data resides in a single PostgreSQL database (`control_tower` in Cloud SQL `agenteye-pg`), organized into two clean schemas:

| Schema | Table | Purpose |
|---|---|---|
| `control_tower` | `registered_services` | Master inventory of all discovered Cloud Run services and jobs. |
| `control_tower` | `service_health` | Hourly operational snapshots (requests, 5xx errors, latency, CPU, instances, job status). |
| `control_tower` | `scheduler_jobs` | Master inventory of Cloud Scheduler cron jobs and target HTTP endpoints. |
| `control_tower` | `provider_status` | Status records for external platforms (OpenAI, GCP, Anthropic, BrightData balance). |
| `control_tower` | `cost_metrics` | Daily spend records by provider and SKU. |
| `control_tower` | `cost_budgets` | Monthly budget limits used for burn rate calculations. |
| `control_tower` | `alerts` | Central queue of operational and data quality alerts. |
| `sentinel` | `catalog_overlay` | AI-generated semantic dictionary, dedup rules, and grain for ClickHouse tables. |
| `sentinel` | `monitor_targets` | Registered check targets and expected update cadences. |
| `sentinel` | `variable_values` | Tracked business partition dimension values (brands, channels). |
| `sentinel` | `reconciliation_rules` | Cross-table consistency rules and promotion states (`observe` / `active`). |
| `sentinel` | `check_results` | Historical audit log of every check run by the check engine. |
| `sentinel` | `incidents` | Open and historical data quality incidents with rollup grouping. |
