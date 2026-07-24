-- Migration 014 — Sentinel: static (intentionally-frozen) table handling.
-- Target: control_tower database (sentinel schema). Run after 013. Idempotent.
--
-- Some tables are intentionally static — a fixed pincode/country/GL master that is
-- loaded once and rarely or never refreshed. Freshness-by-cadence would eventually
-- flag these 'dead' (a false alarm) no matter how long the tolerance. Mark them
-- `is_static` so the check engine reports freshness as INFO (last_write shown, no
-- stale/dead verdict, no incident) instead of failing them.
--
-- is_static is inferred by the LLM from content (a pure reference/lookup with no
-- event/timestamp semantics and long expected cadence) and can be pinned by a human.

ALTER TABLE sentinel.catalog_overlay
  ADD COLUMN IF NOT EXISTS is_static BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE sentinel.monitor_targets
  ADD COLUMN IF NOT EXISTS is_static BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN sentinel.catalog_overlay.is_static IS
  'Table is intentionally frozen (fixed reference/lookup, load-once). Freshness is '
  'reported as info (never stale/dead) so a static table does not false-alarm.';
