-- Migration 008 — Fix mis-registered job regions + add true job execution timestamp
--
-- Bug: 4 jobs registered with region=asia-south1 but actually deployed in
-- us-central1. Cloud Run Admin API silently returns 0 executions for the wrong
-- region/name combo (no error), so their job_status/duration stayed NULL forever.
UPDATE control_tower.registered_services
SET region = 'us-central1'
WHERE service_name IN (
  'crawler-indexer', 'crawler-processor', 'crawler-sync', 'ad-url-updater-sync-job'
) AND platform = 'cloud_run_job';

-- collected_at = when Control Tower scraped this row (hourly).
-- job_last_execution_at = when the job itself actually last finished running —
-- can be very different for infrequently-scheduled jobs. UI needs both.
ALTER TABLE control_tower.service_health
  ADD COLUMN IF NOT EXISTS job_last_execution_at TIMESTAMPTZ;
