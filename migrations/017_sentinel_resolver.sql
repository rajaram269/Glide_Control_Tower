-- Migration 017 — Sentinel: allow the auto-resolver to author overlay rows.
-- Target: control_tower database (sentinel schema). Run after 016. Idempotent.
--
-- The auto-resolver (ct-sentinel-resolver) adjudicates needs_review rows with a third
-- LLM + evidence and auto-confirms the confident ones. It stamps updated_by='resolver'
-- (distinct from 'llm' = discovery, 'human' = manual pin) so its actions are traceable
-- and discovery still re-evaluates on a real structural change. Widen the CHECK.

ALTER TABLE sentinel.catalog_overlay
  DROP CONSTRAINT IF EXISTS catalog_overlay_updated_by_check;

ALTER TABLE sentinel.catalog_overlay
  ADD CONSTRAINT catalog_overlay_updated_by_check
  CHECK (updated_by IN ('llm', 'human', 'resolver'));
