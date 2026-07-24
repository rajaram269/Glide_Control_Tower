#!/usr/bin/env bash
# deploy.sh — Deploy the Sentinel subsystem into the existing Control Tower.
# Incremental (assumes bootstrap.sh already stood up the project, SA, base secrets).
# Idempotent. Run from repo root.
#
# What it does:
#   1. Preflight — verify required secrets exist (LLM keys; SES optional)
#   2. Migrations — apply 011–014 (sentinel schema) via cloud-sql-proxy
#   3. Build + deploy the two Sentinel Cloud Run jobs
#   4. Create the two Cloud Scheduler triggers
#   5. Redeploy the UI (Atlas-styled, new /api/sentinel/* endpoints)
#   6. Verify
#
# Prereqs: gcloud (authed), cloud-sql-proxy, psql, docker/buildpacks, jq
set -euo pipefail

PROJECT=seoai-479305
REGION=asia-south1
INSTANCE=agenteye-pg
SA_EMAIL="ct-collector@${PROJECT}.iam.gserviceaccount.com"
PROXY_PORT=${PROXY_PORT:-5432}

echo "======================================"
echo " Sentinel Deploy → $PROJECT / $REGION"
echo "======================================"

# ── 1. Preflight: required secrets ────────────────────────────────────────
echo ""
echo "[1/6] Checking secrets..."
REQUIRED_SECRETS=(
  ct-pg-connection-string ct-clickhouse-host ct-clickhouse-user ct-clickhouse-password
  ct-sentinel-openai-key ct-sentinel-gemini-key ct-sentinel-anthropic-key
)
missing=0
for s in "${REQUIRED_SECRETS[@]}"; do
  if gcloud secrets describe "$s" --project="$PROJECT" >/dev/null 2>&1; then
    echo "  ✓ $s"
  else
    echo "  ✗ MISSING: $s"; missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "ERROR: create missing secrets before deploy (LLM keys from the Atlas project)." >&2
  exit 1
fi
# SES is optional — alerter defaults to MS Graph when absent
for s in ct-ses-access-key-id ct-ses-secret-access-key ct-ses-region; do
  gcloud secrets describe "$s" --project="$PROJECT" >/dev/null 2>&1 \
    && echo "  ✓ $s (SES enabled)" || echo "  – $s absent (alerter stays on MS Graph)"
done

# ── 2. Migrations 011–014 ─────────────────────────────────────────────────
echo ""
echo "[2/6] Applying migrations (011–014, sentinel schema)..."
PG_CONN=$(gcloud secrets versions access latest --secret=ct-pg-connection-string --project="$PROJECT")
PG_PW=$(echo "$PG_CONN" | grep -oE '(:)[^:@]+(@)' | head -1 | tr -d ':@')
cloud-sql-proxy "${PROJECT}:us-central1:${INSTANCE}" --port="$PROXY_PORT" &
PROXY_PID=$!
trap "kill $PROXY_PID 2>/dev/null || true" EXIT
sleep 4
LOCAL="postgresql://agenteye_app:${PG_PW}@127.0.0.1:${PROXY_PORT}/control_tower"
for m in migrations/011_*.sql migrations/012_*.sql migrations/013_*.sql migrations/014_*.sql; do
  echo "  → $m"
  psql "$LOCAL" -v ON_ERROR_STOP=1 -qf "$m"
done
echo "  Sentinel schema:"
psql "$LOCAL" -tc "SELECT count(*) FROM information_schema.tables WHERE table_schema='sentinel'" \
  | xargs echo "    tables ="
kill $PROXY_PID 2>/dev/null || true; trap - EXIT

