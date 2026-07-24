# Sentinel — Build Spec

**Codename:** Sentinel · **Owner:** RR · **Status:** ready to build
**Source design:** [DATA_MONITOR_SPEC.md](DATA_MONITOR_SPEC.md) (v1.1, finalized)
**Built into:** Control Tower project (this repo) — reuses its infra, SA, secrets, conventions.

This is the *implementation* spec. It maps the finalized design onto Control Tower's real
infra and encodes the build order. **Sentinel supersedes** Control Tower's current freshness
path once live (via parallel-then-switch cutover, §7). Where the design gives options, decisions
are locked here.

---

## 0. Locked decisions

| Decision | Choice | Rationale |
|---|---|---|
| **Storage** | New `sentinel` schema in the existing `control_tower` Cloud SQL DB (`agenteye-pg`) | One store (§design), same `PG_CONN` secret, no new infra, clean namespace vs Control Tower's own tables |
| **ClickHouse** | Read-only source | Per design; nothing Sentinel produces is written back |
| **Compute** | 2 Cloud Run Jobs (`ct-sentinel-discovery`, `ct-sentinel-check`) + Cloud Scheduler | Matches Control Tower's `ct-*` job pattern (bootstrap.sh) |
| **LLM (residue only)** | **OpenAI primary → Gemini secondary → Claude backup**, with a **maker-checker** pass on low-confidence assignments | User decision. Keys from Atlas project → Secret Manager (§0.1) |
| **Cutover** | **Sentinel supersedes** the old freshness path via **parallel-then-switch** (§7) | User decision. No blind window; retire old only after Sentinel verified against live data |
| **Alerting** | Reuse existing path: incident open → insert `control_tower.alerts` row → `ct-alerter` emails | User decision. No new alerter job; incidents stay stateful source of truth (§5, §6) |
| **Email transport** | MS Graph (current) **+ AWS SES** as alternate sender in `ct-alerter` | User providing SES creds; wire both, select via env |
| **Frontend** | Full Sentinel section + rebuild/merge existing screens with better UX (§8) | User decision |
| **Region / project / SA** | `asia-south1`, `seoai-479305`, `ct-collector` SA | Same as all `ct-*` jobs; no new GCP IAM roles |

### 0.1 Secrets to add (Secret Manager, project `seoai-479305`)

Values sourced from `/Users/rajaram/atlas/.env.local` (present: `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`). Copy into Secret Manager — **never commit values**.
Secret Manager writes are blocked in auto mode → **user runs these**.

| Secret name | From | Used by |
|---|---|---|
| `ct-sentinel-openai-key` | Atlas `OPENAI_API_KEY` | discovery LLM (primary/maker) |
| `ct-sentinel-gemini-key` | Atlas `GEMINI_API_KEY` | discovery LLM (secondary/checker) |
| `ct-sentinel-anthropic-key` | Atlas `ANTHROPIC_API_KEY` | discovery LLM (backup) |
| `ct-ses-access-key-id` | user (SES) | alerter SES sender |
| `ct-ses-secret-access-key` | user (SES) | alerter SES sender |
| `ct-ses-region` | user (SES) | alerter SES sender |

`ct-pg-connection-string`, `ct-clickhouse-host/user/password`, `ct-alert-email`, `ct-email-*`
already exist and are reused as-is.

---

## 1. What gets built (deliverables)

1. **Migration** `011_create_sentinel_schema.sql` — **DONE + verified** (applies after 001,
   idempotent, FK to `control_tower.alerts`). 8 tables in `sentinel` schema.
2. **Migration** `012_retire_legacy_freshness.sql` — **cutover** (§7). Applied only at switch, not up front.
3. **Discovery loop** — `sentinel/discovery/main.py` (`ct-sentinel-discovery`).
4. **Check engine** — `sentinel/check_engine/main.py` (`ct-sentinel-check`) — includes the alert bridge (§6).
5. **LLM module** — `sentinel/discovery/llm.py` — provider cascade + maker-checker, PII-safe.
6. **Alerter SES sender** — add AWS SES transport alongside MS Graph in `intelligence/alerter/main.py`.
7. **UI** — full Sentinel section + reworked Freshness screen (§8) in `ui/backend/`.
8. **bootstrap.sh additions** — build/deploy both jobs + 2 schedulers.

Directory layout (new): mirrors `collectors/*` and `intelligence/*`.
```
sentinel/
  discovery/    main.py  llm.py  requirements.txt  Dockerfile
  check_engine/ main.py           requirements.txt  Dockerfile
```
`requirements.txt` = gcp_collector's set minus google-cloud-monitoring/scheduler, plus
`openai`, `google-genai`, `anthropic`.

---

## 2. Lapse resolutions (architecture review outcomes)

