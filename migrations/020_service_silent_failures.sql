-- Migration 020 — deeper service monitoring: catch problems a plain 5xx-rate
-- check misses.
--   error_log_count — app-level ERROR-severity log lines with no 5xx attached,
--     i.e. the service answered 200 but logged an internal error itself
--     ("silent failure"). NULL = couldn't check that run; 0 = checked, none
--     found (the common case today — most services don't log at this level
--     yet); >0 = found some.
--   bad_endpoints — JSON array of specific URL paths that are failing badly
--     (>=3 requests and >=50% erroring) even while the service's OVERALL
--     error rate looks fine. NULL = couldn't check; '[]' = checked, all
--     endpoints healthy; non-empty = at least one endpoint is broken.

ALTER TABLE control_tower.service_health
    ADD COLUMN IF NOT EXISTS error_log_count INTEGER,
    ADD COLUMN IF NOT EXISTS bad_endpoints   JSONB;
