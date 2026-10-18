# Control Tower — What This Project Is (Read This First)

This is a simple explainer for someone new to the project. No jargon left unexplained.

## 1. What is this, in one sentence?

Control Tower watches all of the company's other running systems (websites, background
jobs, data pipelines, cloud spend) and tells engineers by email when something breaks,
stops updating, or costs too much — so nobody has to manually check ten different
dashboards.

## 2. The big picture

Think of it as 4 parts, all sharing one database:

1. **Collectors** — go out and check things (Google Cloud, Amazon Web Services, cloud
   bills) and save what they find.
2. **Intelligence** — reads what the collectors saved, decides what looks wrong, and
   sends the email alerts.
3. **Sentinel** — a newer, separate part. Instead of watching servers, it watches the
   *business data* itself (sales numbers, orders, etc.) sitting in a database called
   ClickHouse. It uses AI to figure out what each data table means and checks that the
   numbers stay sensible and up to date.
4. **The dashboard (this website)** — the one screen you look at. It only reads from
   the shared database. It never talks to Google Cloud or ClickHouse directly.

```
Google Cloud, AWS, ClickHouse, cloud bills
        │  (collectors check these)
        ▼
   one shared database (Postgres)
        │                    │
        ▼                    ▼
  Intelligence          Sentinel (AI)
  (decides what's           │
   wrong)                   ▼
        │             writes findings back
        ▼             to the same database
   sends email alerts
        │
        ▼
   Dashboard (you) — reads everything from that one database
```