The review found 4 lapses vs the live Control Tower. All are resolved **in this build**, not deferred:

| # | Lapse | Resolution |
|---|---|---|
| **1** | **Duplicate freshness/volume** over the same ~328 ClickHouse tables (`watched_tables`→`check_data_freshness`→`data_freshness`→anomaly `idle_sink`) | **Sentinel supersedes** via parallel-then-switch (§7). Old path retired by migration 012 + code removal only *after* Sentinel output verified against live `data_freshness`. |
| **2** | `sentinel.incidents` had **no path to email** | **Alert bridge (§6):** incident open → insert `control_tower.alerts` row → `ct-alerter` (live) emails. `incidents.alert_id` FK links the two (migration 011). No new alerter. |
| **3** | **Schema drift**: `db` vs `database_name`, status vocab, PK type | Migration 011 renamed all `db`→`database_name` (verified: 0 stray, 4 correct). `check_results.status` uses `ok/warn/fail`; the freshness *sub-status* `fresh/stale/dead` lives inside `observed` JSONB (§5.1) so nothing regresses. `incidents` BIGSERIAL PK vs `alerts` UUID is **intentional** (stateful+self-ref vs flat) — documented in 011. |
| **4** | **`system.parts` unproven** in this repo (existing code only uses `max(col)`) | §5 marks `system.parts` reads as **validate-on-real-cluster in S4**; **fall back to `max(col)`** (the proven pattern) if `system.parts` is unreliable on ClickHouse Cloud across replicas. No blind dependency. |

---

## 3. Discovery loop — `ct-sentinel-discovery`

Cloud Run Job, Cloud Scheduler daily (`15 1 * * *` → 06:45 IST, after cost collector).
Idempotent; advisory lock + `max-instances=1`. Steps (§10 of design):

