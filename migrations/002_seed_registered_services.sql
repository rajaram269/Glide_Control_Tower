-- Phase 1 seed: register all known Cloud Run services and jobs.
-- active=false by default — review and set active=true to begin monitoring.
-- Google AI Studio managed services excluded (not production workloads).

INSERT INTO control_tower.registered_services
    (service_name, platform, region, cloud, active)
VALUES
    -- Cloud Run Services (asia-south1)
    ('budgeter-app',                 'cloud_run_service', 'asia-south1', 'gcp', true),
    ('creative-dashboard-backend',   'cloud_run_service', 'asia-south1', 'gcp', true),
    ('creative-dashboard-frontend',  'cloud_run_service', 'asia-south1', 'gcp', true),
    ('holistique-api',               'cloud_run_service', 'asia-south1', 'gcp', true),
    ('holistique-ats-web',           'cloud_run_service', 'asia-south1', 'gcp', true),
    ('holistique-brand-intel-api',   'cloud_run_service', 'asia-south1', 'gcp', true),
    ('holistique-brand-intel-mcp',   'cloud_run_service', 'asia-south1', 'gcp', true),
    ('holistique-packing-list',      'cloud_run_service', 'asia-south1', 'gcp', true),
    ('po-validator-api',             'cloud_run_service', 'asia-south1', 'gcp', true),
    ('price-dashboard',              'cloud_run_service', 'asia-south1', 'gcp', true),
    ('resume-embed-api',             'cloud_run_service', 'asia-south1', 'gcp', true),
    ('resume-search-mcp',            'cloud_run_service', 'asia-south1', 'gcp', true),
    ('yt-dashboard',                 'cloud_run_service', 'asia-south1', 'gcp', true),
    -- Cloud Run Services (us-central1)
    ('ad-url-updater',               'cloud_run_service', 'us-central1', 'gcp', true),
    ('agenteye',                     'cloud_run_service', 'us-central1', 'gcp', true),
    ('librechat',                    'cloud_run_service', 'us-central1', 'gcp', false),
    ('open-webui',                   'cloud_run_service', 'us-central1', 'gcp', false),
    ('workstore-web',                'cloud_run_service', 'us-central1', 'gcp', true),
    ('youtube-desc-gen',             'cloud_run_service', 'us-central1', 'gcp', false),
    -- Cloud Run Jobs (GCP-managed, no region on list — defaulting asia-south1)
    ('ad-url-updater-sync-job',          'cloud_run_job', 'asia-south1', 'gcp', true),
    ('bc-3way-matcher-job',              'cloud_run_job', 'asia-south1', 'gcp', true),
    ('crawler-indexer',                  'cloud_run_job', 'asia-south1', 'gcp', true),
    ('crawler-processor',                'cloud_run_job', 'asia-south1', 'gcp', true),
    ('crawler-sync',                     'cloud_run_job', 'asia-south1', 'gcp', true),
    ('holistique-applicant-enrichment',  'cloud_run_job', 'asia-south1', 'gcp', true),
    ('holistique-ats-worker',            'cloud_run_job', 'asia-south1', 'gcp', true),
    ('holistique-linkedin-mail-fetch',   'cloud_run_job', 'asia-south1', 'gcp', true),
    ('holistique-linkedin-mail-mapping', 'cloud_run_job', 'asia-south1', 'gcp', true),
    ('price-scraper-job',                'cloud_run_job', 'asia-south1', 'gcp', true),
    ('resume-ingest-job',                'cloud_run_job', 'asia-south1', 'gcp', true),
    ('sync-blinkit-portal',              'cloud_run_job', 'asia-south1', 'gcp', true),
    ('sync-nykaa-orders',                'cloud_run_job', 'asia-south1', 'gcp', true),
    ('sync-packing-lists',               'cloud_run_job', 'asia-south1', 'gcp', true),
    ('sync-portal-data',                 'cloud_run_job', 'asia-south1', 'gcp', true),
    ('sync-sku-mapping',                 'cloud_run_job', 'asia-south1', 'gcp', true)
ON CONFLICT (service_name) DO NOTHING;
