-- Migration 023 — Sentinel: backfill legacy watched_tables as table-level targets.
-- Target: control_tower database (sentinel schema). Run after 022. Idempotent.
--
-- Sentinel discovery owns rich catalog metadata, but the legacy freshness path may
-- already contain active watched_tables that have no Sentinel table target yet
-- (notably *_resync tables). Backfill table-level targets from that proven legacy
-- allowlist so Sentinel freshness can run across the same operational universe.
-- These targets are sync-only until discovery later infers richer metadata.

INSERT INTO sentinel.monitor_targets
    (database_name, table_name, variable, monitor_frequency_weeks,
     expected_cadence_weeks, cheap_source, status, next_due_at, selected_by, iteration)
SELECT w.database_name,
       w.table_name,
       '',
       1,
       LEAST(8.0, GREATEST(0.001, w.expected_cadence_minutes::numeric / 10080.0)),
       'system_parts',
       'active',
       now() + INTERVAL '1 week',
       'human',
       0
FROM control_tower.watched_tables w
WHERE w.active = true
  AND NOT EXISTS (
      SELECT 1
      FROM sentinel.monitor_targets t
      WHERE t.database_name = w.database_name
        AND t.table_name = w.table_name
        AND COALESCE(t.variable, '') = ''
  )
ON CONFLICT (database_name, table_name, variable) DO NOTHING;
