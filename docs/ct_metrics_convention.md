# CT_METRICS Logging Convention

Add processing metrics from any Cloud Run job to the Control Tower `pipeline_events` table.
The GCP collector scans Cloud Logging hourly for these log lines.

## How to emit metrics

Add **one log line** at the end of your job's main function:

```python
import json, logging
log = logging.getLogger(__name__)

# At the end of your job:
log.info("CT_METRICS: " + json.dumps({
    "records_processed": total_attempted,   # int — total records the job tried to handle
    "records_success":   total_succeeded,   # int — records successfully written/indexed
    "records_failed":    total_failed,      # int — errors / skipped / retried-and-failed
    "step":              "main",            # str — optional, defaults to "main"
    "duration_ms":       elapsed_ms,        # int — optional wall-clock time in ms
}))
```

### Minimal example (just processed + failed)

```python
log.info("CT_METRICS: " + json.dumps({
    "records_processed": len(items),
    "records_failed": error_count,
}))
```

### Multi-step example

If your job has multiple phases (fetch → embed → index), emit one line per phase:

```python
log.info("CT_METRICS: " + json.dumps({"records_processed": fetched, "step": "fetch"}))
log.info("CT_METRICS: " + json.dumps({"records_processed": embedded, "step": "embed"}))
log.info("CT_METRICS: " + json.dumps({"records_processed": indexed, "records_failed": failed, "step": "index"}))
```

## Priority services to add this to

| Service | What to count |
|---------|--------------|
| `resume-ingest-job` | `records_processed` = resumes fetched, `records_failed` = embed/index errors |
| `crawler-processor` | `records_processed` = images processed, `records_failed` = crawl errors |
| `crawler-indexer` | `records_processed` = items indexed |
| `bc-3way-matcher-job` | `records_processed` = orders matched |
| `sync-nykaa-orders` / `sync-blinkit-portal` etc | `records_processed` = orders synced |
| `price-scraper-job` | `records_processed` = SKUs scraped |

## What gets stored

Each `CT_METRICS:` line → one row in `control_tower.pipeline_events`:

| Column | Source |
|--------|--------|
| `pipeline_id` | Cloud Run job name |
| `step_name` | `step` field (default "main") |
| `execution_id` | Cloud Run execution name (auto-deduplicates) |
| `rows_written` | `records_success` or `records_processed` |
| `rows_failed` | `records_failed` |
| `duration_ms` | `duration_ms` field |
| `status` | `ok` if rows_failed == 0, else `error` |
| `metadata_json` | any extra fields |
| `occurred_at` | log timestamp |

## Viewing the data

Control Tower UI → Pipeline tab, or:

```sql
SELECT pipeline_id, step_name, rows_written, rows_failed, duration_ms, occurred_at
FROM control_tower.pipeline_events
ORDER BY occurred_at DESC
LIMIT 50;
```
