-- Per-VALUE expected cadence (e.g. one platform within a Platform variable).
-- variable_freshness previously judged every value of a variable against the same
-- table-wide cadence - a platform that genuinely reports weekly (not broken, just
-- slower by nature) got flagged "dead" on the same daily yardstick as Amazon/Flipkart.
-- NULL means "not enough history yet to infer one" - falls back to the existing
-- table/variable-level cadence, unchanged behavior for anything not yet inferred.

ALTER TABLE sentinel.variable_values
  ADD COLUMN IF NOT EXISTS expected_cadence_weeks NUMERIC;
