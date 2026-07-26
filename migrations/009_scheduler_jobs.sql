-- Migration 009 — Track Cloud Scheduler jobs whose target is a plain HTTP
-- endpoint (not a Cloud Run Job :run call). Those already show up in
-- service_health via job_last_execution_at; this table covers the ones that
-- don't — cron logic living inside a Cloud Run *service* (e.g. agenteye's
-- /api/cron/* routes), invisible to Control Tower any other way.

CREATE TABLE IF NOT EXISTS control_tower.scheduler_jobs (
    id                    SERIAL PRIMARY KEY,
    scheduler_name        TEXT        NOT NULL UNIQUE,
    schedule              TEXT,
    target_uri            TEXT,
    region                TEXT        NOT NULL,
    active                BOOLEAN     NOT NULL DEFAULT true,
    last_attempt_at       TIMESTAMPTZ,
    last_attempt_status   TEXT,
    checked_at            TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
