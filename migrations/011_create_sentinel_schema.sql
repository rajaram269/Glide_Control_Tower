-- Migration 011 — Sentinel: Data Freshness/Consistency Monitor + Catalog Overlay
-- Target: control_tower database, Cloud SQL agenteye-pg (seoai-479305:us-central1:agenteye-pg)
-- Spec: DATA_MONITOR_SPEC.md (v1.1) + SENTINEL_BUILD_SPEC.md
--
-- All Sentinel-produced data lives in its own `sentinel` schema in the same
-- control_tower database. ClickHouse is a READ-ONLY source; nothing here is
-- written back to ClickHouse. Run once. Idempotent (IF NOT EXISTS throughout).

CREATE SCHEMA IF NOT EXISTS sentinel;

-- ─── VOCAB ────────────────────────────────────────────────────────────────
-- source_type is data-driven (new origins added by the loop, not by migration).

CREATE TABLE IF NOT EXISTS sentinel.source_type_vocab (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO sentinel.source_type_vocab (code, label) VALUES
    ('erp',            'ERP-recorded (Business Central, invoices)'),
    ('sales_channel',  'Sales channel / marketplace-reported'),
    ('perf_marketing', 'Performance marketing (Meta, Google Ads)'),
    ('web_analytics',  'Web analytics (GA4, Shopflo)'),
    ('warehouse_ops',  'Warehouse / fulfilment operations'),
    ('finance',        'Finance / accounting'),
    ('reference',      'Reference / lookup tables'),
    ('derived',        'Derived / computed (*_f_copy, marts)')
ON CONFLICT (code) DO NOTHING;

-- ─── CATALOG OVERLAY ────────────────────────────────────────────────────────
-- §5A. Non-derivable metadata ONLY. Never stores live values (columns, types,
-- row counts, freshness) — those come from native ClickHouse at query/check time.
-- PK is (db, table_name).

CREATE TABLE IF NOT EXISTS sentinel.catalog_overlay (
    database_name        TEXT        NOT NULL,       -- matches control_tower.* naming (was `db`)
    table_name           TEXT        NOT NULL,
    summary              TEXT,                       -- meaning; only for names unfixable at source
    grain                TEXT,                       -- "one row per ..."; guards double-counting
    concept              TEXT,                       -- sales | inventory | spend | ...
    source_type          TEXT        REFERENCES sentinel.source_type_vocab(code),
    scope                TEXT,                       -- business subset (B2B, B2C, brand) — not origin
    authoritative        BOOLEAN     NOT NULL DEFAULT false,
    authority_confidence NUMERIC(4,3),               -- 0.000–1.000
    use_instead          TEXT,                       -- table to use when not authoritative
    requires_dedup       BOOLEAN     NOT NULL DEFAULT false,
    dedup_method         TEXT        CHECK (dedup_method IN ('argmax', 'final', 'none')),
    dedup_key            TEXT[],                     -- exact business key
    version_col          TEXT,                       -- CDC version col (_peerdb_version)
    delete_col           TEXT,                       -- CDC delete col (_peerdb_is_deleted)
    dedup_note           TEXT,
    quirks               TEXT[],                     -- anti-patterns
    variables            JSONB,                      -- [{column, role, note}]
    relationships        JSONB,                      -- [{to, on, purpose}]
    column_notes         JSONB,                      -- {column: note}
    conflict_type        TEXT        NOT NULL DEFAULT 'none'
                             CHECK (conflict_type IN (
                                 'none', 'authority_gap', 'authority_collision',
                                 'dangling_pointer', 'broken_pointer',
                                 'unresolved_dedup', 'broken_join'
                             )),
    review_status        TEXT        NOT NULL DEFAULT 'confirmed'
                             CHECK (review_status IN ('confirmed', 'needs_review')),
    structure_hash       TEXT,                       -- hash of system.columns → drives incremental LLM
    updated_by           TEXT        NOT NULL DEFAULT 'llm'
                             CHECK (updated_by IN ('llm', 'human')),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired              BOOLEAN     NOT NULL DEFAULT false,   -- table gone from system.tables
    PRIMARY KEY (database_name, table_name)
);

-- Consumers filter on the safety gate (§7.2.5); index the flags they read.
CREATE INDEX IF NOT EXISTS idx_overlay_authority
    ON sentinel.catalog_overlay (concept, scope, source_type)
    WHERE authoritative = true;
CREATE INDEX IF NOT EXISTS idx_overlay_conflict
    ON sentinel.catalog_overlay (conflict_type)
    WHERE conflict_type <> 'none';
CREATE INDEX IF NOT EXISTS idx_overlay_review
    ON sentinel.catalog_overlay (review_status)
    WHERE review_status = 'needs_review';

-- ─── MONITOR: TARGETS ─────────────────────────────────────────────────────
-- §5B. Grain (table, variable?). variable = '' means table-level target.
-- Due-state scheduling: daily tick runs targets where next_due_at <= now().

CREATE TABLE IF NOT EXISTS sentinel.monitor_targets (
    id                      SERIAL PRIMARY KEY,
    database_name           TEXT    NOT NULL,
    table_name              TEXT    NOT NULL,
    variable                TEXT    NOT NULL DEFAULT '',   -- '' = table-level
    monitor_frequency_weeks INT     NOT NULL DEFAULT 1
                                CHECK (monitor_frequency_weeks BETWEEN 1 AND 4),
    freshness_tolerance     INTERVAL,
    volume_tolerance_pct    NUMERIC(6,3),
    cheap_source            TEXT    CHECK (cheap_source IN ('system_parts', 'light_scan')),
    status                  TEXT    NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'paused', 'retired')),
    last_checked_at         TIMESTAMPTZ,
    next_due_at             TIMESTAMPTZ,
    selected_by             TEXT    NOT NULL DEFAULT 'llm'
                                CHECK (selected_by IN ('llm', 'human')),
    iteration               INT     NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (database_name, table_name, variable)
);

