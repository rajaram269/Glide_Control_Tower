-- Migration 013 — Sentinel: structured, actionable table description.
-- Target: control_tower database (sentinel schema). Run after 012. Idempotent.
--
-- The single free-text `summary` was too thin to be actionable — "Order records
-- for Holistique brands" tells a consumer (RBAC MCP / an LLM querying the data)
-- nothing about how to use the table correctly. Replace it with a structured
-- description a consumer can act on directly:
--   what_it_is       — one precise line
--   purpose          — why it exists / what questions it answers
--   key_facts        — array: grain, freshness, units, gotchas
--   when_to_use      — the questions this table is the right source for
--   when_not_to_use  — traps / wrong-source cases (e.g. "not deduped = double-count")
--
-- Stored as one JSONB column (no per-field migration churn). `summary` stays as a
-- generated one-liner for list views / backward-compat (populated from what_it_is).

ALTER TABLE sentinel.catalog_overlay
  ADD COLUMN IF NOT EXISTS description JSONB;

COMMENT ON COLUMN sentinel.catalog_overlay.description IS
  'Structured actionable description: {what_it_is, purpose, key_facts[], '
  'when_to_use, when_not_to_use}. Consumer-facing decision surface. `summary` '
  'remains a one-line derivative for compact list views.';
