#!/usr/bin/env python3
"""
Phase 3 setup: create control_tower database in ClickHouse Cloud.
Run once before configuring PeerDB CDC.

Usage:
    python scripts/setup_clickhouse.py

Reads credentials from GCP Secret Manager (same as deployed jobs).
Or set env vars: CH_HOST, CH_USER, CH_PASS
"""
import os, sys
import clickhouse_connect

CH_HOST = os.environ.get("CH_HOST", "htnicbsqm0.ap-south-1.aws.clickhouse.cloud")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASS")

if not CH_PASS:
    print("ERROR: Set CH_PASS env var (ClickHouse password)")
    sys.exit(1)

client = clickhouse_connect.get_client(
    host=CH_HOST, user=CH_USER, password=CH_PASS, port=8443, secure=True
)

print("Connected to ClickHouse:", CH_HOST)

# Create database
client.command("CREATE DATABASE IF NOT EXISTS control_tower")
print("✓ Created database: control_tower")

# Verify
dbs = client.query("SHOW DATABASES").result_rows
db_names = [r[0] for r in dbs]
assert "control_tower" in db_names, "Database creation failed"
print("✓ Verified control_tower database exists")
print()
print("Next step: configure PeerDB CDC pipe.")
print("See docs/peerdb_setup_guide.md for instructions.")
