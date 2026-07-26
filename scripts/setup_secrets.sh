#!/usr/bin/env bash
# setup_secrets.sh — Creates all Secret Manager secrets for the control tower.
# Run once per new GCP project.
#
# SECURITY: no secret values are hardcoded here. Values are read from the
# environment (optionally sourced from a gitignored file passed as $1, default
# ./.env.secrets). Anything not found in the environment is prompted for
# interactively (hidden input). Nothing secret is ever written to the repo.
#
# Usage:
#   ./scripts/setup_secrets.sh                 # prompt for everything missing
#   ./scripts/setup_secrets.sh .env.secrets    # source values from a gitignored file
#
# .env.secrets format (KEY=value per line; file is gitignored):
#   CT_CLICKHOUSE_PASSWORD=...
#   CT_ANTHROPIC_API_KEY=...
#   ... (see VARS below for the full list)
set -euo pipefail

PROJECT=seoai-479305
ENV_FILE="${1:-.env.secrets}"

if [ -f "$ENV_FILE" ]; then
  echo "Sourcing secret values from $ENV_FILE (gitignored)"
  set -a; source "$ENV_FILE"; set +a
fi

create_secret() {
  local name=$1 value=$2
  gcloud secrets create "$name" --project="$PROJECT" --replication-policy=automatic 2>/dev/null || true
  echo -n "$value" | gcloud secrets versions add "$name" --project="$PROJECT" --data-file=- >/dev/null
  echo "  [ok] $name"
}

# put_secret <secret-name> <ENV_VAR_NAME> <prompt text>
# uses $ENV_VAR if set, else prompts (hidden). Skips silently if left blank.
put_secret() {
  local secret=$1 var=$2 desc=$3
  local value="${!var:-}"
  if [ -z "$value" ]; then
    read -rsp "  ${desc} [${secret}] (blank to skip): " value; echo ""
  fi
  if [ -z "$value" ]; then echo "  [skip] $secret (no value)"; return; fi
  create_secret "$secret" "$value"
}

echo "=== Control Tower Secret Manager Setup ==="
echo "Project: $PROJECT"
echo ""

# ── Infra / connection ─────────────────────────────────────────────────────
put_secret "ct-pg-connection-string"  CT_PG_CONNECTION_STRING  "PostgreSQL connection string (Cloud Run socket format)"
put_secret "ct-clickhouse-host"        CT_CLICKHOUSE_HOST       "ClickHouse host"
put_secret "ct-clickhouse-user"        CT_CLICKHOUSE_USER       "ClickHouse user"
put_secret "ct-clickhouse-password"    CT_CLICKHOUSE_PASSWORD   "ClickHouse password"
put_secret "ct-clickhouse-cloud-api-key"    CT_CLICKHOUSE_CLOUD_API_KEY    "ClickHouse Cloud API key"
put_secret "ct-clickhouse-cloud-api-secret" CT_CLICKHOUSE_CLOUD_API_SECRET "ClickHouse Cloud API secret"

# ── AWS (optional) ─────────────────────────────────────────────────────────
put_secret "ct-aws-access-key-id"      CT_AWS_ACCESS_KEY_ID     "AWS Access Key ID"
put_secret "ct-aws-secret-access-key"  CT_AWS_SECRET_ACCESS_KEY "AWS Secret Access Key"

# ── LLM keys (base Control Tower) ──────────────────────────────────────────
put_secret "ct-anthropic-api-key"      CT_ANTHROPIC_API_KEY     "Anthropic API key"
put_secret "ct-openai-api-key"         CT_OPENAI_API_KEY        "OpenAI API key"
put_secret "ct-cohere-api-key"         CT_COHERE_API_KEY        "Cohere API key"

# ── Sentinel LLM cascade (OpenAI primary → Gemini → Claude) ────────────────
put_secret "ct-sentinel-openai-key"    CT_SENTINEL_OPENAI_KEY    "Sentinel OpenAI key"
put_secret "ct-sentinel-gemini-key"    CT_SENTINEL_GEMINI_KEY    "Sentinel Gemini key"
put_secret "ct-sentinel-anthropic-key" CT_SENTINEL_ANTHROPIC_KEY "Sentinel Anthropic key"

# ── Email: MS Graph (default) ──────────────────────────────────────────────
put_secret "ct-email-tenant-id"        CT_EMAIL_TENANT_ID       "MS Graph tenant id"
put_secret "ct-email-client-id"        CT_EMAIL_CLIENT_ID       "MS Graph client id"
put_secret "ct-email-client-secret"    CT_EMAIL_CLIENT_SECRET   "MS Graph client secret"
put_secret "ct-alert-email"            CT_ALERT_EMAIL           "Alert recipient email"

# ── Email: AWS SES (optional alternate transport) ──────────────────────────
put_secret "ct-ses-access-key-id"      CT_SES_ACCESS_KEY_ID     "SES access key id"
put_secret "ct-ses-secret-access-key"  CT_SES_SECRET_ACCESS_KEY "SES secret access key"
put_secret "ct-ses-region"             CT_SES_REGION            "SES region (e.g. ap-south-1)"

# ── Non-secret config (safe to hardcode) ───────────────────────────────────
create_secret "ct-bigquery-billing-dataset" "seoai-479305.billing_export"

# ── healthchecks.io ping URLs (optional) ───────────────────────────────────
put_secret "ct-healthchecks-gcp-ping-url"  CT_HC_GCP_URL  "healthchecks.io URL — GCP collector"
put_secret "ct-healthchecks-aws-ping-url"  CT_HC_AWS_URL  "healthchecks.io URL — AWS collector"
put_secret "ct-healthchecks-cost-ping-url" CT_HC_COST_URL "healthchecks.io URL — cost collector"

echo ""
echo "=== Done. Current ct- secrets: ==="
gcloud secrets list --project="$PROJECT" --filter="name:ct-" --format="table(name,createTime)"
