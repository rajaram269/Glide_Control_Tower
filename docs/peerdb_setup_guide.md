# Phase 3: PeerDB CDC Setup Guide

Replicates the `control_tower` PostgreSQL schema to ClickHouse `control_tower` database.
This unblocks anomaly detector checks 1, 2, 4, 5 (currently skipped).

## Prerequisites

1. Run `python scripts/setup_clickhouse.py` to create the ClickHouse database.
2. PeerDB instance already running (confirmed — existing schemas have `_peerdb_*` columns).

## Step 1 — Add PostgreSQL peer (source)

In PeerDB UI → Peers → New Peer → PostgreSQL:

| Field | Value |
|-------|-------|
| Name | `ct-postgres` |
| Host | `/cloudsql/seoai-479305:us-central1:agenteye-pg` |
| Port | `5432` |
| Database | `control_tower` |
| User | `agenteye_app` |
| Password | (from `ct-pg-connection-string` secret) |
| SSL Mode | `disable` (Cloud SQL proxy handles TLS) |

> If PeerDB runs outside GCP, use the public IP + SSL cert instead of the socket path.

## Step 2 — Add ClickHouse peer (destination)

PeerDB UI → Peers → New Peer → ClickHouse:

| Field | Value |
|-------|-------|
| Name | `ct-clickhouse` |
| Host | `htnicbsqm0.ap-south-1.aws.clickhouse.cloud` |
| Port | `9440` (native TLS) |
| Database | `control_tower` |
| User | `default` |
| Password | (from `ct-clickhouse-password` secret) |
| SSL | enabled |

## Step 3 — Create CDC mirror

PeerDB UI → Mirrors → New Mirror → CDC:

| Field | Value |
|-------|-------|
| Mirror name | `ct-pg-to-ch` |
| Source peer | `ct-postgres` |
| Destination peer | `ct-clickhouse` |
| Publication name | `ct_control_tower_pub` (PeerDB creates it) |
| Replication slot | `ct_control_tower_slot` (PeerDB creates it) |

**Tables to replicate** (select all under schema `control_tower`):

```
control_tower.service_health
control_tower.provider_status
control_tower.third_party_api_health
control_tower.data_freshness
control_tower.pipeline_events
control_tower.alerts
control_tower.cost_metrics
control_tower.registered_services
control_tower.watched_tables
control_tower.cost_budgets
```

**Destination table naming**: PeerDB auto-creates tables in `control_tower` ClickHouse database with the same names, adding:
- `_peerdb_is_deleted UInt8`
- `_peerdb_version Int64`

## Step 4 — Verify replication

After starting the mirror, wait ~5 minutes then verify:

```sql
-- Run in ClickHouse (via clickhouse-client or UI)
SELECT count() FROM control_tower.service_health;
SELECT count() FROM control_tower.provider_status;
```

Both should return row counts matching PostgreSQL.

Also verify the anomaly detector query pattern works:
```sql
SELECT service_name, avg(error_rate_pct)
FROM control_tower.service_health
WHERE collected_at >= now() - INTERVAL 2 HOUR
GROUP BY service_name
HAVING argMax(_peerdb_is_deleted, _peerdb_version) = 0
LIMIT 5;
```

## Step 5 — Confirm anomaly detector unblocked

Trigger a manual run:
```bash
gcloud run jobs execute ct-anomaly-detector --region=asia-south1 --project=seoai-479305 --wait
```

Logs should show `ClickHouse control_tower replica: ready.` instead of the warning.

## Seed watched_tables (already done via migration 004)

Migration 004 seeded 9 ClickHouse tables for freshness monitoring. The GCP collector's
`check_data_freshness` step will start writing to `data_freshness` once these tables exist
in ClickHouse.
