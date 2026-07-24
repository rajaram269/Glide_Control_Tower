-- Migration 012 — Sentinel: separate freshness tolerance from check cadence.
-- Target: control_tower database (sentinel schema). Run after 011. Idempotent.
--
-- Lapse fix (A/B/C): monitor_frequency_weeks was doing two jobs — how often to RUN
-- the check (cheap-ops cadence) AND how stale = bad (freshness expectation). These
-- are different. A table checked weekly but expected to update daily must flag stale
-- after ~1 day, not a week. §5B of the design already separates them.
--
--   monitor_frequency_weeks  → check cadence only (how often the tick runs it), 1-4 int
--   expected_cadence_weeks   → freshness expectation (how often the DATA should update),
--                              DECIMAL so sub-week cadences are expressible:
--                              1 day = 0.1428, 2 hours = 0.0119, 1 week = 1.0
--
-- The check engine flags: fresh <= expected_cadence, stale <= 3x, else dead.
-- Falls back to monitor_frequency_weeks when expected_cadence_weeks IS NULL (so old
-- rows keep working). The legacy INTERVAL `freshness_tolerance` column stays for any
-- manual override but is no longer the primary driver.

ALTER TABLE sentinel.monitor_targets
  ADD COLUMN IF NOT EXISTS expected_cadence_weeks NUMERIC(8,4);

COMMENT ON COLUMN sentinel.monitor_targets.expected_cadence_weeks IS
  'How often the DATA is expected to update (decimal weeks; 0.1428 = daily). '
  'Freshness verdict uses this; falls back to monitor_frequency_weeks if NULL. '
  'Distinct from monitor_frequency_weeks (how often the check RUNS).';

COMMENT ON COLUMN sentinel.monitor_targets.monitor_frequency_weeks IS
  'How often the check RUNS (cheap-ops cadence, 1-4 weeks). NOT the freshness '
  'tolerance — see expected_cadence_weeks.';
