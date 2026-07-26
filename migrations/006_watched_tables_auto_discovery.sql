-- Migration 006 — Support auto-discovered watched_tables
-- GCP collector scans ClickHouse for all PeerDB-mirrored tables (those with a
-- _peerdb_synced_at column) and registers them automatically. Manually seeded
-- rows keep their custom cadence (auto-discovery never overwrites).

ALTER TABLE control_tower.watched_tables
  ADD COLUMN IF NOT EXISTS auto_discovered BOOLEAN NOT NULL DEFAULT false;
