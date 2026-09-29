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
| Services | Always-on apps: traffic, speed, error rate |
| Jobs | Scheduled/batch jobs: did they run, did they succeed, click a row for its logs |
| Providers | Is OpenAI/Anthropic/GCP itself having an outage right now |
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
- **Deploying to production:** `scripts/deploy.sh` rebuilds and redeploys everything.
  `scripts/bootstrap.sh` is only for setting the whole thing up from scratch on a new
  Google Cloud project — you won't normally need this one.

## 7. Where it actually runs

Everything lives in one Google Cloud project (`seoai-479305`). The dashboard and all
the background jobs run there, and they all connect to one Postgres database
(`agenteye-pg`). ClickHouse (the business data) is a separate, already-existing system
that Control Tower only reads from.

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
- `migrations/` — every database change, in order; read these if you want to know
  exactly what data exists.