**Systems it touches:** Google Cloud (where most of the company's apps and jobs run),
Amazon Web Services, ClickHouse (a separate database used for analytics/sales data),
and Outlook/Microsoft 365 (to send the alert emails).

## 3. The 4 parts, one paragraph each

- **`collectors/gcp_collector`** — Runs once an hour. Checks every app and scheduled
  job on Google Cloud: is it running, how fast, did it fail? Also checks if OpenAI,
  Anthropic and other providers are having outages.
- **`collectors/aws_collector`** — Same idea, once an hour, but for anything running on
  Amazon Web Services.
- **`collectors/cost_collector`** — Runs once a day. Adds up how much money was spent
  yesterday across Google Cloud, AWS, ClickHouse, and AI services, and checks it
  against a budget.
- **`intelligence/anomaly_detector`** — Runs once an hour. Looks at everything the
  collectors saved and decides: is this normal, or is something actually wrong?
  Writes anything wrong into an "alerts" list.
- **`intelligence/alerter`** — Runs once an hour, a bit after the detector. Takes that
  alerts list and emails the right people. Uses AI to write a short summary for the
  serious ones.
- **`sentinel/discovery`** — Runs once a week. Looks at every table of business data in
  ClickHouse and uses AI to figure out what it means, whether it's trustworthy, and
  how to check it for problems. Writes this into a "catalog" — like an index of what
  data exists.
- **`sentinel/check_engine`** — Runs once a day. Uses that catalog to actually check the
  data: is it fresh, does it have duplicates, does the row count look normal, do two
  systems that should agree on a number actually agree?
- **`sentinel/resolver`** — Runs once a week. When the AI in `discovery` wasn't fully
  sure about something, this uses a second AI pass to double-check it, so a human
  doesn't have to look at every uncertain case.
- **`ui/backend`** — This dashboard. A single web page that shows everything above.

## 4. The database

Everything is stored in one Postgres database, split into two sections:

**`control_tower`** — the "is the system running OK" section:
- `registered_services` — the list of things being watched (every app, job, server).
- `service_health` — hour-by-hour snapshot: was it up, how many errors, how slow.
- `cost_metrics` — how much was spent, per day, per provider.
- `alerts` — every problem found, waiting to be (or already) emailed out.

**`sentinel`** — the "is the business data OK" section:
- `catalog_overlay` — the AI's notes on what each data table means.
- `monitor_targets` — which tables are actively being checked, and how.
- `incidents` — a live problem: opened when something's wrong, closed when it's fixed.
- `reconciliation_rules` — rules like "sales in System A should match sales in System B".

## 5. What each page on the dashboard shows

| Page | What it shows |
|---|---|
| Overview | Quick summary: how many alerts, failing jobs, things needing review |
| Services | Always-on apps: traffic, speed, error rate. Click any service to see a per-endpoint breakdown (which specific URL is actually broken, not just one overall number) with its own logs and a plain-English reason per failing endpoint |
| Jobs | Scheduled/batch jobs: did they run, did they succeed, click a row for its logs |
| Providers | Is OpenAI/Anthropic/GCP having an outage, plus real account checks for BrightData, Capsolver, Hiker and Gemini (actual balance/API-key health, not just a public status page) |
| Alerts | History of every problem found, with a button to mark it handled |
| Costs | Money spent vs budget |
| Catalog | The AI's description of every business-data table |
| Needs Review | Tables the AI wasn't sure about — needs a human to confirm |
| Coverage | Are expected values (e.g. every brand name) still showing up in the data |
| Reconcile | Do numbers that should match across two systems actually match |
| Freshness | Is each data table still being updated, or has it gone stale |
| Incidents | Open and past data problems found by Sentinel |

## 6. How to run it

- **On your own laptop:** follow [docs/RUN_LOCAL.md](docs/RUN_LOCAL.md). It sets up a
  throwaway database with sample data — no company cloud access needed. Good for
  trying out dashboard changes.
- **Re-running a job without GCP access:** on the Jobs page every job has a **▶ Run**
  button (for re-running after a code fix, since the pipeline deploys from git). The
  dashboard has no login, so the button is **disabled until a run token is configured**:
  create the secret `ct-run-job-token`, give it to `ct-ui` as env `RUN_JOB_TOKEN`
  (`scripts/deploy.sh` does this automatically when the secret exists), and grant the
  `ct-ui` service account `roles/run.developer`. Developers enter the token once per
  browser session. The backend only starts registered, active jobs, never one that is
  already running, allows one start per job per minute, and logs every attempt
  (`RUN STARTED` / `RUN DENIED` in the `ct-ui` logs).
- **Deploying to production:** `scripts/deploy.sh` rebuilds and redeploys everything.
  `scripts/bootstrap.sh` is only for setting the whole thing up from scratch on a new
  Google Cloud project — you won't normally need this one.

## 7. Where it actually runs

Everything lives in one Google Cloud project (`seoai-479305`). The dashboard and all
the background jobs run there, and they all connect to one Postgres database
(`agenteye-pg`). ClickHouse (the business data) is a separate, already-existing system
that Control Tower only reads from.

## 7a. How Sentinel decides things (current rules)

- **Each table is classified once.** Discovery (weekly, Monday 06:45 IST) looks at the
  full column list plus real data profiles (min/max/future dates per date column,
  common values per text column) and records the table's **event date column** (the
  business date, e.g. `SalesDate`) and up to 3 **monitored variables** (e.g. Platform).
  A table is only re-classified if its columns change, a human pin exists (pins always
  win), or `CLASSIFIER_VERSION` in `sentinel/discovery/main.py` is bumped.
- **Event date is verified against the data**, not just the AI's word: a column with no
  real recent values (e.g. all `1970-01-01`) is rejected. Variables are ordered
  Platform, Brand, Channel, Region...; Type/Status/Flag-style columns are only used
  when nothing better exists; identifier-style names (`*_id`, `*_no`) never are.
- **Fresh / Stale / Dead** use the table's expected update interval. Data freshness
  (business date, per table and per platform) and Sync (pipeline) both never judge on less
  than 3 days (`DATA_MIN_DAYS` / `SYNC_MIN_DAYS` in `sentinel/check_engine/main.py`): Fresh
  up to the window, Stale up to 3x it, Dead beyond. **Only the last 12 months are ever
  judged** (`LOOKBACK_DAYS`): a table, or a platform inside one, with no data in 12 months
  is **Inactive** (grey, never alerted). A table's Data badge is the worst of its own newest
  date and its per-platform check. The Freshness tab shows a legend with these definitions.
