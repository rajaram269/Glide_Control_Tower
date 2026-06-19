-- Control Tower Phase 1 — Schema migration
-- Target: agenteye database, Cloud SQL agenteye-pg (seoai-479305:us-central1:agenteye-pg)
-- Run once. Idempotent (IF NOT EXISTS throughout).

CREATE SCHEMA IF NOT EXISTS control_tower;

-- ─── CONFIG TABLES ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS control_tower.registered_services (
    id                  SERIAL PRIMARY KEY,
    service_name        TEXT        NOT NULL UNIQUE,
    platform            TEXT        NOT NULL CHECK (platform IN (
                            'cloud_run_service', 'cloud_run_job', 'ec2', 'ec2_job'
                        )),
    region              TEXT        NOT NULL,
    cloud               TEXT        NOT NULL CHECK (cloud IN ('gcp', 'aws')),
    pipeline_id         TEXT,
    active              BOOLEAN     NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_tower.watched_tables (
    id                      SERIAL PRIMARY KEY,
    database_name           TEXT    NOT NULL,
    table_name              TEXT    NOT NULL,
    freshness_column        TEXT    NOT NULL,
    expected_cadence_minutes INT    NOT NULL,
    active                  BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (database_name, table_name)
);

CREATE TABLE IF NOT EXISTS control_tower.cost_budgets (
    id                      SERIAL PRIMARY KEY,
    cost_source             TEXT            NOT NULL,
    resource_name           TEXT            NOT NULL DEFAULT '*',
    monthly_budget_usd      NUMERIC(10, 2)  NOT NULL,
    warn_threshold_pct      INT             NOT NULL DEFAULT 70,
    critical_threshold_pct  INT             NOT NULL DEFAULT 90,
    active                  BOOLEAN         NOT NULL DEFAULT true,
    UNIQUE (cost_source, resource_name)
);

-- ─── OPERATIONAL DATA TABLES ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS control_tower.service_health (
    id                      BIGSERIAL PRIMARY KEY,
    service_name            TEXT            NOT NULL,
    platform                TEXT            NOT NULL,
    region                  TEXT            NOT NULL,
    collected_at            TIMESTAMPTZ     NOT NULL,
    request_count           BIGINT,
    error_count             BIGINT,
    error_rate_pct          NUMERIC(6, 3),
    p50_latency_ms          NUMERIC(10, 2),
    p95_latency_ms          NUMERIC(10, 2),
    p99_latency_ms          NUMERIC(10, 2),
    instance_count          INT,
    job_exit_code           INT,
    job_duration_ms         BIGINT,
    job_status              TEXT,
    cpu_utilization_pct     NUMERIC(6, 3),
    memory_utilization_pct  NUMERIC(6, 3),
    disk_utilization_pct    NUMERIC(6, 3),
    ec2_instance_id         TEXT,
    ec2_instance_state      TEXT,
    UNIQUE (service_name, collected_at)
);

CREATE TABLE IF NOT EXISTS control_tower.pipeline_events (
    id              BIGSERIAL PRIMARY KEY,
    pipeline_id     TEXT        NOT NULL,
    step_name       TEXT        NOT NULL,
    event_type      TEXT        NOT NULL CHECK (event_type IN (
                        'run_start', 'run_complete', 'run_failed', 'output_validated'
                    )),
    status          TEXT        NOT NULL CHECK (status IN ('ok', 'warn', 'error')),
    rows_written    BIGINT,
    rows_expected_min BIGINT,
    duration_ms     BIGINT,
    error_message   TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_tower.third_party_api_health (
    id                      BIGSERIAL PRIMARY KEY,
    provider                TEXT            NOT NULL,
    operation               TEXT,
    window_start            TIMESTAMPTZ     NOT NULL,
    window_end              TIMESTAMPTZ     NOT NULL,
    call_count_estimate     BIGINT,
    error_count             BIGINT,
    error_rate_pct          NUMERIC(6, 3),
    avg_latency_ms_estimate NUMERIC(10, 2),
    source                  TEXT            NOT NULL CHECK (source IN (
                                'cloud_logging', 'cloudwatch_logs', 'wrapper'
                            )),
    collected_at            TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_tower.data_freshness (
    id                      BIGSERIAL PRIMARY KEY,
    database_name           TEXT        NOT NULL,
    table_name              TEXT        NOT NULL,
    last_write_at           TIMESTAMPTZ,
    expected_cadence_minutes INT        NOT NULL,
    row_count_snapshot      BIGINT,
    row_count_delta         BIGINT,
    freshness_status        TEXT        NOT NULL CHECK (freshness_status IN ('fresh', 'stale', 'dead')),
    checked_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_tower.provider_status (
    id                  BIGSERIAL PRIMARY KEY,
    provider            TEXT        NOT NULL,
    status_page_url     TEXT        NOT NULL,
    overall_status      TEXT        NOT NULL CHECK (overall_status IN (
                            'operational', 'degraded', 'partial_outage', 'major_outage', 'unknown'
                        )),
    affected_components JSONB,
    polled_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_tower.cost_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    cost_source         TEXT            NOT NULL,
    resource_name       TEXT            NOT NULL,
    period_start        DATE            NOT NULL,
    period_end          DATE            NOT NULL,
    cost_usd            NUMERIC(12, 4)  NOT NULL,
    units_consumed      NUMERIC(16, 4),
    unit_type           TEXT,
    cost_per_unit       NUMERIC(12, 8),
    monthly_budget_usd  NUMERIC(10, 2),
    budget_pct_consumed NUMERIC(6, 2),
    UNIQUE (cost_source, resource_name, period_start)
);

CREATE TABLE IF NOT EXISTS control_tower.alerts (
    alert_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_type      TEXT        NOT NULL,
    severity        TEXT        NOT NULL CHECK (severity IN ('info', 'warn', 'critical')),
    service_name    TEXT,
    pipeline_id     TEXT,
    provider        TEXT,
    message         TEXT        NOT NULL,
    context_json    JSONB,
    fired_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ,
    ack_by          TEXT
);

-- ─── INDEXES ────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_service_health_service_collected
    ON control_tower.service_health (service_name, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_events_pipeline_occurred
    ON control_tower.pipeline_events (pipeline_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_alerts_unacked
    ON control_tower.alerts (fired_at DESC) WHERE acknowledged_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_data_freshness_checked
    ON control_tower.data_freshness (database_name, table_name, checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_cost_metrics_source_period
    ON control_tower.cost_metrics (cost_source, period_start DESC);

-- ─── SEED: cost_budgets (conservative initial estimates — tune after 2 weeks) ─

INSERT INTO control_tower.cost_budgets (cost_source, resource_name, monthly_budget_usd, warn_threshold_pct, critical_threshold_pct)
VALUES
    ('openai',           'text-embedding-3-large', 100.00, 70, 90),
    ('openai',           '*',                      200.00, 70, 90),
    ('replicate',        '*',                      150.00, 70, 90),
    ('clickhouse_cloud', '*',                      300.00, 70, 90),
    ('gcp_cloud_run',    '*',                       80.00, 70, 90),
    ('aws_ec2',          '*',                      120.00, 70, 90),
    ('anthropic',        '*',                       50.00, 70, 90)
ON CONFLICT (cost_source, resource_name) DO NOTHING;
