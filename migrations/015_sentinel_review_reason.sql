-- Migration 015 — Sentinel: store WHY a table needs review (UI clarity).
-- Target: control_tower database (sentinel schema). Run after 014. Idempotent.
--
-- "needs review" alone was confusing in the UI. Store a human-readable reason
-- (maker/checker disagreement on source_type or dedup, or low confidence) so the
-- Authority tab explains what to look at.

ALTER TABLE sentinel.catalog_overlay
  ADD COLUMN IF NOT EXISTS review_reason TEXT;

COMMENT ON COLUMN sentinel.catalog_overlay.review_reason IS
  'Why review_status=needs_review: e.g. "maker/checker disagree — source_type: '
  'openai=erp vs gemini=finance", or "low confidence (0.62 < 0.80)". NULL when confirmed.';