CREATE INDEX IF NOT EXISTS idx_targets_due
    ON sentinel.monitor_targets (next_due_at)
    WHERE status = 'active';

-- ─── MONITOR: VARIABLE VALUES ────────────────────────────────────────────────
-- §5B. Tracks the value universe of a monitored variable for coverage checks.

CREATE TABLE IF NOT EXISTS sentinel.variable_values (
    id            SERIAL PRIMARY KEY,
    database_name TEXT NOT NULL,
    table_name    TEXT NOT NULL,
    variable      TEXT NOT NULL,
    value         TEXT NOT NULL,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lifecycle     TEXT NOT NULL DEFAULT 'active'
                      CHECK (lifecycle IN ('active', 'retired')),
    retired_at    TIMESTAMPTZ,
    UNIQUE (database_name, table_name, variable, value)
);

-- ─── MONITOR: RECONCILIATION RULES ────────────────────────────────────────────
-- §5B / §9. Generated autonomously; observe → active auto-promotion.

CREATE TABLE IF NOT EXISTS sentinel.reconciliation_rules (
    rule_id       SERIAL PRIMARY KEY,
    concept       TEXT NOT NULL,
    metric        TEXT NOT NULL,
    source_a      TEXT NOT NULL,      -- "db.table" or "db.table.column"
    source_b      TEXT NOT NULL,
    dimension     TEXT,               -- optional per-dimension breakdown
    tolerance_pct NUMERIC(6,3) NOT NULL DEFAULT 5.0,
    direction     TEXT NOT NULL DEFAULT 'a≈b'
                      CHECK (direction IN ('a≈b', 'a≥b', 'subset')),
    status        TEXT NOT NULL DEFAULT 'observe'
                      CHECK (status IN ('observe', 'active', 'muted')),
    stable_runs   INT NOT NULL DEFAULT 0,   -- consecutive in-tolerance runs → auto-promote
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- dimension is nullable; a table UNIQUE constraint can't wrap it in COALESCE,
-- so uniqueness (treating NULL dimension as its own value) is a unique index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recon_rule
    ON sentinel.reconciliation_rules
       (concept, metric, source_a, source_b, COALESCE(dimension, ''));

CREATE INDEX IF NOT EXISTS idx_recon_status
    ON sentinel.reconciliation_rules (status);

-- ─── MONITOR: CHECK RESULTS ──────────────────────────────────────────────────
-- §5B. One row per check run (append-only history).

CREATE TABLE IF NOT EXISTS sentinel.check_results (
    id                      BIGSERIAL PRIMARY KEY,
    run_ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    database_name           TEXT,
    table_name              TEXT,
    variable                TEXT,
    check_type              TEXT NOT NULL CHECK (check_type IN (
                                'freshness', 'volume', 'variable_coverage',
                                'schema_drift', 'reconciliation'
                            )),
    status                  TEXT NOT NULL CHECK (status IN ('ok', 'warn', 'fail')),
    observed                JSONB,        -- check-specific payload (expected vs actual, missing values, ...)
    monitor_frequency_weeks INT,
    duration_ms             INT,
    query_cost              BIGINT,       -- rows/bytes scanned, if available
    rule_id                 INT REFERENCES sentinel.reconciliation_rules(rule_id)
);

CREATE INDEX IF NOT EXISTS idx_check_results_run
    ON sentinel.check_results (run_ts DESC);
CREATE INDEX IF NOT EXISTS idx_check_results_target
    ON sentinel.check_results (database_name, table_name, check_type, run_ts DESC);

-- ─── MONITOR: INCIDENTS ──────────────────────────────────────────────────────
-- §5B. Stateful source of truth for "something is wrong": severity routes,
-- parent/children suppress, opened/resolved transitions dedup.
--
-- Alert bridge (resolves the "incidents have no path to email" gap): on OPEN,
-- the check engine also inserts one control_tower.alerts row and stores its UUID
-- in `alert_id` here. The existing ct-alerter job already polls control_tower.alerts
-- → MS Graph / SES email — so Sentinel reuses the live delivery path with no new
-- alerter job. incidents = stateful truth; control_tower.alerts = delivery queue.
-- BIGSERIAL PK here (vs alerts' UUID) is deliberate: incidents are stateful and
-- self-referencing (parent_incident_id), alerts are flat fire-and-forget rows.

CREATE TABLE IF NOT EXISTS sentinel.incidents (
    id                  BIGSERIAL PRIMARY KEY,
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    scope               JSONB NOT NULL,   -- {database_name, table, variable?, rule_id?}
    scope_level         TEXT NOT NULL CHECK (scope_level IN (
                            'table', 'variable', 'rule', 'schema'
                        )),
    check_type          TEXT NOT NULL,
    severity            TEXT NOT NULL DEFAULT 'warn'
                            CHECK (severity IN ('info', 'warn', 'critical')),
    parent_incident_id  BIGINT REFERENCES sentinel.incidents(id),
    rolled_up_children  INT NOT NULL DEFAULT 0,
    message             TEXT,
    check_result_id     BIGINT REFERENCES sentinel.check_results(id),
    alert_id            UUID REFERENCES control_tower.alerts(alert_id)  -- delivery bridge
);

CREATE INDEX IF NOT EXISTS idx_incidents_open
    ON sentinel.incidents (opened_at DESC)
    WHERE resolved_at IS NULL;
