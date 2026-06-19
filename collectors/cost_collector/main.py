"""
Cost Collector — Control Tower
Daily Cloud Run job (06:00 IST = 00:30 UTC). Collects spend from GCP BigQuery billing
export, AWS Cost Explorer, ClickHouse Cloud API, OpenAI, Anthropic, and Cohere.
Writes to cost_metrics, then runs cost anomaly detection and writes alerts.
"""
import os, json, datetime, logging, uuid, calendar
import requests
import psycopg2
from psycopg2.extras import execute_values
import boto3
from google.cloud import bigquery

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PG_CONN = os.environ["PG_CONN"]
GCP_PROJECT = os.environ.get("GCP_PROJECT", "seoai-479305")
BQ_BILLING_DATASET = os.environ.get("BQ_BILLING_DATASET", "seoai-479305.billing_export")
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
CH_CLOUD_API_KEY = os.environ.get("CH_CLOUD_API_KEY", "")
CH_CLOUD_API_SECRET = os.environ.get("CH_CLOUD_API_SECRET", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

TODAY = datetime.date.today()
YESTERDAY = TODAY - datetime.timedelta(days=1)


def pg_connect():
    return psycopg2.connect(PG_CONN)


# ─── Cost sources ─────────────────────────────────────────────────────────────

def collect_gcp_costs(bq_client):
    """Queries GCP BigQuery billing export for yesterday's costs per service."""
    rows = []
    query = f"""
        SELECT
            service.description AS service_name,
            SUM(cost) AS cost_usd,
            SUM(usage.amount) AS units,
            MAX(usage.unit) AS unit_type
        FROM `{BQ_BILLING_DATASET}.gcp_billing_export_v1_*`
        WHERE DATE(usage_start_time) = '{YESTERDAY}'
          AND cost > 0
        GROUP BY service.description
        ORDER BY cost_usd DESC
    """
    try:
        results = bq_client.query(query).result()
        for row in results:
            rows.append({
                "cost_source": "gcp",
                "resource_name": row.service_name,
                "cost_usd": float(row.cost_usd or 0),
                "units_consumed": float(row.units or 0),
                "unit_type": row.unit_type,
            })
        log.info("GCP costs: %d services", len(rows))
    except Exception as e:
        log.warning("GCP BigQuery billing query failed: %s", e)
    return rows


def collect_aws_costs():
    """AWS Cost Explorer: yesterday's spend by service."""
    rows = []
    if not AWS_ACCESS_KEY:
        log.info("AWS keys not configured, skipping AWS costs.")
        return rows
    try:
        ce = boto3.client(
            "ce", region_name="us-east-1",
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY,
        )
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(YESTERDAY), "End": str(TODAY)},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        for group in resp["ResultsByTime"][0]["Groups"]:
            service = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost > 0:
                rows.append({
                    "cost_source": "aws",
                    "resource_name": service,
                    "cost_usd": cost,
                    "units_consumed": None,
                    "unit_type": "USD",
                })
        log.info("AWS costs: %d services", len(rows))
    except Exception as e:
        log.warning("AWS Cost Explorer failed: %s", e)
    return rows


def collect_clickhouse_costs():
    """ClickHouse Cloud organization API for daily usage."""
    rows = []
    if not CH_CLOUD_API_KEY:
        return rows
    try:
        # Get organization ID first
        resp = requests.get(
            "https://api.clickhouse.cloud/v1/organizations",
            auth=(CH_CLOUD_API_KEY, CH_CLOUD_API_SECRET),
            timeout=15,
        )
        resp.raise_for_status()
        org_id = resp.json()["result"][0]["id"]

        usage_resp = requests.get(
            f"https://api.clickhouse.cloud/v1/organizations/{org_id}/usages",
            auth=(CH_CLOUD_API_KEY, CH_CLOUD_API_SECRET),
            params={"from": str(YESTERDAY), "to": str(TODAY)},
            timeout=15,
        )
        usage_resp.raise_for_status()
        data = usage_resp.json()
        total_cost = data.get("result", {}).get("totalCostUSD", 0)
        rows.append({
            "cost_source": "clickhouse_cloud",
            "resource_name": "*",
            "cost_usd": float(total_cost),
            "units_consumed": None,
            "unit_type": "USD",
        })
        log.info("ClickHouse Cloud cost: $%.4f", total_cost)
    except Exception as e:
        log.warning("ClickHouse Cloud API failed: %s", e)
    return rows


