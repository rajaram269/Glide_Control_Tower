#!/usr/bin/env bash
# setup_secrets.sh — Creates all Secret Manager secrets for the control tower.
# Run once per new GCP project. Prompts for values not yet known.
# Usage: ./scripts/setup_secrets.sh
set -euo pipefail

PROJECT=seoai-479305

create_secret() {
  local name=$1
  local value=$2
  gcloud secrets create "$name" --project="$PROJECT" \
    --replication-policy=automatic 2>/dev/null || true
  echo -n "$value" | gcloud secrets versions add "$name" \
    --project="$PROJECT" --data-file=- 2>&1 | tail -1
  echo "  [ok] $name"
}

prompt_secret() {
  local name=$1
  local description=$2
  read -rsp "  $description: " value
  echo ""
  create_secret "$name" "$value"
}

echo "=== Control Tower Secret Manager Setup ==="
echo "Project: $PROJECT"
echo ""

# ── Credentials you must supply interactively ──────────────────────────────
echo "--- Required credentials (will prompt) ---"

prompt_secret "ct-pg-connection-string" \
  "PostgreSQL connection string (Cloud Run socket format)"

prompt_secret "ct-aws-access-key-id" \
  "AWS Access Key ID (for CloudWatch + Cost Explorer)"

prompt_secret "ct-aws-secret-access-key" \
  "AWS Secret Access Key"

prompt_secret "ct-healthchecks-gcp-ping-url" \
  "healthchecks.io ping URL for GCP collector"

prompt_secret "ct-healthchecks-aws-ping-url" \
  "healthchecks.io ping URL for AWS collector"

prompt_secret "ct-healthchecks-cost-ping-url" \
  "healthchecks.io ping URL for cost collector"

# ── Static credentials (hardcoded for this deployment) ─────────────────────
echo ""
echo "--- Static credentials ---"

create_secret "ct-clickhouse-host"     "htnicbsqm0.ap-south-1.aws.clickhouse.cloud"
create_secret "ct-clickhouse-user"     "default"
create_secret "ct-clickhouse-password" "7kF7z.TS3gBj2"

create_secret "ct-clickhouse-cloud-api-key"    "JNU3kwXqwYqOfy8WESNJ"
create_secret "ct-clickhouse-cloud-api-secret" "4b1dpvPP7ifTZzttgEIKfgtRHlspy4WLjgjJZVQU5V"

create_secret "ct-anthropic-api-key" \
  "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"

create_secret "ct-openai-api-key" \
  "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"

create_secret "ct-cohere-api-key" "AOiayYEeZKuedHVwM7ll3qpStFHpq6BfskL9PXck"

create_secret "ct-email-tenant-id"     "2cc477df-31d6-4498-bbeb-0f68fa05821f"
create_secret "ct-email-client-id"     "72692f8b-6524-4ff3-8bda-8a4ad232f793"
create_secret "ct-email-client-secret" "ueA8Q~F5uFO0GAqC4MYYR7yFSU.DEg78qukVFc5g"
create_secret "ct-alert-email"         "rajaram.vennam@glidebrands.in"

create_secret "ct-bigquery-billing-dataset" "seoai-479305.billing_export"

echo ""
echo "=== All secrets created. Verify: ==="
gcloud secrets list --project="$PROJECT" --filter="name:ct-" \
  --format="table(name,createTime)"
