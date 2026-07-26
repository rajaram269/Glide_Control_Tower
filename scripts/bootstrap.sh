#!/usr/bin/env bash
# bootstrap.sh — Full from-scratch control tower deployment.
# Idempotent. Run from repo root on a new GCP project.
# Prereqs: gcloud (authed), cloud-sql-proxy, psql, docker, jq
set -euo pipefail

PROJECT=seoai-479305
REGION=asia-south1
INSTANCE=agenteye-pg
SA_NAME=ct-collector
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "======================================"
echo " Control Tower Bootstrap"
echo " Project : $PROJECT"
echo " Region  : $REGION"
echo "======================================"

# ── 1. Enable required GCP APIs ───────────────────────────────────────────
echo ""
echo "[1/7] Enabling GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  cloudscheduler.googleapis.com \
  bigquery.googleapis.com \
  cloudbuild.googleapis.com \
  --project="$PROJECT" --quiet

# ── 2. Create service account ─────────────────────────────────────────────
echo ""
echo "[2/7] Creating service account $SA_EMAIL..."
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Control Tower Collector" \
  --project="$PROJECT" 2>/dev/null || echo "  Already exists."

# Grant required roles
for role in \
  roles/monitoring.viewer \
  roles/logging.viewer \
  roles/run.viewer \
  roles/run.invoker \
  roles/cloudscheduler.viewer \
  roles/cloudsql.client \
  roles/secretmanager.secretAccessor \
  roles/bigquery.dataViewer \
  roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$role" --quiet 2>/dev/null
  echo "  Granted $role"
done

# ── 3. Set up secrets ─────────────────────────────────────────────────────
echo ""
echo "[3/7] Setting up secrets..."
./scripts/setup_secrets.sh

# ── 4. Database + migrations ──────────────────────────────────────────────
echo ""
echo "[4/7] Setting up database..."
./scripts/setup_db.sh

# ── 5. Build and deploy Cloud Run jobs ────────────────────────────────────
echo ""
echo "[5/7] Deploying Cloud Run jobs..."

deploy_job() {
  local name=$1
  local dir=$2
  echo "  Building $name..."
  gcloud builds submit "$dir" \
    --tag="gcr.io/${PROJECT}/${name}:latest" \
    --project="$PROJECT" --quiet

  gcloud run jobs create "$name" \
    --image="gcr.io/${PROJECT}/${name}:latest" \
    --region="$REGION" \
    --service-account="$SA_EMAIL" \
    --set-cloudsql-instances="${PROJECT}:us-central1:${INSTANCE}" \
    --set-secrets="PG_CONN=ct-pg-connection-string:latest,\
CH_HOST=ct-clickhouse-host:latest,\
CH_USER=ct-clickhouse-user:latest,\
CH_PASS=ct-clickhouse-password:latest,\
OPENAI_API_KEY=ct-openai-api-key:latest,\
ANTHROPIC_API_KEY=ct-anthropic-api-key:latest,\
COHERE_API_KEY=ct-cohere-api-key:latest,\
HEALTHCHECK_URL=ct-healthchecks-${name}-ping-url:latest,\
EMAIL_TENANT_ID=ct-email-tenant-id:latest,\
EMAIL_CLIENT_ID=ct-email-client-id:latest,\
EMAIL_CLIENT_SECRET=ct-email-client-secret:latest,\
ALERT_EMAIL=ct-alert-email:latest" \
    --max-retries=2 \
    --task-timeout=600 \
    --project="$PROJECT" 2>/dev/null || \
  gcloud run jobs update "$name" \
    --image="gcr.io/${PROJECT}/${name}:latest" \
    --region="$REGION" --project="$PROJECT" --quiet
  echo "  Deployed $name"
}

deploy_job "ct-gcp-collector"  "collectors/gcp_collector"
deploy_job "ct-aws-collector"  "collectors/aws_collector"
deploy_job "ct-cost-collector" "collectors/cost_collector"
deploy_job "ct-anomaly-detector" "intelligence/anomaly_detector"
deploy_job "ct-alerter"        "intelligence/alerter"

# ── Sentinel jobs (data freshness/consistency monitor + catalog overlay) ──
# Discovery needs the LLM keys; both need PG + ClickHouse read. Deployed with a
# dedicated secret set (the generic deploy_job secret list lacks the sentinel keys).
deploy_sentinel_job() {
  local name=$1 dir=$2 extra_secrets=$3
  echo "  Building $name..."
  gcloud builds submit "$dir" \
    --tag="gcr.io/${PROJECT}/${name}:latest" \
    --project="$PROJECT" --quiet
  gcloud run jobs create "$name" \
    --image="gcr.io/${PROJECT}/${name}:latest" \
    --region="$REGION" \
    --service-account="$SA_EMAIL" \
    --set-cloudsql-instances="${PROJECT}:us-central1:${INSTANCE}" \
    --set-secrets="PG_CONN=ct-pg-connection-string:latest,\
CH_HOST=ct-clickhouse-host:latest,\
CH_USER=ct-clickhouse-user:latest,\
CH_PASS=ct-clickhouse-password:latest${extra_secrets}" \
    --max-retries=1 \
    --task-timeout=1800 \
    --project="$PROJECT" 2>/dev/null || \
  gcloud run jobs update "$name" \
    --image="gcr.io/${PROJECT}/${name}:latest" \
    --region="$REGION" --project="$PROJECT" --quiet
  echo "  Deployed $name"
}