def collect_openai_costs():
    rows = []
    if not OPENAI_API_KEY:
        return rows
    try:
        resp = requests.get(
            "https://api.openai.com/v1/usage",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            params={"date": str(YESTERDAY)},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # Aggregate by model
        model_costs = {}
        for item in data.get("data", []):
            model = item.get("snapshot_id", "unknown")
            cost = float(item.get("cost", 0)) / 100  # OpenAI returns in cents
            model_costs[model] = model_costs.get(model, 0) + cost
        for model, cost in model_costs.items():
            if cost > 0:
                rows.append({
                    "cost_source": "openai",
                    "resource_name": model,
                    "cost_usd": cost,
                    "units_consumed": None,
                    "unit_type": "USD",
                })
        log.info("OpenAI costs: %d models", len(rows))
    except Exception as e:
        log.warning("OpenAI usage API failed: %s", e)
    return rows


def collect_anthropic_costs():
    rows = []
    if not ANTHROPIC_API_KEY:
        return rows
    try:
        resp = requests.get(
            "https://api.anthropic.com/v1/usage",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            params={"start_date": str(YESTERDAY), "end_date": str(TODAY)},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        total_cost = sum(
            float(item.get("cost_usd", 0))
            for item in data.get("usage", [])
        )
        if total_cost > 0:
            rows.append({
                "cost_source": "anthropic",
                "resource_name": "*",
                "cost_usd": total_cost,
                "units_consumed": None,
                "unit_type": "USD",
            })
        log.info("Anthropic cost: $%.4f", total_cost)
    except Exception as e:
        log.warning("Anthropic usage API failed: %s", e)
    return rows


# ─── Budget enrichment ────────────────────────────────────────────────────────

def enrich_with_budgets(pg_cur, cost_rows):
    pg_cur.execute(
        "SELECT cost_source, resource_name, monthly_budget_usd "
        "FROM control_tower.cost_budgets WHERE active = true"
    )
    budgets = {}
    for source, resource, budget in pg_cur.fetchall():
        budgets[(source, resource)] = float(budget)

    days_in_month = calendar.monthrange(YESTERDAY.year, YESTERDAY.month)[1]
    days_elapsed = YESTERDAY.day
    projected_factor = days_in_month / max(days_elapsed, 1)

    enriched = []
    for row in cost_rows:
        source = row["cost_source"]
        resource = row["resource_name"]
        budget = budgets.get((source, resource)) or budgets.get((source, "*"))
        budget_pct = None
        if budget and budget > 0:
            projected = row["cost_usd"] * projected_factor
            budget_pct = round((projected / budget) * 100, 2)
        enriched.append({**row, "monthly_budget_usd": budget, "budget_pct_consumed": budget_pct})
    return enriched


# ─── Cost anomaly detection ───────────────────────────────────────────────────

def run_cost_anomaly_checks(pg_cur):
    alerts = []
    now = datetime.datetime.now(datetime.timezone.utc)

    # Check 1: Daily spend spike (>2x 30-day average, floor $5)
    pg_cur.execute("""
        WITH yesterday AS (
            SELECT cost_source, resource_name, cost_usd
            FROM control_tower.cost_metrics
            WHERE period_start = %s
        ),
        avg30 AS (
            SELECT cost_source, resource_name,
                   AVG(cost_usd) AS avg_daily
            FROM control_tower.cost_metrics
            WHERE period_start >= %s AND period_start < %s
            GROUP BY cost_source, resource_name
        )
        SELECT y.cost_source, y.resource_name, y.cost_usd, a.avg_daily
        FROM yesterday y
        JOIN avg30 a USING (cost_source, resource_name)
        WHERE y.cost_usd > 5
          AND y.cost_usd > a.avg_daily * 2
    """, (YESTERDAY, YESTERDAY - datetime.timedelta(days=30), YESTERDAY))

    for source, resource, cost, avg in pg_cur.fetchall():
        alerts.append({
            "alert_type": "cost_daily_spike",
            "severity": "warn",
            "provider": source,
            "message": f"{source}/{resource}: ${cost:.2f} yesterday vs ${avg:.2f} 30-day avg (2x+)",
            "context_json": {"cost_usd": float(cost), "avg_30d": float(avg)},
        })

    # Check 2: Budget burn rate
    pg_cur.execute("""
        SELECT cost_source, resource_name, budget_pct_consumed, monthly_budget_usd
        FROM control_tower.cost_metrics
        WHERE period_start = %s
          AND budget_pct_consumed IS NOT NULL
          AND budget_pct_consumed >= 70
    """, (YESTERDAY,))

    for source, resource, pct, budget in pg_cur.fetchall():
        severity = "critical" if pct >= 90 else "warn"
        alerts.append({
            "alert_type": "cost_budget_burn_rate",
            "severity": severity,
            "provider": source,
            "message": f"{source}/{resource}: {pct:.1f}% of ${budget:.0f}/mo budget projected",
            "context_json": {"budget_pct": float(pct), "monthly_budget_usd": float(budget)},
        })

    if alerts:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.alerts
               (alert_id, alert_type, severity, provider, message, context_json, fired_at)
               VALUES %s""",
            [
                (
                    str(uuid.uuid4()),
                    a["alert_type"],
                    a["severity"],
                    a["provider"],
                    a["message"],
                    json.dumps(a["context_json"]),
                    now,
                )
                for a in alerts
            ],
        )
        log.info("Wrote %d cost alerts", len(alerts))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("Cost Collector starting. Collecting for %s", YESTERDAY)

    pg = pg_connect()
    bq = bigquery.Client(project=GCP_PROJECT)

    try:
        all_cost_rows = []
        all_cost_rows.extend(collect_gcp_costs(bq))
        all_cost_rows.extend(collect_aws_costs())
        all_cost_rows.extend(collect_clickhouse_costs())
        all_cost_rows.extend(collect_openai_costs())
        all_cost_rows.extend(collect_anthropic_costs())

        with pg.cursor() as cur:
            enriched = enrich_with_budgets(cur, all_cost_rows)

            if enriched:
                execute_values(
                    cur,
                    """INSERT INTO control_tower.cost_metrics (
                        cost_source, resource_name, period_start, period_end,
                        cost_usd, units_consumed, unit_type,
                        monthly_budget_usd, budget_pct_consumed
                    ) VALUES %s
                    ON CONFLICT (cost_source, resource_name, period_start) DO UPDATE SET
                        cost_usd = EXCLUDED.cost_usd,
                        budget_pct_consumed = EXCLUDED.budget_pct_consumed""",
                    [
                        (
                            r["cost_source"], r["resource_name"],
                            YESTERDAY, TODAY,
                            r["cost_usd"], r.get("units_consumed"),
                            r.get("unit_type"),
                            r.get("monthly_budget_usd"),
                            r.get("budget_pct_consumed"),
                        )
                        for r in enriched
                    ],
                )
                log.info("Wrote %d cost_metrics rows", len(enriched))

            run_cost_anomaly_checks(cur)
            pg.commit()

    except Exception as e:
        pg.rollback()
        log.error("Cost Collector failed: %s", e)
        raise
    finally:
        pg.close()

    if HEALTHCHECK_URL:
        try:
            requests.get(HEALTHCHECK_URL, timeout=10)
        except Exception:
            pass

    log.info("Cost Collector complete.")


if __name__ == "__main__":
    main()
