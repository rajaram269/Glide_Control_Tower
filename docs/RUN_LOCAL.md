# Running Control Tower locally

How to run the Control Tower dashboard on your own laptop (Windows / Mac / Linux),
with **no Google Cloud access needed**. Written for a developer taking over the codebase.

---

## 1. What you are running

Control Tower has two halves:

| Part | Folder | What it does | Runs locally? |
|---|---|---|---|
| **Dashboard (UI + API)** | `ui/backend/` | FastAPI server. `main.py` = REST API (`/api/*`), `index.html` = the whole frontend (Preact + htm, no build step) | ✅ Yes — this guide |
| Collectors | `collectors/` | Cloud Run jobs that scrape GCP / AWS / costs and write to Postgres | Needs real cloud credentials |
| Intelligence | `intelligence/` | Anomaly detector + alerter (emails) | Anomaly detector: yes (PG only). Alerter: needs email creds |
| Sentinel | `sentinel/` | Data-quality jobs against ClickHouse (discovery, checks, resolver) | Needs ClickHouse + LLM API keys |

**Key point:** the dashboard does not talk to Google Cloud directly. It only reads
from one Postgres database (schemas `control_tower` and `sentinel`). In production
the collectors/sentinel jobs fill that database; locally we fill it with sample data.

```
 production:   collectors / sentinel jobs  ──write──▶  Cloud SQL Postgres  ◀──read──  ui/backend
 local:        scripts/run_local.py (sample data) ──▶  local Postgres      ◀──read──  ui/backend (same code)
```

---

## 2. Prerequisites

- **Python 3.10+** (production uses 3.11 — see `ui/backend/Dockerfile`)
  Check: `python --version`
- **Git**
- That's it. You do **not** need Postgres, Docker or `gcloud` installed —
  `pgserver` (a pip package) ships its own Postgres binaries.

---

## 3. First-time setup

From the repo root (`Glide_Control_Tower/`):

**Windows (PowerShell)**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r ui\backend\requirements.txt
pip install pgserver
```

**Mac / Linux**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r ui/backend/requirements.txt
pip install pgserver
```

> If PowerShell blocks `Activate.ps1`, run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

`venv/` is already in `.gitignore`.

---

## 4. Start the dashboard

```bash
python scripts/run_local.py
```

Wait for:
```
INFO:     Uvicorn running on http://127.0.0.1:8080
```
Open **http://localhost:8080**. Stop with **Ctrl+C**.

### What `scripts/run_local.py` does

1. Starts an embedded Postgres, data stored in `~/.control_tower_local_pg`
   (your home folder, outside the repo).
2. **First run only:** applies every file in `migrations/` in order
   (`001_…` → `017_…`) to create the `control_tower` and `sentinel` schemas,
   then inserts 30 sample Cloud Run jobs (`sample-job-00` … `sample-job-29`:
   mix of succeeded / failed / no data).
3. Sets `PG_CONN` to the local database and starts the **unchanged**
   `ui/backend/main.py` with uvicorn on port 8080.
4. Re-launches Python in UTF-8 mode — on Windows, `main.py` reads `index.html`
   with the OS default encoding (cp1252), which fails on the emoji/symbols in the
   file. Linux (production) defaults to UTF-8, so this only matters locally.
5. Registers `.mjs` as `text/javascript` — Windows often maps it to `text/plain`,
   and browsers refuse to run the Preact modules → blank page.

The app code is identical to production; only the data is fake.

### What you'll see

| Page | Local content |
|---|---|
| Jobs | 30 sample jobs + 5 sample Cloud Schedulers (job / http / pubsub targets, one paused, one failing) |
| Job / service logs (click a row) | Shows *"Logs unavailable…"* — logs are read live from Google Cloud Logging, which needs GCP credentials. Works on the deployed dashboard (it uses its service account). Locally it only works if `gcloud auth application-default login` is done with an account that can read logs in `seoai-479305` |
| Services, Alerts, Costs, Freshness, Providers | Empty |
| Sentinel pages (Catalog, Review, Coverage, Reconcile, Incidents) | Empty |

Empty pages are expected — they load (HTTP 200) but nothing has collected data.
To see more, insert rows into the relevant table (section 6).

---

## 5. Day-to-day development

| You changed… | Do this |
|---|---|
| `ui/backend/index.html` (frontend) | Hard-refresh the browser: **Ctrl+Shift+R** (Mac: Cmd+Shift+R). No restart needed — HTML is read from disk on every request |
| `ui/backend/main.py` (API) | Ctrl+C, then `python scripts/run_local.py` again |
| Added a new file in `migrations/` (or pulled code that did — e.g. `018`) | Reset the local DB (below) so it's applied — otherwise pages reading the new columns return errors |
| `ui/backend/requirements.txt` changed | `pip install -r ui/backend/requirements.txt` again |

**Reset the local database** (wipes sample data, re-runs all migrations):
```powershell
# Windows
Remove-Item -Recurse -Force "$HOME\.control_tower_local_pg"
```
```bash
# Mac / Linux
rm -rf ~/.control_tower_local_pg
```
Then start again with `python scripts/run_local.py`.

**Quick API check** (while the server runs):
```bash
curl http://localhost:8080/api/jobs
curl http://localhost:8080/api/summary
```