1. **Advisory lock** — `pg_try_advisory_lock(<discovery_key>)`; exit clean if not held.
2. **Enumerate** ClickHouse `system.tables` (skip `_peerdb_raw_mirror*`, `%_backup_2%`, `system.*`).
   Reuse the exact filter the collector already uses ([gcp_collector/main.py:429-438](collectors/gcp_collector/main.py#L429-L438)).
3. **Introspect cheaply** — `system.tables` (engine, ORDER BY), `system.columns`.
   `structure_hash = sha256(sorted (name,type) from system.columns)`.
4. **Incremental gate** — `structure_hash` == stored → **skip LLM**; only refresh `variable_values.last_seen`
   + mark unseen values `retired`. This is what makes daily affordable.
5. **PII-safe sampling** (changed tables only) — send LLM: column names+types, table stats,
   low-cardinality `DISTINCT` samples (≤50 distinct, str len ≤64). **Hard-block** raw row values from
   `recruitment_hr.*` and high-cardinality/sensitive columns (R5) — schema + stats only, asserted in code.
6. **LLM pass** (§4) — infer `summary`, `grain`, `concept`, `source_type`, `variables`+roles,
   `monitor_frequency_weeks` (1–4), `relationships`, `quirks`, dedup proposal. `source_type` heuristics
   run *before* the LLM: `BC_*`→`erp`, channel names→`sales_channel`, Meta→`perf_marketing`,
   GA4/Shopflo→`web_analytics`, lookups→`reference`, `*_f_copy`→`derived`.
7. **Authority + conflict** (§7.3–7.4 design): evidence-first; LLM+maker-checker for residue;
   confidence gate (non-blocking) → `authoritative`/`review_status`/`conflict_type`; enforce/flag I1–I4;
   re-evaluate each run; **never overwrite `updated_by='human'` rows**.
8. **Drift & dropped tables** — structural change → `needs_review`; gone from `system.tables` →
   overlay `retired=true` + `monitor_targets.status='retired'`.
9. **Monitor targets** — upsert `monitor_targets` (per table + per attached variable);
   set `monitor_frequency_weeks`, `cheap_source`, `next_due_at=now()` for new targets.
10. **Reconciliation rules** — generate in `observe`; auto-promote stable to `active` (§9 design).
11. **Write** (upsert). Discovery **never alerts**.
12. Release lock; ping healthchecks.io (last, on success — reuse Control Tower pattern).

Log skip-vs-LLM counts each run (cost visibility).

---

## 4. LLM module — `sentinel/discovery/llm.py`

**Cascade:** Maker = OpenAI → Gemini → Claude on error/timeout. **Checker (maker-checker):** for any
assignment below `SENTINEL_CONFIRM_THRESHOLD` (start `0.80`) **or** any authority/dedup decision, a
*different* provider re-derives the same fields from the same PII-safe payload. Agree → confirm;
disagree → keep best-guess + `review_status='needs_review'` (non-blocking, §7.4). Replaces the removed human gate.

All calls **structured output** (JSON schema) validated before write; invalid JSON → retry once then
fall through cascade. PII builder has no path to raw rows; `recruitment_hr` = schema+stats only, asserted.

Keep thin: `infer_overlay(payload) -> dict`, `check(payload, maker_result) -> agree|disagree`.

---

## 5. Check engine — `ct-sentinel-check`

Cloud Run Job, Cloud Scheduler daily tick (`0 2 * * *` → 07:30 IST). Selects `monitor_targets` where
`next_due_at <= now() AND status='active'`; runs; updates `last_checked_at`/`next_due_at`
(`+ monitor_frequency_weeks weeks`). Advisory lock + `max-instances=1`. 5 checks (§8 design), table-level
+ per attached variable value:

1. **Freshness** — latest data vs frequency + `freshness_tolerance`. **Primary mechanism: `system.parts`**
   (`max(modification_time)`); **fallback: `max(freshness_column)`** — the proven collector pattern
   ([gcp_collector/main.py:507-511](collectors/gcp_collector/main.py#L507-L511)) — selected per config/probe (Lapse 4).
   **dedup OFF** (signal). Freshness sub-status `fresh|stale|dead` written inside `observed` JSONB; the row
   `status` is `ok|warn|fail`.
2. **Volume/completeness** — recent row count vs band (`sum(rows)` over active parts, or light
   `GROUP BY {variable}` on pruned partitions). **dedup OFF**. This is the genuinely-new coverage the old
   path spec'd but never implemented (`data_freshness.row_count_delta` was always NULL).
3. **Variable coverage** — `active` values in `variable_values` missing from recent data (set-difference). New.
4. **Schema drift** — columns added/removed/retyped vs stored `structure_hash`/`system.columns`. New.
5. **Cross-table reconciliation** (§6 design) — **dedup ON** (correctness): overlay `dedup_key`/method.

**Dedup split is load-bearing:** OFF for 1–2 (CDC dupes don't distort a signal), ON for 5 (dupes create
false discrepancies). Each check → append `check_results` (`ok|warn|fail`, `observed`, `duration_ms`, `query_cost`).

---

## 6. Incidents + alert bridge (Lapse 2 resolution)

On `warn`/`fail`: open/update a `sentinel.incidents` row (dedup by open scope; roll children under a
parent to suppress floods). **On open**, the check engine ALSO inserts one `control_tower.alerts` row
and stores its UUID in `incidents.alert_id` (FK, migration 011):

```
alert_type   = 'sentinel_' || check_type      -- e.g. sentinel_freshness
severity     = incidents.severity             -- same info/warn/critical vocab
message      = incidents.message
context_json = incidents.scope + observed excerpt + incident_id
```

`ct-alerter` (live, at :45) already polls `control_tower.alerts WHERE acknowledged_at IS NULL` → emails
via MS Graph/SES → acks. No new alerter job. On recovery, set `incidents.resolved_at` (the alert already
fired once; dedup prevents re-fire). Parent/child rollup keeps one email per incident cluster.

**Reconciliation depth + observe→active** (§9 design): rules computed **with dedup**; `observe` logs only;
`stable_runs ≥ N` (start N=3) auto-promotes to `active` (alerting). Rule firing immediately stays `observe`
+ flagged. No human gate. Deferred design open item ("self connector") stays out of scope.

---

## 7. Cutover — parallel then switch (Lapse 1 resolution)

**Phase A — parallel (default at launch).** Sentinel discovery + check run alongside the existing
`watched_tables`/`data_freshness`/anomaly `idle_sink` path. Both write; nothing removed. UI Freshness tab
shows both sources side by side (§8) for comparison.

**Phase B — verify.** For ≥1 week, compare Sentinel freshness verdicts against `data_freshness` over the
same ~329 tables. Success criterion: Sentinel `fresh/stale/dead` classification matches `data_freshness`
for ≥ 99% of tables (mismatches investigated — expected only where Sentinel's `system.parts` mechanism or
adaptive cadence differs intentionally).

**Phase C — switch (migration 012 + code removal).** Only after B passes:
- `012_retire_legacy_freshness.sql`: migrate the 6 manually-seeded `watched_tables` rows into
  `sentinel.monitor_targets` (preserve cadence, `selected_by='human'`); mark `watched_tables.active=false`
  (keep table + `data_freshness` history for rollback, do not drop).
- Remove `auto_discover_watched_tables` + `check_data_freshness` calls from `gcp_collector/main.py`
  (steps 5, main.py:807-809) — only YOUR changes' orphans removed (per CLAUDE.md §3).
- Remove anomaly detector Check 3 (`idle_sink`) — Sentinel freshness incidents replace it.
- UI Freshness tab switches to Sentinel-only.

**Rollback:** re-enable the removed calls + `watched_tables.active=true`. History retained, so no data loss.

---

## 8. Frontend (full Sentinel section + rework)

`ui/backend/` is FastAPI + vendored Preact/htm (no npm build). Add `/api/sentinel/*` endpoints and new
nav tabs; rework Freshness with better UX.

**New tabs:**
- **Catalog** — `catalog_overlay` browser: per table → concept/scope/source_type/grain/summary, authority
  badge, dedup spec, relationships, quirks. Filter by concept/source_type. Surfaces the semantic layer.
- **Authority & Conflicts** — the review queue: rows where `conflict_type <> 'none'` OR
  `review_status='needs_review'`. Shows conflict type + options (the §7.2.5 gate made visible). Optional
  human-pin action (writes `updated_by='human'`).
- **Coverage** — `variable_values`: active vs retired values per monitored variable; missing-value findings.
- **Reconciliation** — `reconciliation_rules` + latest `check_results` per rule: observe/active/muted,
  tolerance, last ok/warn/fail, cross-source drift.
- **Incidents** — `sentinel.incidents`: open/resolved timeline, severity, scope, parent/child rollup,
  linked alert. Replaces raw freshness alert noise.

**Reworked screens:**
- **Freshness** — during cutover Phase A/B: two columns (Sentinel verdict | legacy `data_freshness`) for
  comparison. After switch: Sentinel-only, richer (freshness + volume band + drift in one view).
- **Overview** — add Sentinel summary cards: tables cataloged, needs-review count, open incidents,
  reconciliation rules active.

Keep the vendored-libs / no-CDN convention and `Cache-Control: no-cache` on the shell.

---

## 9. Build order (phased)

| Phase | Deliverable | Verify |
|---|---|---|
| **S0** | Migration 011 (**done, verified**) | `\dt sentinel.*` → 7 tables; vocab 8 rows; `alert_id` FK present; 0 `db` cols |
| **S1** | Add 6 secrets (3 LLM + 3 SES) — **user** | `gcloud secrets list \| grep -E 'ct-sentinel\|ct-ses'` → 6 |
| **S2** | Discovery: steps 1–6 (introspect, incremental gate, PII-safe sample, LLM), write overlay only | Local run via cloud-sql-proxy → overlay rows for changed tables; skip-count logged |
| **S3** | Discovery: steps 7–10 (authority, conflict, targets, recon rules) | I1–I4 flagged on a seeded collision; `monitor_targets` + `observe` rules written |
| **S4** | Check engine: checks 1–4 + `system.parts` probe/fallback (Lapse 4) | Due targets run; `system.parts` validated on real cluster OR fallback active; a stale table → `check_results` fail |
| **S5** | Check engine: check 5 reconciliation + observe→active + **alert bridge** (§6) | Deduped rule promotes after N runs; incident open → `control_tower.alerts` row → `ct-alerter` emails (E2E) |
| **S6** | Alerter SES sender | Email sends via SES when `EMAIL_TRANSPORT=ses`; MS Graph still works |
| **S7** | UI: Sentinel section + reworked Freshness (parallel view) | All new tabs render live data; Freshness shows both sources |
| **S8** | bootstrap.sh: 2 jobs + 2 schedulers | `gcloud run jobs list \| grep ct-sentinel` → 2; 2 `trigger-ct-sentinel-*` green |
| **S9** | **Cutover** (§7 Phase C): migration 012 + code removal + UI switch | After ≥1wk parallel verify ≥99% match; old path removed; rollback path documented |

S2–S7 are code. S1 + S8 + S9 touch infra (user-approved: Secret Manager write, scheduler create;
IAM already covered). S9 gated on the Phase B verification result.

---

## 10. Risks carried from design (accepted)

- **R2 (autonomy tail):** confidently-wrong LLM authority with no reconciliation evidence → possible wrong
  silent answer. Mitigated by evidence-first + **maker-checker** + per-run re-eval. Pin critical concepts
  (`updated_by='human'`) to eliminate locally.
- **R3 (overlay ↔ structure drift):** frequent refresh + drift flag + §7.2.5 gate; overlay advisory over live structure.
- **R5 (PII):** enforced in prompt builder (schema+stats only for `recruitment_hr`); revisit for new sensitive DBs.
- **R1 (consumer reads Postgres):** consumers need read on `sentinel.*` — a separate bridging task, out of scope.

---

## 11. Non-goals (explicit)

- No write to ClickHouse (read-only source).
- No blocking human step anywhere.
- No RBAC MCP middleware bridge (separate).
- No auto-remediation / "self connector" (deferred until design open item decided).
- No new GCP IAM roles beyond `ct-collector`'s current set.
- No drop of legacy `watched_tables`/`data_freshness` — deactivated + retained for rollback (§7 Phase C).
