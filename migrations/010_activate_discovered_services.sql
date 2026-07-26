-- Migration 010 — Activate legit auto-discovered services/jobs that were
-- sitting inactive with nobody reviewing them.
--
-- auto_discover() defaults new finds to active=false as a safety net (don't
-- start alerting on something unvetted). 23 rows accumulated this way and
-- were never reviewed, making Control Tower blind to ~40% of real infra
-- (they're excluded from collection entirely while inactive).
--
-- Reviewed against `gcloud run jobs/services list` + Cloud Scheduler triggers:
-- these 19 are real, currently-scheduled Glide pipelines. The remaining 4
-- (librechat, open-webui, youtube-desc-gen, my-google-ai-studio-applet) were
-- deliberately excluded in the original build (shared tools / managed
-- services, not Glide's own pipelines) and stay inactive.

UPDATE control_tower.registered_services
SET active = true
WHERE service_name IN (
  -- jobs — all confirmed on active Cloud Scheduler triggers
  'creative-analysis-job', 'google-ads-sync-job', 'hector-sync', 'nykaa-ads-sync',
  'process-blinkit-asn', 'process-nykaa-invoices', 'process-zepto-asn', 'shopflo-sync',
  'vinculum-sync', 'zepto-fetch-packing-lists', 'zepto-po-merge', 'zepto-run-pipeline',
  -- services — real Glide services, not shared/managed tools
  'bharat-trends-scanner', 'hol-ai-skin-analyser', 'holistique-library-manager',
  'seo-geo-content-automator', 'skin-analyzer-dashboard',
  'report-platform-api', 'report-platform-frontend'
) AND active = false;
