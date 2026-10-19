"""
run_local.py — run the Control Tower UI fully offline (no Google Cloud).

Starts an embedded Postgres (pip install pgserver), applies migrations/*.sql on
first run, seeds sample Cloud Run job rows, then serves the UI on :8080.

Usage (from repo root):  python scripts/run_local.py
Reset the local DB:      delete ~/.control_tower_local_pg
"""
import os, random, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# main.py reads index.html with the default encoding — cp1252 on Windows breaks it
if not sys.flags.utf8_mode:
    sys.exit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))

import pgserver
import psycopg2

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path.home() / ".control_tower_local_pg"

pg = pgserver.get_server(DATA_DIR, cleanup_mode=None)  # keep server data between runs
conn_str = pg.get_uri()

conn = psycopg2.connect(conn_str)
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT to_regclass('control_tower.service_health')")
if cur.fetchone()[0] is None:
    for sql in sorted((ROOT / "migrations").glob("*.sql")):
        print(f"applying {sql.name}")
        cur.execute(sql.read_text(encoding="utf-8"))

    # sample jobs: mostly succeeded, a few failed, a few never run
    now = datetime.now(timezone.utc)
    rng = random.Random(42)
    for i in range(30):
        status = "failed" if i % 7 == 3 else None if i % 11 == 5 else "succeeded"
        last_run = None if status is None else now - timedelta(hours=rng.randint(1, 40))
        cur.execute("""
            INSERT INTO control_tower.service_health
                (service_name, platform, region, job_status, job_duration_ms,
                 job_last_execution_at, collected_at)
            VALUES (%s, 'cloud_run_job', %s, %s, %s, %s, %s)
        """, (f"sample-job-{i:02d}", rng.choice(["asia-south1", "us-central1"]),
              status, rng.randint(10_000, 400_000) if status else None,
              last_run, now - timedelta(hours=1)))
    for i, (ttype, tname, uri, state, status) in enumerate([
        ("cloud_run_job", "sample-job-00", "https://run.googleapis.com/.../jobs/sample-job-00:run", "ENABLED", "ok"),
        ("cloud_run_job", "sample-job-03", "https://run.googleapis.com/.../jobs/sample-job-03:run", "ENABLED", "error (code 13)"),
        ("http", None, "https://sample-service.run.app/api/cron/daily", "ENABLED", "ok"),
        ("http", None, "https://sample-service.run.app/api/cron/weekly", "PAUSED", "ok"),
        ("pubsub", "sample-topic", "projects/local/topics/sample-topic", "ENABLED", "never run"),
    ]):
        cur.execute("""
            INSERT INTO control_tower.scheduler_jobs
                (scheduler_name, schedule, target_uri, region, active, last_attempt_at,
                 last_attempt_status, checked_at, target_type, target_name, state)
            VALUES (%s, '0 * * * *', %s, 'asia-south1', true, %s, %s, %s, %s, %s, %s)
        """, (f"sample-scheduler-{i}", uri,
              None if status == "never run" else now - timedelta(hours=i + 1),
              status, now - timedelta(hours=1), ttype, tname, state))
    print("seeded sample jobs + schedulers")
conn.close()

os.environ["PG_CONN"] = conn_str
os.chdir(ROOT / "ui" / "backend")
sys.path.insert(0, os.getcwd())

# Windows registry often maps .mjs to text/plain; browsers refuse to run
# module scripts served that way (blank page). Linux gets this right.
import mimetypes
mimetypes.add_type("text/javascript", ".mjs")

import uvicorn
print("Control Tower: http://localhost:8080")
uvicorn.run("main:app", host="127.0.0.1", port=8080, reload=False)
