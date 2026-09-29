-- Migration 019 — store each Cloud Scheduler job's IANA time zone (e.g.
-- "Etc/UTC", "Asia/Kolkata"). Needed to correctly compute "when should this
-- cron have fired next" for missed-schedule detection — a cron expression
-- like "0 6 * * *" means a different UTC instant depending on the schedule's
-- own time zone.

ALTER TABLE control_tower.scheduler_jobs
    ADD COLUMN IF NOT EXISTS time_zone TEXT;
