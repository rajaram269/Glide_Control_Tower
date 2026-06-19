-- Migration 004 — Seed watched_tables for ClickHouse data freshness monitoring
-- These checks run after Phase 3 (PeerDB CDC) is live.
-- Update table/column names to match actual ClickHouse schema once PeerDB syncs.

INSERT INTO control_tower.watched_tables
  (database_name, table_name, freshness_column, expected_cadence_minutes, active)
VALUES
  -- RecruitBot data
  ('recruitment_hr', 'applicants',              'created_at',      60,   true),
  ('recruitment_hr', 'job_postings',            'created_at',      1440, true),
  ('recruitment_hr', 'resume_embeddings',       'created_at',      60,   true),

  -- Holistique e-commerce
  ('holistique_default_database', 'products',   'created_at',      1440, true),
  ('holistique_default_database', 'orders',     'created_at',      120,  true),
  ('holistique_default_database', 'inventory',  'updated_at',      120,  true),

  -- Performance Marketing (Meta ads sync)
  ('Performance_Marketing_meta_insights', 'meta_insights', 'date_start', 1440, true),

  -- Control Tower own health (confirms PeerDB pipe is alive)
  ('control_tower', 'service_health',           'collected_at',    65,   true),
  ('control_tower', 'cost_metrics',             'period_start',    1500, true)

ON CONFLICT (database_name, table_name) DO NOTHING;
