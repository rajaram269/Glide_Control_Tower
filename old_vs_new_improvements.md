# Control Tower — Old Code vs. New Improvements (System Evolution)

> **Purpose:** A complete, plain-English technical breakdown of how the Control Tower codebase evolved. It compares the original MVP received from the previous developer with the modern, production-grade improvements made through AI pair programming (Claude & Antigravity).

---

## 1. Executive Summary: What Changed?

| Dimension | Old Original Code (Initial MVP) | New Improved Code (Current Development) |
|---|---|---|
| **Core Capability** | Basic infrastructure poller (GCP/AWS stats + simple status pages). | **Full Observability + AI Data Quality Suite (Sentinel)**. |
| **Job Monitoring** | Looked only at completed runs within the past hour; if a job ran earlier, fields were `NULL`. Missed schedules were invisible. | **Full Cloud Run Executions API integration + Missed Schedule Detection**. Tracks `succeeded`, `running`, `stuck`, `cancelled`, `missed`, and `failed`. |
| **Log Exploration** | Required logging into Google Cloud Console with IAM permissions. | **Built-in Cloud Logging explorer directly on the dashboard** with execution dropdowns and error filters. |
| **Service Health** | Only caught hard HTTP 5xx errors. Silent application crashes with 200 OK responses were invisible. | **Deep Service Monitoring**: Detects silent app crashes via internal error log counts and per-endpoint error breakdowns. |
| **Provider Health** | Basic public status pages only (often lagged or showed green during localized outages). | **Real API-level checks (e.g. live BrightData account balance & cost tracking)** alongside status pages. |
| **Database Data Quality** | Simple `watched_tables` table with coarse daily checks; crashed on non-replicated tables. | **Sentinel Subsystem**: AI multi-model cascade (OpenAI + Gemini + Claude), semantic catalog, reconciliation rules, coverage tracking, and dual freshness. |
| **Incident Management** | Unstructured alert table; alert spam on every hourly tick. | **Deduplicated alerts (4h window), Synthetic Incident Rollup (groups ≥3 issues)**, and 1-click UI acknowledgement/resolution. |
| **UI Stability & Aesthetics** | Mixed single table for both services and jobs; loaded React from external CDN (causing blank pages on network blips). | **Split Services/Jobs architecture, local vendored libraries (zero CDN dependency), Atlas Design System**, and interactive drawers. |

---

## 2. Detailed Evolution: Phase-by-Phase Breakdown

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ MILESTONE 1: Original MVP (Jun 2026)                                          │
│ • Initial GCP/AWS collectors, simple PostgreSQL schema, basic React UI.       │
└──────────────────────────────────────┬────────────────────────────────────────┘
                                       │ (Encountered permissions & stability issues)
                                       ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ MILESTONE 2: Infra Recovery & Job Visibility (Jul 2026)                       │
│ • Fixed IAM permissions (24-day silent outage fix).                           │
│ • Split Services vs. Jobs in backend and UI.                                  │
│ • Cloud Run Admin API integration (job_last_execution_at).                    │
│ • Auto-discovered 328+ mirrored ClickHouse tables.                            │
│ • Vendored UI dependencies locally (fixed blank screen on refresh).           │
└──────────────────────────────────────┬────────────────────────────────────────┘
                                       │ (Added AI Business Data Monitoring)
                                       ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ MILESTONE 3: Sentinel Subsystem Integration (Jul 2026)                        │
│ • AI Multi-LLM Catalog Discovery (OpenAI, Gemini, Claude).                    │
│ • Check Engine: Freshness, Volume, Schema Drift, Coverage, Reconciliation.    │
│ • Auto-Resolver for automated review triage.                                  │
│ • Dual-Source Freshness (Sync vs Data/Business-date).                         │
│ • Progressive scanning & per-table DB commits (timeout-proof).                │
└──────────────────────────────────────┬────────────────────────────────────────┘
                                       │ (Modern Production Hardening)
                                       ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ MILESTONE 4: Deeper Observability & Hardening (Sep 2026)                      │
│ • Silent Failure & Per-URL Endpoint Breakdown.                                │
│ • Missed Schedule & Stuck Job Alerting with Actionable Advice.                │
│ • Live Cloud Logging stream directly in Dashboard UI.                         │
│ • BrightData Live Balance & Cost Monitor.                                     │
│ • Region-agnostic discovery (all 38 GCP regions).                             │
│ • Removed dead/unadopted Pipeline page.                                       │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Deep-Dive: Old Code Limitations vs. New Solutions

---

### Feature 1: Cloud Run Jobs & Scheduled Cron Monitoring

#### How the Old Code Worked:
- The old collector used Cloud Monitoring metrics (`run.googleapis.com/job/completed_execution_count`).
- **The Major Flaw:** Cloud Monitoring only reports metrics if a job completed *within the scraping hour*. If a job runs once a day at 2:00 AM, by 3:00 AM the metric disappeared. The dashboard showed `NULL` or `-` for duration and last execution.
- If a Cloud Scheduler stopped triggering a job, the old system never noticed because it had no concept of the expected schedule.