- **How a variable (e.g. Platform) is judged, value by value** (`judge_values` in
  `sentinel/check_engine/main.py`): values with no data in the last 12 months are **inactive** and
  ignored; values that stopped on the same date as at least 3 others (within 45 days) are
  **in a batch** (e.g. retail platforms delivered once a month) and count as on time; if no
  active value is current the **whole table stopped** and only the table check reports it.
  Variables named like individual entities (campaign, ad group, keyword, portfolio,
  journey, report name, SKU/ASIN/merchant codes) are never tracked or judged - a finished
  campaign is not a failure.
- **Per-platform rhythm:** each platform value gets its own expected cadence
  (`variable_values.expected_cadence_weeks`, migration 022) so slow reporters are not
  judged on the daily yardstick.
- **Checks run 3x a day** (07:30 / 13:30 / 19:30 IST). Volume, schema drift, coverage and
  reconciliation only run on targets that are due (every 1-4 weeks). **Freshness is
  refreshed daily for every active table/variable**: the first run of each IST day
  (07:30) re-runs every target not yet refreshed today, within a 25-minute budget per run
  (leftovers carry to the next run), so a verdict on the Freshness tab is never more than
  about a day old.
- **All displayed times are IST** (dashboard, alert emails, Cloud Scheduler). Internal
  logic and Cloud Logging stay on UTC.
- **AI models:** discovery/resolver cascade is OpenAI `gpt-5-mini` -> Gemini
  `gemini-2.5-flash` -> Anthropic `claude-haiku-5-5` (cheapest, last fallback). The
  alerter's triage also uses `claude-haiku-5-5`. No Sonnet is used anywhere. Providers-tab
  and cost-collector Anthropic calls use no tokens (free).
- "Not checked yet" on the Freshness tab mostly means the table is in the old watch list
  but no longer exists in ClickHouse (a leftover), or discovery has not reached it yet.

## 8. Things to know before you touch anything

- **The dashboard has no login.** Anyone with the link can open it. This was a
  deliberate choice made earlier, not an oversight — worth confirming it's still what
  you want before adding anything sensitive to it.
- **Two different "is the data fresh" systems exist side by side on purpose.** An older,
  simpler one and the newer Sentinel one. This is intentional overlap during a
  transition — don't delete the older one without checking first.
- **The `sentinel/resolver` folder is missing two files on a fresh checkout.** They get
  copied in automatically during deployment (`scripts/deploy.sh`) and aren't stored in
  git. If you try to run it locally straight after cloning, it won't work until that
  copy step runs.
- **The alert emails are sent from one fixed address**, because of a Microsoft email
  policy — changing that address in the code will quietly break email sending.
- **"Deployed" doesn't always mean "definitely working."** This project's history
  (`BUILD_PROGRESS.md`) has real examples of things being live but broken for weeks
  before anyone noticed (e.g. a wrong Google Cloud permission silently broke every
  scheduled job for over three weeks). When in doubt, check that a feature is actually
  producing data, not just that it deployed without an error.
- **The AI parts can silently degrade.** If one AI provider changes something, the
  system quietly falls back to a different one. If the "Catalog" page ever looks thin
  or empty, that's worth investigating rather than assuming it's fine.

## 9. Where to look next

- `BUILD_PROGRESS.md` — a dated log of everything built and every bug found so far.
  Worth skimming top to bottom once.
- `docs/RUN_LOCAL.md` — step-by-step for running this on your own machine.
- `LLM_USAGE.md` — every LLM call in the project: which model does what, fallback order, prices and status.
- `SCHEDULES.md` — every Control Tower job and the dashboard service: what runs when (IST), how long it takes, known issues.
- `migrations/` — every database change, in order; read these if you want to know
  exactly what data exists.
