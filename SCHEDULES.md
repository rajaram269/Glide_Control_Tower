# Control Tower — what runs when

Last updated: 2026-10-18. **All times are IST (Asia/Kolkata).** Run times below are measured from each
job's last ~40 real executions ("typical" = median, "longest" = worst seen). Everything lives in the
Google Cloud project `seoai-479305`, region `asia-south1`.

Each background job is a **Cloud Run Job** (`ct-…`) started by a **Cloud Scheduler** trigger named
`trigger-ct-…` (same region, time zone Asia/Kolkata). The dashboard is one always-on **Cloud Run service** (`ct-ui`).

## 1. At a glance — every job

| Job | What it does | Runs (IST) | Typical time | Longest seen | Time limit | Retries | Health now |
|---|---|---|---|---|---|---|---|
| `ct-gcp-collector` | Pulls metrics for all Cloud Run services/jobs, polls provider status pages, checks API health in the logs, checks key table freshness (old system), discovers new services | **Every hour at :00** | 5.0 min | 6.7 min | 10 min | 2 | OK |
| `ct-aws-collector` | Pulls CloudWatch metrics for registered EC2 instances | **Every hour at :00** | — | — | 10 min | 2 | **Failing every run** (see §5) |
| `ct-anomaly-detector` | Reads the database and runs 8 anomaly checks, writes alerts | **Every hour at :30** | 18 sec | 27 sec | 10 min | 2 | Fixed 2026-10-18 (was failing ~1 run in 4; see §5) |
| `ct-alerter` | Sends email for new alerts; adds an AI triage paragraph for critical ones | **Every hour at :45** | 25 sec | 46 sec | 10 min | 2 | OK |
| `ct-cost-collector` | Collects spend (GCP billing export, AWS, ClickHouse Cloud, OpenAI, Anthropic, Cohere) and runs cost anomaly checks | **Daily 06:00** | 23 sec | 42 sec | 10 min | 2 | OK |
| `ct-alerter-weekly` | Weekly cost-digest email (same code as the alerter with `WEEKLY_DIGEST=true`) | **Mondays 09:00** | 15 sec | 42 sec | 10 min | 2 | OK |
| `ct-sentinel-discovery` | Catalogs each table once: description, event-date column, monitored variables, dedup rule (uses the LLMs — see [LLM_USAGE.md](LLM_USAGE.md)) | **Daily 06:45** | 48 min | 4 h* | 2 h | 1 | OK (80 tables per run) |
| `ct-sentinel-resolver` | Settles "Needs Review" tables with a third AI opinion | **Daily 07:10** | 2.6 min | 17 min | 1 h | 1 | OK |
| `ct-sentinel-check` | Freshness (Sync + Data, per table and per platform) for every table; volume, schema drift, coverage and reconciliation when due; opens/closes incidents and alerts | **Daily 07:30, 13:30, 19:30** | 41 sec** | 47 min | 1 h | 1 | OK |
| `ct-ui` *(service)* | The dashboard and its API | **Always on** (1–2 instances) | — | — | 5 min per request | — | OK |

\* The longest discovery run includes the automatic retry after a failed first attempt and the one-time catch-up passes in October.
\*\* When nothing is due. The **first run of each day (07:30)** re-judges every table's freshness and takes about **25 minutes**; the 13:30 and 19:30 runs then only pick up leftovers.

## 2. A typical day, hour by hour

| Time (IST) | What starts | Runs for |
|---|---|---|
| **Every hour :00** | GCP collector (and the AWS collector, which currently fails) | ~5 min |
| **Every hour :30** | Anomaly detector | ~20 sec |
| **Every hour :45** | Alerter (emails for new alerts) | ~25 sec |
| **06:00** | Cost collector | ~25 sec |
| **06:45 (Mondays only)** | Sentinel discovery | ~50 min |
| **07:10** | Sentinel resolver | ~3 min |
| **07:30** | Sentinel check #1 — refreshes **every** table's freshness | ~25 min |
| **09:00 (Mondays only)** | Weekly cost-digest email | ~15 sec |
| **13:30** | Sentinel check #2 — leftovers only | seconds |
| **19:30** | Sentinel check #3 — leftovers only | seconds |

