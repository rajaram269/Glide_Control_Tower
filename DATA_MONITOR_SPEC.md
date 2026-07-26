# Data Freshness/Consistency Monitor + Catalog Overlay — Spec (v1.1, finalized)

**Working codename:** *Sentinel*
**Owner:** RR
**Infra:** storage = **Cloud SQL for PostgreSQL** (single store for all Sentinel-produced data); compute = **GCP Cloud Run** (Jobs + Cloud Scheduler). ClickHouse is a **read-only source** (introspection + check queries), never a Sentinel write target.
**Scope:** produce and keep correct two structured outputs in Postgres — a **catalog overlay** and **monitor data** — as byproducts of one discovery loop, plus the deterministic **resolution logic** encoded in that data so any consumer selects the right table/dedup without confusion. **Bridging these outputs to a consumer (your RBAC MCP middleware, Control Tower, dashboard) is out of scope — you do that separately.**

**Changelog v1.0 → v1.1:** removed the blocking human-in-the-loop — the flow is **fully autonomous**. Authority and reconciliation rules are assigned autonomously (evidence-first + confidence, §7.4), ambiguity is *flagged* (non-blocking) not gated, authority is *self-auditing* via reconciliation, and human review is an optional async dashboard, never a pipeline dependency.
**Changelog v0.8 → v1.0 (finalize):** storage consolidated to **Postgres** (overlay moved out of ClickHouse — ClickHouse is now read-only); **Cloud Run** deployment specified; reconciliation now **dedups** (correctness) while freshness/volume stay dedup-free (signal); discovery loop made **incremental** (LLM only on changed tables) and **PII-safe** (no sensitive row values sent to the LLM); check engine given **due-state scheduling**; added `grain`; full critique in §11.

---

## 1. The shape

One **discovery loop** (Cloud Run Job) introspects ClickHouse on a frequent cadence and writes two structured outputs to **Postgres**:

1. **Catalog overlay** (`catalog_overlay`) — non-derivable metadata that makes queries *correct and unambiguous*: meaning + grain, origin/purpose (`source_type`: ERP vs sales-channel vs perf-marketing…), which table is authoritative for a concept/scope/origin, the exact dedup key, joins, quirks, conflict flags. Regenerated/refreshed every run.
2. **Monitor data** — `monitor_targets`, `variable_values`, `reconciliation_rules`, `check_results`, `incidents`.

Governing principle for *what to store*:

> **Native for what's live and derivable; overlay for what isn't.** Live structure and facts (columns, types, current values, freshness, row counts) come from native ClickHouse (`DESCRIBE`, `system.*`, sampling) in real time at query/check time and are never copied into the overlay. Only meaning, grain, origin, authority, dedup key, joins, quirks, and conflict state persist.

ClickHouse holds the data and is queried live; Postgres holds Sentinel's metadata and results. Nothing Sentinel produces is written back to ClickHouse.

---

## 2. Two cadences (independent)

- **Discovery / overlay refresh** — frequent, configurable (daily+). Introspection is cheap; the LLM runs only on tables whose structure changed since last run (§10), so frequent is affordable.
- **Per-table check frequency** — integer **weeks, 1–4**, assigned per target by the loop. A daily check-engine tick runs only the targets that are *due* (§5B `next_due_at`), so cadence is data-driven, not a schedule per table.

---

## 3. Components & deployment (Cloud Run)

| Component | Cloud Run form | Trigger | Reads | Writes |
|---|---|---|---|---|
| **Discovery loop** | Job | Cloud Scheduler (frequent, e.g. daily) | ClickHouse `system.*` + PII-safe samples | Postgres: overlay, targets, values, recon proposals |
| **Check engine** | Job | Cloud Scheduler (daily tick) | ClickHouse `system.parts` + light/reconciliation queries; Postgres targets | Postgres: `check_results`, `incidents` |

