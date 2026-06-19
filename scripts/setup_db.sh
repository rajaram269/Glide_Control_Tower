#!/usr/bin/env bash
# setup_db.sh — Creates the control_tower database and runs all migrations.
# Run from repo root. Requires: gcloud auth, cloud-sql-proxy, psql.
# Usage: ./scripts/setup_db.sh [--proxy-port PORT]
set -euo pipefail

PROJECT=seoai-479305
INSTANCE=agenteye-pg
DB_NAME=control_tower
DB_USER=agenteye_app
PROXY_PORT=${2:-5432}

echo "=== Control Tower DB Setup ==="
echo "Project:  $PROJECT"
echo "Instance: $INSTANCE"
echo "Database: $DB_NAME"

# ── 1. Fetch password from Secret Manager ──────────────────────────────────
echo "Fetching credentials..."
PG_CONN=$(gcloud secrets versions access latest \
  --secret=ct-pg-connection-string --project="$PROJECT")

# ── 2. Create database (idempotent) ────────────────────────────────────────
echo "Creating database $DB_NAME (if not exists)..."
gcloud sql databases create "$DB_NAME" \
  --instance="$INSTANCE" --project="$PROJECT" 2>/dev/null \
  && echo "  Created." || echo "  Already exists, skipping."

# ── 3. Start Cloud SQL Auth Proxy ──────────────────────────────────────────
echo "Starting Cloud SQL Auth Proxy on port $PROXY_PORT..."
cloud-sql-proxy "$PROJECT:us-central1:$INSTANCE" --port="$PROXY_PORT" &
PROXY_PID=$!
trap "kill $PROXY_PID 2>/dev/null || true" EXIT
sleep 3

# ── 4. Run migrations in order ─────────────────────────────────────────────
LOCAL_CONN="postgresql://${DB_USER}:$(echo "$PG_CONN" | grep -oP '(?<=:)[^@]+(?=@)')@127.0.0.1:${PROXY_PORT}/${DB_NAME}"

for migration in migrations/*.sql; do
  echo "Running $migration..."
  psql "$LOCAL_CONN" -f "$migration"
  echo "  Done."
done

echo ""
echo "=== Setup complete. Tables in control_tower schema: ==="
psql "$LOCAL_CONN" -c "\dt control_tower.*"