#### How the New Code Works:
- Completely rewritten to use the **Google Cloud Run Executions API (`run_v2.ExecutionsClient`)** and **Cloud Scheduler API (`CloudSchedulerClient`)**.
- Stores the job's true completion timestamp (`job_last_execution_at`), which remains visible and accurate days after execution.
- **Missed Schedule Detector (`detect_missed_schedules`):** Automatically maps each job to its Cloud Scheduler cron expression (e.g. `0 4 * * *`). If the current time exceeds the next expected run window by >30 minutes without a new execution, the status flips to **`missed`** and immediately triggers an alert.
- Classifies jobs into **6 distinct statuses**: `succeeded`, `running`, `stuck`, `cancelled`, `missed`, and `failed`.
- Real-world impact: Caught 9 broken jobs the instant it went live, including two that had been failing silently since July (`holistique-location-cleanup` and `seed-all`).

---

### Feature 2: In-Dashboard Cloud Logging Explorer

#### How the Old Code Worked:
- If an engineer saw a failed job or an erroring service on the dashboard, they had to:
  1. Open Google Cloud Console in a new tab.
  2. Request Cloud IAM permissions.
  3. Navigate to Cloud Logging and construct complex log filter queries manually.

#### How the New Code Works:
- Added `/api/logs/{kind}/{name}` and `/api/logs/job/{name}/executions` endpoints.
- **Click-to-Expand Log Stream:** Clicking any job or service on the dashboard expands a live log viewer directly under the table row.
- **Job Execution Dropdown:** Allows selecting any run from the past 2 days to inspect historical failure stack traces.
- **Errors-Only Checkbox:** Filters logs to show only `ERROR`, `CRITICAL`, and `EMERGENCY` severity lines.
- **Raw JSON Formatter:** Automatically formats raw audit logs and JSON payloads into clean, readable text with expandable raw details.

---

### Feature 3: Service Health & Silent Failure Detection

#### How the Old Code Worked:
- Only inspected HTTP 5xx status codes from Cloud Monitoring.
- **The Blindspot:** Many modern microservices catch exceptions internally and return a `200 OK` with an empty response or an error payload. To Google Cloud, the service looked 100% healthy, even if the backend process completely crashed.
- An aggregate error rate hid broken endpoints: If a service had 10,000 requests to `/health` (100% success) and 50 requests to `/checkout` (100% fail), the overall error rate was only 0.5% (green).

#### How the New Code Works:
- **Silent Failure Monitor (`error_log_count`):** Scans Cloud Logging for internal application error logs (`severity >= ERROR`) that occurred during HTTP 200 responses. Alerts if error logs ≥ 3 while HTTP error rate < 5%.
- **Per-Endpoint URL Breakdown (`bad_endpoints`):** Normalizes URL paths (collapsing UUIDs/IDs into `{id}`) and calculates per-endpoint request and error counts.
- **Broken Endpoint Alerting:** Fires an alert if any specific endpoint suffers ≥ 50% error rate with ≥ 3 requests, even if the overall service is green.
- **Interactive UI Panel:** Clicking a service displays a dedicated URL breakdown table showing exact routes and failure rates.

---

### Feature 4: Database Connection Stability & Batch Commits

#### How the Old Code Worked:
- Collectors ran long loops (e.g. checking 38 regions, 35+ services, 450+ data tables) inside a single large database transaction, only calling `pg.commit()` at the very end of the script.
- **The Bug:** Cloud SQL PostgreSQL enforces a 60-second `idle-in-transaction` timeout. As the number of monitored services grew, the script spent more than 60 seconds making API calls, causing PostgreSQL to kill the database connection. The collector crashed, and zero data was saved.

#### How the New Code Works:
- **Phase-Based & Chunked Commits:** Code commits per phase (e.g. commit after discovery, commit after metrics) and commits in batches of 5 services.
- If an API call takes time or encounters a transient failure, all previous progress is safely committed to the database.

---

### Feature 5: Sentinel AI Data Quality vs. Legacy Data Freshness

#### How the Old Code Worked:
- Used a simple `watched_tables` table with hardcoded queries.
- Checked only when the table was last modified.
- Crashed when querying tables that did not have specific replication helper columns.

#### How the New Code Works (Sentinel Subsystem):
- **AI-Powered Discovery (`sentinel/discovery`):**
  - Analyzes all ClickHouse tables without manual configuration.
  - Multi-LLM cascade: Primary analysis by OpenAI GPT-4o, verified by Gemini 1.5 Pro, with Claude 3.5 Sonnet fallback.
  - Confidence scoring and Maker-Checker validation to prevent hallucinated metadata.
  - Auto-infers business concepts, primary keys, deduplication rules (ReplacingMergeTree `argMax`), and expected update cadences.
- **Dual-Freshness Checking:**
  - *Sync Freshness:* Checks partition modification timestamp via `system.parts`.
  - *Data Freshness:* Checks the actual maximum business event date (`max(event_date)`), catching cases where sync scripts run but source data is frozen.
