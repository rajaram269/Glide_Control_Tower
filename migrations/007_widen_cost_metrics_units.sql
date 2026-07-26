-- Migration 007 — Widen cost_metrics.units_consumed
-- GCP billing export usage.amount is often in bytes (10^14+), overflowing NUMERIC(16,4).

ALTER TABLE control_tower.cost_metrics
  ALTER COLUMN units_consumed TYPE NUMERIC(30, 4);
