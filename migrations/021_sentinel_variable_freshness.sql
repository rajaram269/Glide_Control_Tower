-- Adds 'variable_freshness' as a valid sentinel.check_results.check_type — the new
-- per-value freshness check (is each tracked value's own data still current, not just
-- "does this value exist anywhere in the table"). Widen the CHECK.

ALTER TABLE sentinel.check_results
  DROP CONSTRAINT IF EXISTS check_results_check_type_check;

ALTER TABLE sentinel.check_results
  ADD CONSTRAINT check_results_check_type_check
  CHECK (check_type IN (
      'freshness', 'volume', 'variable_coverage', 'variable_freshness',
      'schema_drift', 'reconciliation'
  ));