---

## 6. Adding your own test data

Connect to the local DB with any Postgres client (DBeaver, pgAdmin, psql).
Get the connection string by adding `print(conn_str)` after `conn_str = pg.get_uri()`
in `scripts/run_local.py`, or insert data from Python:

```python
import pgserver, psycopg2
from pathlib import Path
pg = pgserver.get_server(Path.home() / ".control_tower_local_pg", cleanup_mode=None)
conn = psycopg2.connect(pg.get_uri()); conn.autocommit = True
cur = conn.cursor()
cur.execute("""
  INSERT INTO control_tower.alerts (...) VALUES (...)
""")
```

Table definitions live in `migrations/` — `001` for `control_tower.*`, `011` for `sentinel.*`.
The SQL each page reads is in the matching `@app.get("/api/...")` function in `ui/backend/main.py`.

> Note: the dashboard's POST actions (acknowledge alert, resolve/mark-static in
> Sentinel review) write to whatever DB `PG_CONN` points at. Locally that's
> the throwaway DB — safe to click.

---

## 7. Running without the helper script

`run_local.py` is only a convenience. The dashboard itself needs exactly one
environment variable, `PG_CONN`, pointing at a Postgres that has `migrations/*.sql` applied:

```powershell
# Windows
$env:PG_CONN = "postgresql://USER:PASSWORD@localhost:5432/control_tower"
$env:PYTHONUTF8 = "1"
cd ui\backend
uvicorn main:app --reload --port 8080
```
```bash
# Mac / Linux
export PG_CONN="postgresql://USER:PASSWORD@localhost:5432/control_tower"
cd ui/backend
uvicorn main:app --reload --port 8080
```

Use this if you install Postgres yourself or run it in Docker.

---

## 8. Running other components locally (optional)

All jobs are plain Python scripts (`python main.py`) configured by env vars.
Point `PG_CONN` at your local DB so they never touch production.

| Component | Env vars needed | Local-friendly? |
|---|---|---|
| `intelligence/anomaly_detector` | `PG_CONN` | ✅ Works against local DB |
| `sentinel/check_engine` | `PG_CONN`, `CH_HOST`, `CH_USER`, `CH_PASS` | Needs ClickHouse |
| `sentinel/discovery` | above + `OPENAI_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | Needs ClickHouse + LLM keys |
| `sentinel/resolver` | `PG_CONN`, `CH_HOST`, … | Needs ClickHouse. ⚠ Also needs `discovery_lib.py` and `llm.py`, which are **copied in at deploy time** (`scripts/deploy.sh` lines ~120) and gitignored: `cp sentinel/discovery/main.py sentinel/resolver/discovery_lib.py` and `cp sentinel/discovery/llm.py sentinel/resolver/llm.py` |
| `collectors/gcp_collector` | `PG_CONN`, `GCP_PROJECT`, `CH_*` + gcloud login | Needs GCP |
| `collectors/aws_collector` | `PG_CONN`, `AWS_*` | Needs AWS |
| `collectors/cost_collector` | `PG_CONN`, `GCP_PROJECT`, `BQ_BILLING_DATASET`, `AWS_*`, `CH_CLOUD_API_*`, LLM keys | Needs all providers |
| `intelligence/alerter` | `PG_CONN`, email transport vars (`EMAIL_*` or `SES_*`), `ALERT_EMAIL` | ⚠ Sends real emails |

Example:
```powershell
pip install -r intelligence\anomaly_detector\requirements.txt
$env:PG_CONN = "<local connection string>"
python intelligence\anomaly_detector\main.py
```

Never put real credentials in files inside the repo — `.env`, `credentials*.json`
and `service-account*.json` are gitignored for this reason.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `KeyError: 'PG_CONN'` | Started `main.py` directly without setting `PG_CONN` — use `run_local.py` or section 7 |
| Page returns **500**, log shows `UnicodeDecodeError: 'charmap'` | Windows encoding issue — use `run_local.py`, or set `PYTHONUTF8=1` before uvicorn |
| `address already in use` / port 8080 busy | An old server is still running. Windows: `Get-NetTCPConnection -LocalPort 8080` → `Stop-Process -Id <OwningProcess>` |
| Blank page, console says *"Expected a JavaScript module… MIME type text/plain"* | Windows registry maps `.mjs` to `text/plain`. `run_local.py` fixes this; if running uvicorn yourself, fix the registry: `HKEY_CLASSES_ROOT\.mjs` → `Content Type` = `text/javascript` |
| Blank page (other) | Check browser console (F12). Preact/htm are served from `ui/backend/vendor/` — make sure that folder exists |
| Changes to `index.html` not showing | Hard-refresh (Ctrl+Shift+R) |
| Migration error on first run | Reset the local DB (section 5); if it persists, a migration may use a Postgres feature `pgserver`'s version lacks |
| `ModuleNotFoundError` | venv not activated, or deps not installed (section 3) |

---

## 10. Deploying (for reference)

Production deploy is handled by `scripts/deploy.sh` (Cloud Run + Cloud SQL,
GCP project `seoai-479305`). It needs `gcloud` auth with access to that project.
Local development never needs it.
