-- Migration 016 — Sentinel: DATA freshness (business date) vs SYNC freshness.
-- Target: control_tower database (sentinel schema). Run after 015. Idempotent.
--
-- Two distinct freshnesses:
--   SYNC freshness — when a row was last written/ingested (_peerdb_synced_at /
--     system.parts). Answers "is the pipeline running?".
--   DATA freshness — the newest BUSINESS/EVENT date in the data (max SalesDate).
--     Answers "is the data itself current?". Catches the case where a sales table
--     synced today but its newest SalesDate is May (2 months of stale business data).
--
-- event_date_column is the LLM-identified business date column (null when the table
-- has no business date — dimension/reference/snapshot tables get sync freshness only).

ALTER TABLE sentinel.monitor_targets
  ADD COLUMN IF NOT EXISTS event_date_column TEXT;

ALTER TABLE sentinel.catalog_overlay
  ADD COLUMN IF NOT EXISTS event_date_column TEXT;

COMMENT ON COLUMN sentinel.monitor_targets.event_date_column IS
  'Business/event date column (SalesDate, order_date, posting_date). Drives DATA '
  'freshness (max(event_date) — is the data current). NULL = no business date, '
  'table gets SYNC freshness only. Distinct from _peerdb_synced_at (sync freshness).';