- **Automated Reconciliation Engine:**
  - AI creates cross-table consistency rules (e.g., segment vs total, referential integrity).
  - Rules start in `observe` mode and automatically graduate to `active` after 3 consecutive successful checks.
- **Coverage Monitoring:**
  - Samples top business partition dimensions (brands, channels, regions).
  - Alerts if a brand or channel disappears from recent ingestions.

---

### Feature 6: Auto-Resolver & Human Triage Loop

#### How the Old Code Worked:
- Ambiguous tables or conflicting classifications were dumped into a review list that required a developer to manually review every single item in the database.

#### How the New Code Works:
- **Auto-Resolver (`sentinel/resolver`):** An automated AI adjudicator runs periodically to evaluate tables flagged as `needs_review`, resolving obvious ambiguities automatically.
- **Actionable UI Review Queue (`#review`):** Displays remaining uncertain tables with exact reasons for the flag.
- **Permanent Human Pinning:** When a human clicks `Confirm` or `Mark Static` in the UI, the record is tagged with `updated_by='human'`. Future AI discovery runs respect this flag and will never overwrite human decisions.

---

### Feature 7: UI Reliability & Atlas Design System

#### How the Old Code Worked:
- React and Preact libraries were imported at runtime from public CDNs (`esm.sh`).
- Any network hiccup, CDN latency, or browser ad-blocker caused the dashboard to render a completely blank white screen.
- Used a single monolithic table for both web services and cron jobs, cluttering the view with dashes (`-`).

#### How the New Code Works:
- **Zero Runtime Dependencies:** Vendored `preact.mjs`, `preact-hooks.mjs`, and `htm.mjs` locally in `ui/backend/vendor/`. The dashboard loads 100% offline and instantly.
- **Atlas Design System:** Fully styled with a modern, responsive design system supporting light/dark themes, metric cards, status badges, and expandable side drawers.
- **Split Navigation:** Distinct **Services** (traffic/latency) and **Jobs** (status/duration/schedule) pages for complete clarity.
- **URL Hash Routing:** URL reflects the active tab (e.g. `#jobs`, `#alerts`), allowing easy bookmarking and sharing.

---

### Feature 8: Dead Code & Redundant Page Removal

#### How the Old Code Worked:
- Included a `Pipeline` page intended to display row counts processed by backend jobs.
- Required every job in the company to adopt a special logging format (`CT_METRICS: {...}`).

#### Why It Was Removed:
- A live production audit revealed that **zero jobs** had adopted the convention after months of deployment.
- All critical job execution metrics (status, duration, failure logs, schedules) were already automatically collected by the new Cloud Run Executions integration.
- The `Pipeline` page, its API endpoint, and its background log scanner were safely removed to keep the codebase clean, lean, and maintainable.

---

## 4. Summary of Code Improvements by Component

| Component | Directory | Key Improvements Made |
|---|---|---|
| **GCP Collector** | `collectors/gcp_collector/` | • Region-agnostic discovery (all 38 GCP regions)<br>• Cloud Run Admin API for exact execution status<br>• Missed cron schedule detection<br>• Cloud Scheduler discovery & HTTP endpoint tracking<br>• Silent failure & bad endpoint log analysis<br>• BrightData live balance API check<br>• Per-phase DB commits |
| **Anomaly Detector** | `intelligence/anomaly_detector/` | • Decoupled from ClickHouse (queries PostgreSQL directly)<br>• Added checks for failed, missed, cancelled, and stuck jobs<br>• Added silent failure and broken endpoint alert rules<br>• 4-hour alert deduplication cooldown window |
| **Alerter** | `intelligence/alerter/` | • AI automated root-cause triage block in emails<br>• Microsoft Graph API + AWS SES dual email support<br>• Provider outage cross-checking<br>• Weekly cost digest automation |
| **Sentinel Discovery** | `sentinel/discovery/` | • Multi-LLM cascade (OpenAI -> Gemini -> Claude)<br>• Structure hash incremental scanning (skips unchanged tables)<br>• PII-safe distinct value sampling<br>• Progressive batch scanning (max 80 tables/run) with per-table DB commits |
| **Sentinel Check Engine** | `sentinel/check_engine/` | • Dual Freshness (Sync partition timestamp + Data event date)<br>• Volume drop anomaly detection<br>• Variable dimension coverage validation<br>• Deduplicated SQL reconciliation (`argMax`)<br>• 3-run observe-to-active rule promotion<br>• Alert bridge & synthetic incident rollup |
| **Sentinel Auto-Resolver** | `sentinel/resolver/` | • Automated secondary AI adjudication for review queue<br>• Conflict resolution & human pin protection |
| **Dashboard UI** | `ui/backend/` | • Local Preact/htm vendoring (no CDN failure risk)<br>• Split Services / Jobs views with 3-way column sorting<br>• Live Cloud Logging viewer with execution picker<br>• Per-URL endpoint breakdown panel<br>• Rich side drawers for alerts, incidents, freshness, catalog<br>• 1-Click alert ack, review resolve, and static toggle |
