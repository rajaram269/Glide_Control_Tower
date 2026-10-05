# Things Left To Fix — Explained Simply

This is my understanding of each open item you listed, in plain English, based on
reading the actual code. See [document.md](document.md) first if you haven't read the
project overview yet.

## Quick status

| # | Item | Status |
|---|---|---|
| 1 | Provider API health check + alerting | 🟡 IN_PROGRESS |
| 2 | Job status check — logs | 🟢 DONE |
| 2 | Job status check — hide deleted services/jobs/schedulers | 🟢 DONE |
| 2 | Job status check — reverify status logic (stuck/cancelled/missed runs) | 🟢 DONE |
| 2 | Job status check — trigger alerts | 🟢 DONE |
| 2 | Job status check — correction steps | 🟢 DONE (for these 4 alert types) |
| 3 | Pipeline page redundancy | 🟢 DONE (removed) |
| 4 | Deeper service monitoring (silent failures) | 🟢 DONE |
| 5 | Sentinel reconciliation alerting | 🔴 PENDING |
| 6 | Sentinel freshness cleanup | 🔴 PENDING |

🟢 DONE · 🟡 IN_PROGRESS · 🔴 PENDING

---

## 1. Provider API health check and alerting — currently a placeholder

**STATUS: IN_PROGRESS**

*Real progress (updated):* 4 providers now have genuine account-level checks,
not placeholders — confirmed live on the dashboard:
- **BrightData** — real balance ($91.69)
- **Capsolver** — real balance ($8.41)
- **Hiker (HikerAPI)** — real balance ($132.97)
- **Gemini** — real reachability check (can the key list its models)
- **OpenAI** — a balance check was added too, but it's currently showing
  "degraded" with no balance figure — worth a quick look at why (likely needs
  an Admin-tier API key, same issue the cost collector hit for OpenAI/Anthropic
  cost tracking).

**One gap worth knowing:** the endpoint-level error descriptions on the Services
page use an OpenAI call to turn a raw error into a plain-English sentence — but
`ct-ui` doesn't have `OPENAI_API_KEY` set, so in production this always falls
back to the raw error line, silently. Harmless (nothing breaks), just means the
AI-summary feature isn't actually active yet despite being built.

**What it means:** know when OpenAI, ClickHouse, AWS etc. are failing *for us*
specifically, and get alerted.

**What's still a placeholder:** ClickHouse and AWS aren't checked at all yet.
The separate "scan our own logs for API errors" attempt still has the same
original bug — it only ever counts errors, never a total, so a rate can't be
computed and that specific alert can never fire.

**What "done" looks like:** ClickHouse and AWS get the same real-check
treatment as BrightData/Capsolver/Hiker/Gemini, the OpenAI balance check
actually returns a number, and an alert fires when any of them look bad.

---

## 2. Job status check logic — reverify, logs, alerts, correction steps

**STATUS: IN_PROGRESS**

- **Logs to the frontend — STATUS: DONE.** You can now click a job or service row on
  the dashboard and read its Cloud Logging entries directly, no Google Cloud console
  access needed.
- **Hide deleted services/jobs/schedulers — STATUS: DONE.** A service, job, or
  scheduler that no longer exists in Google Cloud now disappears from the dashboard
  automatically instead of lingering forever.
- **Reverify the status logic — STATUS: DONE.** Jobs now show 3 new statuses beyond
  succeeded/failed: **running** (currently in progress, normal), **stuck** (running
  far longer than this job normally takes), and **cancelled** (Cloud Run itself
  cancelled it). Separately, each job's own cron schedule is now compared to when it
  actually last ran, so a schedule that silently stopped firing shows as **missed**.
  Caught a real one immediately: `purplle-appointment-booking` hasn't run since
  Sept 23 — 5 days overdue — and was invisible on the dashboard before this fix.
- **Trigger alerts — STATUS: DONE.** A new check emails whenever a job's *current*
  status is failed, missed, cancelled, or stuck (not just "ran slow"). Verified live:
  found 9 real broken jobs the moment this went live — including 2 silently failing
  since **July** (`holistique-location-cleanup`, `seed-all`) — and all 9 real emails
  sent successfully.
- **Correction steps — STATUS: DONE (for these 4 alert types).** Each of the 4 new
  alert types has its own plain-English "what to do about it" text on the Alerts
  page — e.g. missed → check the Cloud Scheduler trigger; stuck → check for a hang,
  consider cancel+rerun. Older alert types (error rate spike, job duration drift,
  EC2 pressure) still fall back to a generic message — not covered by this pass.

---

## 3. Pipeline page seems redundant — assess and remove if no value

**STATUS: DONE — removed.**

Checked live before removing: **0 rows**, ever. No job had adopted the required
`CT_METRICS:` log-line convention. Removed: the Pipeline tab, its `/api/pipeline`
endpoint, the collector's Cloud Logging scan for it, and the now-dead
`docs/ct_metrics_convention.md` guide. Left the empty `pipeline_events` database
table untouched (not asked to drop it). If a job ever wants to report row-counts
in the future, that's a small feature to re-add on its own, ideally as a column
on the Jobs page rather than a separate page.

---

## 4. Service monitoring needs to go deeper — silent failures

**STATUS: DONE**