On Mondays the discovery job (06:45, ~50 min) is still running when the 07:30 check starts. They use separate locks and do not
block each other, but the database is busier for that half hour.

## 3. What each job feeds on the dashboard

| Job | You see its result in |
|---|---|
| GCP collector | **Overview**, **Services**, **Jobs**, **Providers** tabs; "Last data … ago" in the top bar |
| AWS collector | EC2 rows on Services/Jobs |
| Anomaly detector | **Alerts** tab (new alerts), then emailed by the alerter |
| Alerter | Alert emails (hourly) |
| Cost collector / weekly digest | **Costs** tab; the Monday digest email |
| Sentinel discovery | **Catalog**, **Needs Review**, and the *Event date column* / *Monitored variables* columns on **Freshness** |
| Sentinel resolver | **Needs Review** (rows disappear when auto-confirmed) |
| Sentinel check | **Freshness**, **Coverage**, **Reconcile**, **Incidents**, and the alerts those raise |

## 4. How to run something now, and where to look

* **Dashboard → Jobs → ▶ Run** (needs the run token; see `document.md`). Only one run of a job at a time, and one start per job per minute.
* Command line:
  `gcloud run jobs execute <job-name> --region=asia-south1 --project=seoai-479305`
  Add `--wait` to hold the terminal until it finishes.
* Re-judge **every** freshness verdict right now (about 25 min):
  `gcloud run jobs execute ct-sentinel-check --update-env-vars SENTINEL_FORCE_REFRESH=1 --region=asia-south1 --project=seoai-479305`
* Logs: **Dashboard → Jobs → click the job row** (read live from Cloud Logging), or the Cloud Run console.
* A schedule is changed in Cloud Scheduler (`trigger-ct-…`, location `asia-south1`). Always set the time zone to **Asia/Kolkata** and write the cron in IST.

## 5. Known issues (as of 2026-10-18)

| Issue | Effect | Cause |
|---|---|---|
| `ct-aws-collector` fails **every** run | A red failed job every hour; no EC2 metrics | It reads a column `ec2_instance_id` from `registered_services`, but that column exists only on `service_health` (`collectors/aws_collector/main.py:64`). **This is the original code**: identical on `main` and `development`, and only ever touched by the first commit (2026-06-19). **Not changed — decision pending** |
| ~~`ct-anomaly-detector` fails about 1 run in 4~~ **FIXED 2026-10-18** | It failed in blocks of 5-6 hourly runs every night (about 02:30-07:30 IST); every alert from a failed run was lost, and the checks `error_rate_spike`, `api_degradation`, `ec2_resource_pressure` and `silent_failure` had never saved an alert | It saved each alert's details as JSON and crashed on database numbers of type `Decimal`. The bug is in the original code on `main` too. Fixed by converting `Decimal` to a number when saving (`_json_default`) |
| `ct-gcp-collector` uses 5–7 min of a 10 min limit | If it gets slower it will time out and be retried | Hourly collection has grown (more services/tables) |
| OpenAI credits exhausted (2026-10-18) | Catalog runs fall through to Gemini/Haiku (dearer, slower); dashboard shows raw API-error text | Recharge the OpenAI account; see [LLM_USAGE.md](LLM_USAGE.md) |

*Fixed on 2026-10-18:* the anomaly detector (see above); the cost collector and weekly digest were running at 00:00 IST and Monday 03:15 IST (their crons were written for UTC);
they now run at **06:00 IST daily** and **Monday 09:00 IST** as documented.

## 6. Not covered here

* About 70 other Cloud Scheduler jobs in the same project belong to **other pipelines** (Zepto/Flipkart/Nykaa syncs, GRN automation, appointment booking …). They are not part of Control Tower; roughly 20 of them are defined in UTC.
* `scripts/deploy.sh` creates schedules **only if they do not exist** and without a time zone (UTC). It does not overwrite the IST schedules above — change a schedule in Cloud Scheduler, not by re-running the script.