# Discovery: + LLM cascade keys (OpenAI/Gemini/Claude). Add these secrets first.
deploy_sentinel_job "ct-sentinel-discovery" "sentinel/discovery" ",\
OPENAI_API_KEY=ct-sentinel-openai-key:latest,\
GEMINI_API_KEY=ct-sentinel-gemini-key:latest,\
ANTHROPIC_API_KEY=ct-sentinel-anthropic-key:latest"

# Check engine: PG + CH read only (no LLM).
deploy_sentinel_job "ct-sentinel-check" "sentinel/check_engine" ""

# To switch the alerter to AWS SES instead of MS Graph, add the ct-ses-* secrets
# then update ct-alerter (and ct-alerter-weekly):
#   gcloud run jobs update ct-alerter --region=$REGION --project=$PROJECT \
#     --update-env-vars=EMAIL_TRANSPORT=ses \
#     --update-secrets=SES_ACCESS_KEY_ID=ct-ses-access-key-id:latest,\
# SES_SECRET_ACCESS_KEY=ct-ses-secret-access-key:latest,SES_REGION=ct-ses-region:latest
# MS Graph remains the default (EMAIL_TRANSPORT unset → graph).

# Weekly digest = same alerter image with WEEKLY_DIGEST=true.
# Its scheduler trigger targets this job name, so it must exist.
gcloud run jobs create ct-alerter-weekly \
  --image="gcr.io/${PROJECT}/ct-alerter:latest" \
  --region="$REGION" \
  --service-account="$SA_EMAIL" \
  --set-cloudsql-instances="${PROJECT}:us-central1:${INSTANCE}" \
  --set-secrets="PG_CONN=ct-pg-connection-string:latest,\
ANTHROPIC_API_KEY=ct-anthropic-api-key:latest,\
EMAIL_TENANT_ID=ct-email-tenant-id:latest,\
EMAIL_CLIENT_ID=ct-email-client-id:latest,\
EMAIL_CLIENT_SECRET=ct-email-client-secret:latest,\
ALERT_EMAIL=ct-alert-email:latest" \
  --set-env-vars="WEEKLY_DIGEST=true,SENDER_EMAIL=ai@holistique.in" \
  --max-retries=2 --task-timeout=600 \
  --project="$PROJECT" 2>/dev/null || \
gcloud run jobs update ct-alerter-weekly \
  --image="gcr.io/${PROJECT}/ct-alerter:latest" \
  --region="$REGION" --project="$PROJECT" --quiet
echo "  Deployed ct-alerter-weekly"

# Deploy UI (Cloud Run service, not a job)
echo "  Building ct-ui..."
gcloud builds submit ui/backend \
  --tag="gcr.io/${PROJECT}/ct-ui:latest" \
  --project="$PROJECT" --quiet

gcloud run deploy ct-ui \
  --image="gcr.io/${PROJECT}/ct-ui:latest" \
  --region="$REGION" \
  --service-account="$SA_EMAIL" \
  --set-cloudsql-instances="${PROJECT}:us-central1:${INSTANCE}" \
  --set-secrets="PG_CONN=ct-pg-connection-string:latest" \
  --allow-unauthenticated \
  --min-instances=1 \
  --max-instances=2 \
  --memory=512Mi \
  --project="$PROJECT" --quiet
echo "  Deployed ct-ui"

# ── 6. Cloud Scheduler triggers ───────────────────────────────────────────
echo ""
echo "[6/7] Setting up Cloud Scheduler..."
SCHEDULER_SA="$SA_EMAIL"

create_schedule() {
  local job_name=$1
  local schedule=$2
  local description=$3
  gcloud scheduler jobs create http "trigger-${job_name}" \
    --location="$REGION" \
    --schedule="$schedule" \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${job_name}:run" \
    --http-method=POST \
    --oauth-service-account-email="$SCHEDULER_SA" \
    --description="$description" \
    --project="$PROJECT" 2>/dev/null \
    && echo "  Created schedule: $job_name ($schedule)" \
    || echo "  Schedule exists: $job_name"
}

# Collector at :00, anomaly detector at :30, alerter at :45
create_schedule "ct-gcp-collector"    "0 * * * *"            "GCP Collector — hourly"
create_schedule "ct-aws-collector"    "0 * * * *"            "AWS Collector — hourly"
create_schedule "ct-cost-collector"   "30 0 * * *"           "Cost Collector — daily 06:00 IST"
create_schedule "ct-anomaly-detector" "30 * * * *"           "Anomaly Detector — hourly at :30"
create_schedule "ct-alerter"          "45 * * * *"           "Alerter — hourly at :45"
create_schedule "ct-alerter-weekly"   "15 3 * * 1"           "Weekly cost digest — Mon 09:00 IST"

# Sentinel: discovery daily 06:45 IST (after cost collector), check tick daily 07:30 IST
create_schedule "ct-sentinel-discovery" "15 1 * * *"         "Sentinel discovery — daily 06:45 IST"
create_schedule "ct-sentinel-check"     "0 2 * * *"          "Sentinel check tick — daily 07:30 IST"

# ── 7. Verify ─────────────────────────────────────────────────────────────
echo ""
echo "[7/7] Verification..."
echo ""
echo "Cloud Run jobs:"
gcloud run jobs list --region="$REGION" --project="$PROJECT" \
  --filter="name:ct-" --format="table(name,status.observedGeneration)"

echo ""
echo "Cloud Scheduler:"
gcloud scheduler jobs list --location="$REGION" --project="$PROJECT" \
  --filter="name:trigger-ct-" --format="table(name,schedule,state)"

echo ""
echo "======================================"
echo " Bootstrap complete."
echo " Run a manual test: gcloud run jobs execute ct-gcp-collector --region=$REGION --project=$PROJECT"
echo "======================================"