# ── 3. Build + deploy the two Sentinel jobs ───────────────────────────────
echo ""
echo "[3/6] Deploying Sentinel Cloud Run jobs..."
deploy_sentinel_job() {
  local name=$1 dir=$2 extra_secrets=$3 timeout=$4
  echo "  Building $name..."
  gcloud builds submit "$dir" --tag="gcr.io/${PROJECT}/${name}:latest" --project="$PROJECT" --quiet
  gcloud run jobs create "$name" \
    --image="gcr.io/${PROJECT}/${name}:latest" \
    --region="$REGION" --service-account="$SA_EMAIL" \
    --set-cloudsql-instances="${PROJECT}:us-central1:${INSTANCE}" \
    --set-secrets="PG_CONN=ct-pg-connection-string:latest,\
CH_HOST=ct-clickhouse-host:latest,CH_USER=ct-clickhouse-user:latest,\
CH_PASS=ct-clickhouse-password:latest${extra_secrets}" \
    --max-retries=1 --task-timeout="$timeout" \
    --project="$PROJECT" 2>/dev/null || \
  gcloud run jobs update "$name" \
    --image="gcr.io/${PROJECT}/${name}:latest" \
    --region="$REGION" --project="$PROJECT" --quiet
  echo "  Deployed $name"
}

# Discovery: LLM cascade keys + long timeout (LLM per changed table). 3600s ceiling.
deploy_sentinel_job "ct-sentinel-discovery" "sentinel/discovery" ",\
OPENAI_API_KEY=ct-sentinel-openai-key:latest,\
GEMINI_API_KEY=ct-sentinel-gemini-key:latest,\
ANTHROPIC_API_KEY=ct-sentinel-anthropic-key:latest" 3600

# Check engine: PG + CH read only, no LLM.
deploy_sentinel_job "ct-sentinel-check" "sentinel/check_engine" "" 1800

# ── 4. Cloud Scheduler triggers ───────────────────────────────────────────
echo ""
echo "[4/6] Creating Cloud Scheduler triggers..."
create_schedule() {
  local job=$1 sched=$2 desc=$3
  gcloud scheduler jobs create http "trigger-${job}" \
    --location="$REGION" --schedule="$sched" \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${job}:run" \
    --http-method=POST --oauth-service-account-email="$SA_EMAIL" \
    --description="$desc" --project="$PROJECT" 2>/dev/null \
    && echo "  Created trigger-${job} ($sched)" || echo "  trigger-${job} exists"
}
# Discovery 06:45 IST (after cost collector), check tick 07:30 IST
create_schedule "ct-sentinel-discovery" "15 1 * * *" "Sentinel discovery — daily 06:45 IST"
create_schedule "ct-sentinel-check"     "0 2 * * *"  "Sentinel check tick — daily 07:30 IST"

# ── 5. Redeploy UI (Atlas-styled + /api/sentinel/*) ───────────────────────
echo ""
echo "[5/6] Redeploying UI..."
gcloud builds submit ui/backend --tag="gcr.io/${PROJECT}/ct-ui:latest" --project="$PROJECT" --quiet
gcloud run deploy ct-ui \
  --image="gcr.io/${PROJECT}/ct-ui:latest" \
  --region="$REGION" --service-account="$SA_EMAIL" \
  --set-cloudsql-instances="${PROJECT}:us-central1:${INSTANCE}" \
  --set-secrets="PG_CONN=ct-pg-connection-string:latest" \
  --allow-unauthenticated --min-instances=1 --max-instances=2 --memory=512Mi \
  --project="$PROJECT" --quiet
echo "  UI: $(gcloud run services describe ct-ui --region=$REGION --project=$PROJECT --format='value(status.url)' 2>/dev/null)"

# ── 6. Verify ─────────────────────────────────────────────────────────────
echo ""
echo "[6/6] Verification..."
echo "Sentinel jobs:"
gcloud run jobs list --region="$REGION" --project="$PROJECT" \
  --filter="name:ct-sentinel" --format="table(name,status.observedGeneration)"
echo ""
echo "Sentinel triggers:"
gcloud scheduler jobs list --location="$REGION" --project="$PROJECT" \
  --filter="name:trigger-ct-sentinel" --format="table(name,schedule,state)"

echo ""
echo "======================================"
echo " Sentinel deploy complete."
echo " Manual first run (discovery may take a while on first full pass):"
echo "   gcloud run jobs execute ct-sentinel-discovery --region=$REGION --project=$PROJECT"
echo "   gcloud run jobs execute ct-sentinel-check      --region=$REGION --project=$PROJECT"
echo "======================================"
