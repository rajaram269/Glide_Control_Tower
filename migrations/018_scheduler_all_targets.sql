-- Migration 018 — scheduler_jobs now tracks EVERY Cloud Scheduler job in every
-- region (not just HTTP-direct ones in 3 regions), so each row records what it
-- triggers and whether it is paused.
--   target_type: 'cloud_run_job' | 'http' | 'pubsub' | 'app_engine'
--   target_name: Cloud Run job name for cloud_run_job, pubsub topic for pubsub, else NULL
--   state:       ENABLED | PAUSED | DISABLED | UPDATE_FAILED (Cloud Scheduler Job.State)

ALTER TABLE control_tower.scheduler_jobs
    ADD COLUMN IF NOT EXISTS target_type TEXT,
    ADD COLUMN IF NOT EXISTS target_name TEXT,
    ADD COLUMN IF NOT EXISTS state       TEXT;