Deployment notes:
- **Cloud SQL for PostgreSQL** as the store; jobs connect via the Cloud SQL connector.
- Service account with read on ClickHouse Cloud and read/write on Cloud SQL; secrets in **Secret Manager**; egress configured for ClickHouse Cloud.
- Both jobs are **idempotent** (upsert on keys) and safe to retry. Prevent overlap with `max-instances = 1` per job plus a Postgres **advisory lock** at job start.
- No PeerDB/ClickHouse write path for Sentinel. (If you later want results history in ClickHouse for a dashboard, that's an *optional* CDC from Postgres — out of scope here.)

Consumers (your MCP middleware, Control Tower, dashboard) are bridged separately.

---

## 4. Architecture

```
                 ┌────────────────────────────────────────────┐
                 │            ClickHouse Cloud (READ-ONLY)      │
                 │  data tables · system.tables/parts/columns   │
                 └───▲───────────────────────────▲──────────────┘
   introspect + PII-  │                           │ system.parts +
   safe sample        │                           │ light scan / dedup reconcile
        ┌─────────────┴────────┐      ┌───────────┴───────────┐
        │  Discovery Loop       │      │   Check Engine         │
        │  Cloud Run Job (freq) │      │  Cloud Run Job (daily) │
        └─────────────┬────────┘      └───────────┬───────────┘
                      │ upsert overlay/targets/     │ results/incidents
                      │ values/recon                │
                      ▼                             ▼
        ┌──────────────────────────────────────────────────────┐
        │            Cloud SQL for PostgreSQL (single store)     │
        │  catalog_overlay · monitor_targets · variable_values · │
        │  reconciliation_rules · check_results · incidents      │
        └──────────────────────────────────────────────────────┘

   ┌────────────────────────────────────────────────────────────────┐
   │ Consumers (OUT OF SCOPE — bridged separately): your RBAC MCP      │
   │ middleware · Control Tower · dashboard  (all read from Postgres)  │
   └────────────────────────────────────────────────────────────────┘
```

---

## 5. Data model (all Postgres)

### 5A. `catalog_overlay` — PK `(db, table_name)`
Non-derivable metadata only; never live values.

| Column | Type | Purpose |
|---|---|---|
| `db`, `table_name` | text | key |
| `summary` | text | meaning — only for names you can't fix at source |
| `grain` | text | "one row per …" (guards against double-counting on joins) |
| `concept` | text | business concept (`sales`, `inventory`, `spend`) |
| `source_type` | text | origin/domain driving purpose (`erp`, `sales_channel`, `perf_marketing`, `web_analytics`, `warehouse_ops`, `finance`, `reference`, `derived`); FK to `source_type_vocab` |
| `scope` | text | business subset within concept (`B2B`, `B2C`, a brand) — not the origin |
| `authoritative` | boolean | true = canonical table for its `(concept, scope, source_type)` |
| `authority_confidence` | numeric | 0–1; ≥ threshold auto-confirms, below flags `needs_review` (non-blocking, §7.4) |
| `use_instead` | text | if not authoritative, the table to use for that key |
| `requires_dedup` | boolean | must dedup before use |
| `dedup_method` | text | `argmax` \| `final` \| `none` |
| `dedup_key` | text[] | exact business key |
| `version_col`, `delete_col` | text | source-table CDC cols (`_peerdb_version`, `_peerdb_is_deleted`) |
| `dedup_note` | text | free-text nuance |
| `quirks` | text[] | anti-patterns (e.g. Meta insight×campaigns rule) |
| `variables` | jsonb | `[{column, role, note}]` |
| `relationships` | jsonb | `[{to, on, purpose}]` |
| `column_notes` | jsonb | `{column: note}` for un-renameable columns |
| `conflict_type` | text | `none`\|`authority_gap`\|`authority_collision`\|`dangling_pointer`\|`broken_pointer`\|`unresolved_dedup`\|`broken_join` (§7) |
| `review_status` | text | `confirmed` \| `needs_review` |
| `structure_hash` | text | hash of `system.columns` — drives incremental LLM (§10) |
| `updated_by`, `updated_at` | text / timestamptz | `llm` \| `human`; version |

`source_type_vocab` is a small lookup table (`code`, `label`) so new origins are added by data, not migration; the loop adds new codes autonomously (you can relabel later — non-blocking).

### 5B. Monitor data
| Table | Grain | Key fields |
|---|---|---|
| `monitor_targets` | (table, variable?) | `monitor_frequency_weeks` (1–4), `freshness_tolerance`, `volume_tolerance`, `cheap_source` (`system_parts`\|`light_scan`), `status`, `last_checked_at`, `next_due_at`, `selected_by`, `iteration` |
| `variable_values` | (table, variable, value) | `first_seen`, `last_seen`, `lifecycle` (`active`\|`retired`), `retired_at` |
| `reconciliation_rules` | rule | `rule_id`, `concept`, `metric`, `source_a`, `source_b`, `dimension?`, `tolerance_pct`, `direction` (`a≈b`\|`a≥b`\|`subset`), `status` (`observe`\|`active`\|`muted`; auto-promoted, §9) |
| `check_results` | run | `run_ts`, scope, `check_type`, `status`, `observed` (jsonb), `monitor_frequency_weeks`, `duration_ms`, `query_cost` |
| `incidents` | incident | `opened_at`/`resolved_at`, `scope` (jsonb), `scope_level`, `check_type`, `severity`, `parent_incident_id`, `rolled_up_children`, `message` |

The `incidents` structure is alert-ready for your separate bridge: `severity` for routing, `parent_incident_id`/`rolled_up_children` for suppression, `opened`/`resolved` transitions for dedup.

---

## 6. The overlay: what it fixes

**Naming gaps → summaries/notes, only where unfixable.** Where a name *can* be renamed to something honest, discovery emits a **rename candidate** to fix at source rather than documenting a bad name forever.

**Purpose / origin → `source_type`.** The same concept can legitimately come from different systems for different purposes. ERP-recorded sales (`erp`, e.g. `BC_SalesInvoiceLineBI`) and channel-reported sales (`sales_channel`) are **not duplicates** — they answer different questions, at different grain and latency. `source_type` marks the origin so selection is purpose-driven and the conflict detector never mistakes different-origin tables for competing authorities.

**Duplicacy → structured fields, not prose.**
- *Same concept + scope + source_type, multiple tables* → `authoritative` + `use_instead` (§7). Different `source_type` = different purpose, never a duplicate.
- *Duplicate rows in a table* → `requires_dedup` + `dedup_method` + `dedup_key` + `version_col`/`delete_col` (§7).

---

## 7. Query-resolution logic & conflict guarantees

The overlay encodes a **deterministic resolution path**, and the loop **guarantees invariants** (or flags exactly where they're violated so nothing is guessed).

### 7.1 Invariants (enforced/flagged)
- **I1 — single authority:** for each `(concept, scope, source_type)`, exactly one table is `authoritative`. Same concept, different `source_type` (ERP vs channel) = distinct purposes, not competitors.
- **I2 — every alias redirects:** every non-authoritative table in a `(concept, scope, source_type)` has `use_instead` → that key's authoritative table.
- **I3 — dedup fully specified:** every CDC/Replacing-engine table has `requires_dedup = true` and a non-empty `dedup_key` (+ method + version/delete cols).
- **I4 — joins resolve:** every `relationships[].on` references columns that currently exist (checked against live native structure).

### 7.2 Resolution path (deterministic, for any consumer)
1. **Intent → (concept, scope, source_type).** Purpose usually implies origin — "ERP/invoiced sales" vs "channel/marketplace-reported sales". If any axis is unclear, the overlay exposes the options under the concept so the consumer disambiguates — it does not guess.
2. **Pick table:** the `authoritative` table for that key; if a referenced table is non-authoritative, follow `use_instead`.
3. **Dedup:** if `requires_dedup`, wrap with `dedup_method` over `dedup_key` (`argMax(<col>, version_col) GROUP BY <dedup_key> HAVING argMax(delete_col, version_col)=0`, or `FINAL`).
4. **Joins & quirks:** use `relationships`; apply `quirks` (e.g. Meta `DISTINCT id` CTE); respect `grain` to avoid double-counting.
5. **Safety gate:** if the chosen table's `conflict_type != none` **or** `review_status = needs_review`, **stop and surface the conflict/options** — never auto-resolve. Ambiguity becomes explicit, never a wrong silent answer.

### 7.3 Conflict states detected each run
| `conflict_type` | Trigger | Consumer behavior |
|---|---|---|
| `authority_gap` | a `(concept, scope, source_type)` with 0 authoritative | ambiguous → ask/pick |
| `authority_collision` | a `(concept, scope, source_type)` with >1 authoritative | ambiguous → ask/pick |
| `dangling_pointer` | non-authoritative table with no `use_instead` | unusable until fixed |
| `broken_pointer` | `use_instead` → nonexistent/non-authoritative table | unusable until fixed |
| `unresolved_dedup` | Replacing-engine table with no `dedup_key` | don't query without manual dedup |
| `broken_join` | relationship references a dropped column | drop that join path |

Two authoritative tables under the same `concept`/`scope` but different `source_type` are **not** a collision — that's ERP-vs-channel, and both are correct. Authority is assigned **autonomously** (§7.4); genuine ambiguity is *flagged* (`needs_review`/`conflict_type`), never handed to a blocking human step. The optional review queue = `SELECT * FROM catalog_overlay WHERE conflict_type <> 'none' OR review_status = 'needs_review'` is where a human can help *if and when* they choose. Because a consumer honors 7.2.5, a flagged conflict yields an explicit "ambiguous, resolve first" — never a wrong answer — so the pipeline is safe whether or not anyone ever looks.

### 7.4 Autonomy — authority without a human gate
The flow is fully autonomous; there is **no blocking human step**. Authority is (re)assigned every discovery run:

1. **Evidence first.** Deterministic signals decide most cases without opinion: engine/naming conventions (e.g. `_BI` marts, `BC_*` origin), row completeness, freshness, and — crucially — **reconciliation agreement** (a candidate that agrees with related sources is trustworthy).
2. **LLM only for the residue**, where evidence is insufficient. Each assignment carries `authority_confidence`.
3. **Confidence gate (not a human gate):** high confidence + a single clear candidate → `authoritative = true`, `review_status = confirmed`, fully autonomous. Otherwise → best-guess assignment **plus** `needs_review`/`conflict_type`. This flag is **non-blocking** — the pipeline continues; it only changes *consumer* behavior at query time (surface options, §7.2.5).
4. **Self-auditing.** Authority is re-evaluated each run against fresh evidence. A wrong pick shows up as reconciliation divergence (§9) and flips to `needs_review` on the next run — so authority corrects itself over time without anyone intervening. This is what makes autonomy safe.

The only correctness gate on *behavior* lives at query-resolution time (§7.2.5) and is **data-driven** (`conflict_type`/`review_status`), not human. Discovery never waits on anyone.

**Honest residual:** a case the LLM is *confidently wrong* about, where no reconciliation evidence exists to contradict it, can still produce a wrong silent answer. Evidence-first assignment, reconciliation self-check, and per-run re-evaluation shrink this tail sharply but don't erase it — that is the accepted cost of zero human gating. Where you want to eliminate it for a specific critical concept, pin its authority manually once (a `human`-set row the loop won't override); that's an option, not a requirement.

---

## 8. The monitor (checks)

Each runs table-level and, where a variable is attached, per value:
1. **Freshness** — latest data vs the target's `monitor_frequency_weeks` + tolerance.
2. **Volume/completeness** — recent row count vs expected band.
3. **Variable coverage** — which `active` values are missing (set-difference vs `variable_values`).
4. **Schema drift** — columns added/removed/retyped from `system.columns`.
5. **Cross-table reconciliation** — §9.

Reading — **dedup off for signals (1–2), on for correctness (5):** freshness/volume read table-level from `system.parts` (`max(modification_time)`, `max(max_date)`, `sum(rows)` over active parts) and per-variable via a light non-deduped `GROUP BY {variable}` on pruned recent partitions — duplicates don't matter for a freshness/volume *signal*. Reconciliation (§9) is different: it compares metric *values*, where CDC duplicates would cause false discrepancies, so it **applies the overlay dedup pattern**. `system.parts` gives per-variable numbers free only if the variable is in the partition/order key; else the light scan is used.

The check-engine tick selects targets where `next_due_at <= now()`, runs them, and updates `last_checked_at`/`next_due_at`.

---

## 9. Depth — cross-table reconciliation

Driven by `reconciliation_rules` generated **autonomously** by the loop from `concept`/`relationships`/`source_type`. New rules start in `observe` (log discrepancies, don't alert); a rule that stays within `tolerance_pct` across runs **auto-promotes** to `active` (alerting); one that fires immediately stays in `observe` and is flagged rather than paging. No human confirmation gates activation — the observe period is the safeguard against false-alarm storms, replacing the old human sign-off. Metrics are computed **with dedup** (overlay `dedup_key`/method) so discrepancies are real, not CDC artifacts. Four uses:

1. **Segment-vs-total** — sum across a variable's values reconciles to the table/authoritative source within `tolerance_pct`.
2. **Referential integrity** — a variable's values ⊆ a reference table (`direction = subset`), e.g. `division_name ⊆ Holistique_pincodes_reference.divisionname`.
3. **Duplicate-concept divergence** — reconcile an `authoritative` table against its deprecated `use_instead` counterpart before retiring the duplicate; catches silent drift while both exist. Closes the loop with §7.
4. **Cross-source agreement** — same concept from *different* `source_type`s that should roughly agree (ERP-invoiced vs channel-reported sales) reconciled within `tolerance_pct` to surface leakage between systems. Neither is deprecated; both are kept.

A rule computes deduped metric(source_a) vs metric(source_b) (optionally per dimension value), compares to `tolerance_pct`/`direction`, emits `ok|warn|fail`.

**Still open:** "self connector → link to logic code" — auto-remediation (agent reruns/fixes the failing pipeline) vs traceability (gap links to source query). Not designed until you pick one.

---

## 10. Discovery loop

Frequent cadence, per table:
1. **Introspect cheaply** — `system.tables` (engine, ORDER BY → key candidate), `system.columns`, `system.parts`. Compute `structure_hash`.
2. **Incremental gate** — if `structure_hash` == stored, **skip the LLM**; only refresh `variable_values` (`last_seen`) and check retirement. This keeps the frequent cadence cheap regardless of table count.
3. **PII-safe sampling** — for tables needing an LLM pass, send column names/types + `system.parts` stats + low-cardinality `DISTINCT` samples only. Never send raw values from high-cardinality or sensitive columns (esp. `recruitment_hr`); mask/omit them (R5).
4. **LLM pass** — `summary`, `grain`, `concept`, `source_type` (origin: `BC_*`→`erp`, channel names→`sales_channel`, Meta→`perf_marketing`, GA4/Shopflo→`web_analytics`, lookups→`reference`, `*_f_copy`→`derived`), candidate `variables`+`role`, `monitor_frequency_weeks` (1–4), `relationships`, `quirks`, `dedup_key`/method/version-delete proposal.
5. **Naming** — rename candidates for fixable names; `summary`/`column_notes` only for unfixable.
6. **Authority & conflict detection (§7.3–7.4)** — assign authority autonomously (evidence-first + confidence); enforce/flag I1–I4; low-confidence/ambiguous → `needs_review` (non-blocking); re-evaluate prior assignments against new evidence, respecting any `human`-pinned rows.
7. **Drift & dropped tables** — structural change → `needs_review`; a table gone from `system.tables` → mark its overlay row/targets retired.
8. **Reconciliation rules** — generate `reconciliation_rules` in `observe`; auto-promote stable ones to `active` (§9).
9. **Write** (upsert) — overlay, targets (+ `next_due_at`), values, recon proposals.

Discovery only writes data; it never alerts.

---

## 11. Final critique & review

**Issues found in this pass and resolved:**
- **Store split was now unjustified.** With MCP bridging out of scope, the reason to keep the overlay in ClickHouse (native read by a CH-brokering middleware) no longer holds, and you've chosen Postgres. → Overlay moved to Postgres; ClickHouse is read-only. This also **dissolves the old PeerDB-comment risk** (overlay is our Postgres table; CDC never touches it).
- **Reconciliation vs "no dedup" contradiction.** §8 said dedup off, but §9 compares sums where duplicates cause false failures. → Split explicitly: dedup **off** for freshness/volume signals, **on** for reconciliation (§8, §9).
- **Frequent LLM-per-table = cost trap.** Across four databases this could be hundreds of LLM calls per run. → Incremental gate on `structure_hash` (§10.2); LLM runs only on changed tables.
- **PII exposure.** Sampling `recruitment_hr` rows into an LLM leaks applicant data. → PII-safe sampling (§10.3): schema + stats + low-cardinality categoricals only.
- **Check scheduling had no due-state.** `monitor_frequency_weeks` existed but nothing tracked when a target was last run. → `last_checked_at`/`next_due_at` on `monitor_targets`; daily tick runs due targets.
- **Missing grain.** Re-added `grain` to the overlay — needed to avoid double-counting on joins.
- **Concurrency/idempotency** on Cloud Run retries. → upsert everywhere + advisory lock + `max-instances=1` (§3).

**Residual risks (accepted):**
- **R1 — Consumer reads Postgres.** Your MCP middleware brokers ClickHouse; the overlay now lives in Postgres, so your separate bridge must give the middleware Postgres read access (or mirror the overlay into ClickHouse for its read path). Deliberate, acknowledged bridging task — not a flaw.
- **R2 — Autonomy (resolved, was the human-in-loop concern).** The flow has **no blocking human step**. Authority and reconciliation rules are assigned autonomously (evidence-first + confidence, §7.4; observe→active promotion, §9). Ambiguity is *flagged* (non-blocking), not gated, and authority is *self-auditing* via reconciliation. A human dashboard exists only as optional async help. Honest residual: a confidently-wrong assignment with no reconciliation evidence to contradict it can yield a wrong silent answer; evidence-first + self-re-evaluation shrink but don't erase this tail. Manual authority-pinning is available per critical concept if you want that tail gone there — an option, not a requirement.
- **R3 — Overlay ↔ structure drift between runs.** Mitigated by frequent refresh + drift flag + the 7.2.5 gate; consumers treat the overlay as advisory over live native structure.
- **R4 — Coverage bootstrap.** A value that never appeared can't be flagged missing; pin must-exist values manually if needed.
- **R5 — PII.** Handled by §10.3; revisit if new sensitive databases are added.

**Verdict:** finalized and buildable on Postgres + Cloud Run with no external dependencies beyond ClickHouse read access. No blocking unknowns remain. The only undesigned item is the optional "self connector" (§9), which is out of the core scope.

---

*Open item: "self connector → link to logic code" — auto-remediation or traceability? Everything else is finalized for v1.1. Build order/phasing is left to the build process (Claude Code).*
