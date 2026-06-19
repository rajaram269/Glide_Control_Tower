"""
AWS Collector — Control Tower
Hourly Cloud Run job. Pulls CloudWatch metrics for EC2 instances registered
in control_tower.registered_services. Also discovers new EC2 instances.
"""
import os, json, datetime, logging
import boto3
import psycopg2
from psycopg2.extras import execute_values
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PG_CONN = os.environ["PG_CONN"]
AWS_ACCESS_KEY = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

NOW = datetime.datetime.now(datetime.timezone.utc)
WINDOW_START = NOW - datetime.timedelta(minutes=65)


def pg_connect():
    return psycopg2.connect(PG_CONN)


def boto_client(service):
    return boto3.client(
        service,
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def get_metric_stat(cw, instance_id, metric_name, namespace, stat="Average"):
    try:
        resp = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=WINDOW_START,
            EndTime=NOW,
            Period=3600,
            Statistics=[stat],
        )
        points = resp.get("Datapoints", [])
        if not points:
            return None
        return round(points[0][stat], 3)
    except Exception as e:
        log.warning("CloudWatch metric %s/%s for %s failed: %s",
                    namespace, metric_name, instance_id, e)
        return None


def collect_ec2_health(pg_cur):
    ec2 = boto_client("ec2")
    cw = boto_client("cloudwatch")

    pg_cur.execute(
        "SELECT service_name, ec2_instance_id FROM control_tower.registered_services "
        "WHERE active = true AND cloud = 'aws' AND platform IN ('ec2', 'ec2_job')"
    )
    # For services without stored instance_id, try to match by service_name tag
    services = pg_cur.fetchall()

    rows = []
    for service_name, stored_instance_id in services:
        # Resolve instance ID from EC2 tags if not stored
        instance_id = stored_instance_id
        if not instance_id:
            try:
                resp = ec2.describe_instances(Filters=[
                    {"Name": "tag:Name", "Values": [service_name]},
                    {"Name": "instance-state-name", "Values": ["running", "stopped"]},
                ])
                reservations = resp.get("Reservations", [])
                if reservations:
                    instance_id = reservations[0]["Instances"][0]["InstanceId"]
            except Exception as e:
                log.warning("Could not resolve EC2 instance for %s: %s", service_name, e)
                continue

        if not instance_id:
            log.warning("No instance ID for %s, skipping", service_name)
            continue

        # Instance state
        instance_state = None
        try:
            resp = ec2.describe_instance_status(InstanceIds=[instance_id], IncludeAllInstances=True)
            statuses = resp.get("InstanceStatuses", [])
            if statuses:
                instance_state = statuses[0]["InstanceState"]["Name"]
        except Exception as e:
            log.warning("EC2 instance status for %s failed: %s", instance_id, e)

        cpu = get_metric_stat(cw, instance_id, "CPUUtilization", "AWS/EC2")
        mem = get_metric_stat(cw, instance_id, "mem_used_percent", "CWAgent")
        disk = get_metric_stat(cw, instance_id, "disk_used_percent", "CWAgent")

        rows.append((
            service_name, "ec2", AWS_REGION, NOW,
            None, None, None, None, None, None, None,  # request/error/latency metrics
            None, None, None,                           # job fields
            cpu, mem, disk,
            instance_id, instance_state,
        ))
        log.info("EC2 %s (%s): cpu=%.1f%% mem=%s disk=%s state=%s",
                 service_name, instance_id,
                 cpu or 0, mem, disk, instance_state)

    if rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.service_health (
                service_name, platform, region, collected_at,
                request_count, error_count, error_rate_pct,
                p50_latency_ms, p95_latency_ms, p99_latency_ms,
                instance_count, job_exit_code, job_duration_ms, job_status,
                cpu_utilization_pct, memory_utilization_pct, disk_utilization_pct,
                ec2_instance_id, ec2_instance_state
            ) VALUES %s
            ON CONFLICT (service_name, collected_at) DO UPDATE SET
                cpu_utilization_pct = EXCLUDED.cpu_utilization_pct,
                memory_utilization_pct = EXCLUDED.memory_utilization_pct,
                disk_utilization_pct = EXCLUDED.disk_utilization_pct,
                ec2_instance_state = EXCLUDED.ec2_instance_state""",
            rows,
        )
        log.info("Upserted %d EC2 service_health rows", len(rows))


def auto_discover_ec2(pg_cur):
    """Discover running EC2 instances not yet in registered_services."""
    ec2 = boto_client("ec2")
    pg_cur.execute("SELECT service_name FROM control_tower.registered_services")
    known = {r[0] for r in pg_cur.fetchall()}

    new_rows = []
    try:
        resp = ec2.describe_instances(Filters=[
            {"Name": "instance-state-name", "Values": ["running"]}
        ])
        for reservation in resp.get("Reservations", []):
            for inst in reservation["Instances"]:
                instance_id = inst["InstanceId"]
                # Use Name tag as service name, fall back to instance ID
                name = instance_id
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        name = tag["Value"]
                        break
                if name not in known:
                    new_rows.append((name, "ec2", AWS_REGION, "aws", False))
                    log.info("Discovered EC2: %s (%s)", name, instance_id)
    except Exception as e:
        log.warning("EC2 auto-discovery failed: %s", e)

    if new_rows:
        execute_values(
            pg_cur,
            """INSERT INTO control_tower.registered_services
               (service_name, platform, region, cloud, active)
               VALUES %s ON CONFLICT (service_name) DO NOTHING""",
            new_rows,
        )


def main():
    log.info("AWS Collector starting.")
    pg = pg_connect()
    try:
        with pg.cursor() as cur:
            collect_ec2_health(cur)
            auto_discover_ec2(cur)
            pg.commit()
        log.info("AWS Collector complete.")
    except Exception as e:
        pg.rollback()
        log.error("AWS Collector failed: %s", e)
        raise
    finally:
        pg.close()

    if HEALTHCHECK_URL:
        try:
            requests.get(HEALTHCHECK_URL, timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    main()
