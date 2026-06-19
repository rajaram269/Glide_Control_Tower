-- Migration 003 — Extend pipeline_events for CT_METRICS processing metrics
-- Adds: rows_failed, metadata_json (arbitrary extra metrics), execution_id (Cloud Run execution name)
-- Idempotent via ADD COLUMN IF NOT EXISTS.

ALTER TABLE control_tower.pipeline_events
  ADD COLUMN IF NOT EXISTS rows_failed    BIGINT,
  ADD COLUMN IF NOT EXISTS metadata_json  JSONB,
  ADD COLUMN IF NOT EXISTS execution_id   TEXT;

-- Index for deduplication: GCP collector checks execution_id before inserting
CREATE INDEX IF NOT EXISTS idx_pipeline_events_execution_id
  ON control_tower.pipeline_events (execution_id)
  WHERE execution_id IS NOT NULL;
