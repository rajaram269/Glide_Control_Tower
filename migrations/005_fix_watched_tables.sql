-- Migration 005 — Fix watched_tables: replace guessed names with actual ClickHouse table/column names
-- Migration 004 used assumed names (products, orders, applicants) — these don't exist.
-- This replaces them with confirmed names from ClickHouse system.tables.

-- Remove all rows seeded in migration 004 (guessed names)
DELETE FROM control_tower.watched_tables
WHERE (database_name, table_name) IN (
  ('recruitment_hr',                  'applicants'),
  ('recruitment_hr',                  'job_postings'),
  ('recruitment_hr',                  'resume_embeddings'),
  ('holistique_default_database',     'products'),
  ('holistique_default_database',     'orders'),
  ('holistique_default_database',     'inventory'),
  ('Performance_Marketing_meta_insights', 'meta_insights'),
  ('control_tower',                   'service_health'),
  ('control_tower',                   'cost_metrics')
);

-- Seed with confirmed table names and timestamp columns
INSERT INTO control_tower.watched_tables
  (database_name, table_name, freshness_column, expected_cadence_minutes, active)
VALUES
  -- RecruitBot: applicant vector index (runs ~daily after resume ingest job)
  ('recruitment_hr',              'applicant_vectors',                    'ingested_at',       1440, true),

  -- Holistique: marketplace orders (hourly PeerDB sync from Middleware)
  ('holistique_default_database', 'Holistique_BC_MW_Orders',             '_peerdb_synced_at', 120,  true),

  -- Holistique: Meta ad performance (daily sync)
  ('holistique_default_database', 'Performance_Marketing_meta_insight',  '_peerdb_synced_at', 1440, true),
  ('holistique_default_database', 'Performance_Marketing_meta_insight_region', '_peerdb_synced_at', 1440, true),

  -- Holistique: price/web scraper runs
  ('holistique_default_database', 'Web_Scrapers_Scraper_Run_Logs',        'created_at',        1440, true),

  -- Finance: sales invoice header (daily BC sync)
  ('Finance_Data',                'Finance_SalesInvoiceHeaderBIALL',     '_peerdb_synced_at', 1440, true)

ON CONFLICT (database_name, table_name) DO UPDATE SET
  freshness_column          = EXCLUDED.freshness_column,
  expected_cadence_minutes  = EXCLUDED.expected_cadence_minutes,
  active                    = EXCLUDED.active;