**What it means:** today a service only counts as "broken" if it returns a hard
server error (a 5xx). Two real failure patterns slip through that entirely:
- **Silent failure:** the service answers "200 OK" (looks fine) but the actual work
  inside failed, was skipped, or returned an empty/wrong result.
- **One broken endpoint hiding in the average:** if one specific request type (say,
  `/export`) always fails, but it's a small slice of total traffic, the overall error
  rate stays low enough that nothing turns red and no alert fires.

**What was built:**
- **Per-endpoint breakdown** — click any service on the Services page to see request
  and error counts broken down by URL, not just one overall number. Uses data Cloud
  Run already logs for every request — no changes needed to any service's own code.
  Proven on a real example: `report-platform-api`'s "57% error rate" turned out to be
  only 2 of 15 endpoints actually broken (`/api/runs/worker` at 76-84%,
  `/api/schedules/dispatch` at ~20%) — the other 13 were completely healthy.
- **Silent-failure alerting** — a new check counts each service's own internal error
  logs; alerts if a service is logging real errors (≥3) while its HTTP error rate
  still looks fine (<5%). Checked honestly before building: only 1 of 34 services
  currently logs errors this way, so it won't catch anything *new* today — but it
  needs no further work to start catching a service the moment it begins logging
  errors.
- **Broken-endpoint alerting** — alerts when one specific endpoint has ≥3 requests
  and ≥50% failing, even while the service overall looks healthy.
- **Real finding along the way:** while validating this, found that
  `report-platform-api` is repeatedly hitting its container memory limit (4096 MiB
  limit, actually using 4109-4212 MiB) — very likely the real root cause of its
  worker failures. Worth fixing the memory limit or the underlying leak.
- **A real bug found and fixed during this work:** the collector's per-service loop
  got slower (one extra log check per service) and started tripping the same
  60-second idle-database-connection limit fixed earlier this session, in a new
  place. Fixed by committing every 5 services instead of only at the very end;
  verified clean on 2 consecutive real runs afterward.

---

## 5. Sentinel — reconciliation logic needs to be clearly defined, and flagged data needs alerts

**STATUS: PENDING**

**How it actually works right now:** an AI (not a person) automatically writes rules
like "the total in this table should roughly match the sum of the same numbers in
that other table." Every new rule starts in a trial mode called `observe` — it runs
and gets logged, but nothing is sent to anyone yet. Only after a rule has agreed with
reality 3 times in a row does it get promoted to `active`. **Only `active` rules can
trigger an alert.**

**Why this needs a closer look:** since brand-new rules always start in `observe`,
a genuinely wrong number found on a freshly-checked table can sit there — logged, but
invisible to everyone — for as long as it takes to reach 3 clean runs (which, if the
data stays wrong, might never happen; if a rule keeps failing, it never gets to
`active`, so it can never alert either way). That's the opposite of what you'd want:
the newest, least-trusted data is exactly the case where nobody gets told about a
problem. This looks like it was a deliberate choice to avoid false-alarm spam while
Sentinel was new, but it's worth deciding explicitly: should a serious mismatch on an
`observe` rule ever alert (maybe at a lower urgency), or is silence really intended
until a rule "graduates"?

**What "done" looks like:** a clear, written-down decision on when a reconciliation
mismatch should notify someone, and the code changed to match that decision — not the
current default of "new problems are invisible by design."

---

## 6. Sentinel — freshness logic is messy at the variable level

**STATUS: PENDING**

**What "variable level" means:** Sentinel doesn't just track "is this whole table
up to date" — for some tables it also tracks up to 3 individual columns inside that
table (e.g. brand name) as their own separate tracked things, called "variables,"
each with its own copy of the same settings (how often to check, how stale is too
stale).

**The actual mess I found:** the freshness check itself (is the data current) is
**only ever run at the whole-table level** — it never looks at those per-variable
rows at all. But per-variable rows still exist in the same settings table with their
own copies of "how often" and "how stale is bad," which are never used for freshness
and only matter for a *different* check (are all the expected values still showing
up). So there are extra rows with unused settings living alongside the real ones,
in the same table, which makes it confusing to read and easy to misconfigure.

**One more thing worth knowing:** there are currently *two separate* freshness
systems running side by side on purpose — an older, simpler one, and this newer
Sentinel one — while the team confirms the new one gives the same answers as the old
one on real data before retiring the old one. That part is intentional, not a bug.

**What "done" looks like:** separate "how often do we check" from "how stale counts
as a problem" cleanly (the code has already tried to fix this once with limited
success), stop creating per-variable rows that the freshness check ignores, and once
the new system is confirmed to agree with the old one, retire the old one.

---

## Suggested order

Rough priority, based on impact and how contained each fix is:

1. **#5 (reconciliation alerting)** — smallest, clearest decision to make, and it's
   about real data problems staying silent, which is the whole point of Sentinel.
2. **#4 (silent service failures)** — currently zero visibility into a known real
   failure mode.
3. **#1 (provider health)** — currently structurally broken (can never alert).
4. **#2 (job alerts + correction steps)** — logs and deleted-item filtering are done;
   alerting is the next piece.
5. **#6 (freshness cleanup)** — a design cleanup, not a live gap; lower urgency.
6. **#3 (pipeline page)** — a simple decision + cleanup, do whenever convenient.

Happy to plan any one of these in detail before writing code — just say which.
