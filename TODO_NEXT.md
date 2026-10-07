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
| 6 | Sentinel freshness cleanup | 🟡 IN_PROGRESS |
| 7 | Freshness checks include deleted/tombstone rows (new, found 2026-10-06) | 🟢 DONE |

🟢 DONE · 🟡 IN_PROGRESS · 🔴 PENDING

---

## 7. Freshness checks don't filter out deleted rows — found while investigating a weird date

**STATUS: DONE**

**What happened:** `Holistique_Base` showed a "newest data" date of Dec 8, 2026 — about 2 months in the future. Checked the real source (MySQL): that date doesn't exist there at all. Root cause, confirmed directly in ClickHouse: those rows have `_peerdb_is_deleted = 1` — they're deleted/tombstone rows from the CDC replication, not live data. The convention everywhere else in this project is `_peerdb_is_deleted = 0` = the real, live data.

**The fix — made universal, not just this one spot:** decided this should apply everywhere, not just this one function — any ClickHouse read, any database, any table, should only ever consider `_peerdb_is_deleted = 0` rows. Added a small helper (`_live_only_clause`, checks whether the table even has that column, cached per table per run) and applied it everywhere ClickHouse is read for live data: freshness (`_maxcol_freshness`), variable coverage, the no-dedup fallback paths of reconciliation's count/sum helpers, and discovery's segment-cardinality/segment-value sampling. Deliberately left `check_volume` alone (it reads `system.parts` row counts, which is a different, cheaper mechanism than scanning the table — filtering there would mean a full table scan every day; row-count drift already gets caught by the volume-drop alert, so the correctness gain wasn't worth the cost).

**Deployed:** `ct-sentinel-check`, `ct-sentinel-discovery`, and `ct-sentinel-resolver` (discovery's fix propagates into resolver via the existing file-copy step) are all live with this fix as of 2026-10-06.

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

**STATUS: IN_PROGRESS**

**What "variable level" means:** Sentinel doesn't just track "is this whole table
up to date" — for some tables it also tracks up to 3 individual columns inside that
table (e.g. Platform, Brand) as their own separate tracked things, called "variables,"
each with its own copy of the same settings (how often to check, how stale is too
stale).

**The original mess:** the freshness check itself (is the data current) was
**only ever run at the whole-table level** — it never looked at per-variable rows at
all. A table like `Holistique_Base` could keep looking "fresh" overall because most of
its ~40-50 platforms keep syncing, while one specific platform's feed quietly died —
invisible, because nothing checked per-platform recency.

**What was built (2026-10-06):** a new check, `variable_freshness`, alongside the
existing `variable_coverage`. Coverage asks "does this value still exist anywhere in
the table" (set-difference against recent data); freshness asks "is each value's *own*
data still current" (per-value `max(event_date)`, classified fresh/stale/dead the same
way table-level freshness is). Cost stays bounded on purpose:
- **One query per tracked variable per table**, not one per value — a single
  `GROUP BY` gets every platform's/brand's last-seen date in one pass.
- **One alert per variable**, never one per value — never "40 platforms × 4 brands =
  160 alerts." A variable with several stale/dead values produces one incident listing
  which values are affected and what % of the tracked set that is.
- Severity scales with how much is affected (a couple of stale values → warn; a large
  chunk dead → fail), same pattern as `variable_coverage`.

This directly targets the ~40-50 platform × 3-4 brand scale for `Holistique_Base`
without exploding alert volume — each of Platform and Brand gets exactly one
freshness verdict per run, regardless of how many values each one has.

**Deployed, but blocked on one step:** `ct-sentinel-check`'s image with this new check
was built, but **rolled back to the previous image** before going live — the new
check writes `check_type = 'variable_freshness'`, which isn't yet in the database's
CHECK constraint (`sentinel.check_results`), so every run would crash the instant it
hit a variable target. Migration `021_sentinel_variable_freshness.sql` widens that
constraint. **This needs you to run it** (see "Needs your action" below) — I don't have
write access to the Postgres secret this session. Once it's applied, redeploy with:
`gcloud run jobs update ct-sentinel-check --image="gcr.io/seoai-479305/ct-sentinel-check@sha256:262e7245a363a8bcd09ef707fdf73da186c1dd2e8ea209d2e62af34a155b5519" --project=seoai-479305 --region=asia-south1`

**Still not done from the original ask:** separating "how often do we check" from
"how stale counts as a problem" cleanly for per-variable rows, and retiring the old
`control_tower.data_freshness` system once the new one is confirmed to agree with it.
There are currently *two separate* freshness systems running side by side **on
purpose** — don't delete the old one without checking first.

---

### Needs your action — can't do these myself this session

**1. Apply migration 021** (adds `variable_freshness` to the allowed check types).
Run via `cloud-sql-proxy` + `psql` the same way `scripts/deploy.sh` applies the others,
or paste this into whatever Postgres client you use against `agenteye-pg` /
`control_tower`:
```sql
ALTER TABLE sentinel.check_results
  DROP CONSTRAINT IF EXISTS check_results_check_type_check;

ALTER TABLE sentinel.check_results
  ADD CONSTRAINT check_results_check_type_check
  CHECK (check_type IN (
      'freshness', 'volume', 'variable_coverage', 'variable_freshness',
      'schema_drift', 'reconciliation'
  ));
```
Then tell me and I'll redeploy `ct-sentinel-check` to the version with the new check
(image digest above, already built and pushed).

**2. Wipe old freshness history, restart clean from today** (your request). This
deletes Sentinel's own freshness-family history, not the legacy
`control_tower.data_freshness` table (that one's still intentionally running
side-by-side — see above):
```sql
DELETE FROM sentinel.incidents
  WHERE check_type IN ('freshness', 'variable_coverage', 'variable_freshness');

DELETE FROM sentinel.check_results
  WHERE check_type IN ('freshness', 'variable_coverage', 'variable_freshness');
```
Incidents first — `check_results` rows are referenced by `incidents.check_result_id`,
so deleting results first would fail with a foreign-key error.

**3. A second blocker, found 2026-10-07 while checking the live UI:** raising
`MAX_DISTINCT` alone does **not** make Platform/Brand start getting tracked for
`Holistique_Base`, even after discovery re-runs. Discovery has an efficiency gate
(`sentinel/discovery/main.py`, the "incremental gate"): it skips the AI pass entirely
for any table whose ClickHouse column structure hasn't changed since it was last
catalogued — and only a structure change makes it re-evaluate which columns to track
as variables. I changed the Python cutoff, not `Holistique_Base`'s actual ClickHouse
columns, so discovery will keep silently skipping it forever unless nudged. The nudge
is forcing its `structure_hash` to look stale so discovery treats it as needing a
fresh look.

**Combined runbook — run this whole script once, in order, against `agenteye-pg` /
`control_tower`:**
```sql
-- 1) Force discovery to re-evaluate these two tables under the new MAX_DISTINCT=150
--    cutoff (discovery otherwise skips any table whose ClickHouse structure hasn't
--    changed, so this nudge is required — just raising the cutoff isn't enough).
UPDATE sentinel.catalog_overlay
SET structure_hash = NULL
WHERE database_name = 'holistique_default_database'
  AND table_name IN ('Holistique_Base', 'Holistique_Base_resync');

-- 2) Allow the new per-value freshness check type.
ALTER TABLE sentinel.check_results
  DROP CONSTRAINT IF EXISTS check_results_check_type_check;

ALTER TABLE sentinel.check_results
  ADD CONSTRAINT check_results_check_type_check
  CHECK (check_type IN (
      'freshness', 'volume', 'variable_coverage', 'variable_freshness',
      'schema_drift', 'reconciliation'
  ));

-- 3) Wipe old freshness-family history so monitoring restarts clean from today.
--    Incidents first -- check_results rows are FK-referenced by incidents.
--    Scope is Sentinel's own freshness-family only, NOT the legacy
--    control_tower.data_freshness table (that one's intentionally still running
--    side-by-side until the two are confirmed to agree).
DELETE FROM sentinel.incidents
  WHERE check_type IN ('freshness', 'variable_coverage', 'variable_freshness');

DELETE FROM sentinel.check_results
  WHERE check_type IN ('freshness', 'variable_coverage', 'variable_freshness');
```

Once that's run, tell me and I'll do the rest myself (none of this needs the Postgres
secret, so it's not blocked on my end):
1. `gcloud run jobs execute ct-sentinel-discovery` — reprocess `Holistique_Base`/
   `_resync` immediately instead of waiting for the weekly schedule; Platform/Brand
   should appear as tracked variables in the UI right after this.
2. `gcloud run jobs update ct-sentinel-check --image="gcr.io/seoai-479305/ct-sentinel-check@sha256:262e7245a363a8bcd09ef707fdf73da186c1dd2e8ea209d2e62af34a155b5519"` — redeploy the new per-value freshness check now that the DB allows it.
3. `gcloud run jobs execute ct-sentinel-check` — run it immediately so you don't have to
   wait for the next 02:00/08:00/14:00 UTC tick to see per-platform freshness data.

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
