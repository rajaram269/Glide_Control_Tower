"""
Alerter — Control Tower
Hourly Cloud Run job (at :45). Reads unacknowledged alerts from PostgreSQL,
checks provider_status for context, calls Anthropic AI triage for critical alerts,
sends email via MS Graph, marks alerts acknowledged.

Weekly cost digest runs on Monday 09:00 IST via a separate Cloud Scheduler trigger
pointing to this same job with WEEKLY_DIGEST=true env override.
"""
import os, json, datetime, logging
import psycopg2
import anthropic
import msal
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PG_CONN = os.environ["PG_CONN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ALERT_EMAIL = os.environ["ALERT_EMAIL"]
# ai@holistique.in is the only mailbox the tenant's AppOnly AccessPolicy allows
# this app to send as (hr@ and others return ErrorAccessDenied / RAOP)
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "ai@holistique.in")
WEEKLY_DIGEST = os.environ.get("WEEKLY_DIGEST", "false").lower() == "true"

# Email transport: "graph" (MS Graph, default) or "ses" (AWS SES).
EMAIL_TRANSPORT = os.environ.get("EMAIL_TRANSPORT", "graph").lower()
# MS Graph creds — required only for the graph transport
EMAIL_TENANT_ID = os.environ.get("EMAIL_TENANT_ID", "")
EMAIL_CLIENT_ID = os.environ.get("EMAIL_CLIENT_ID", "")
EMAIL_CLIENT_SECRET = os.environ.get("EMAIL_CLIENT_SECRET", "")
# AWS SES creds — required only for the ses transport
SES_ACCESS_KEY_ID = os.environ.get("SES_ACCESS_KEY_ID", "")
SES_SECRET_ACCESS_KEY = os.environ.get("SES_SECRET_ACCESS_KEY", "")
SES_REGION = os.environ.get("SES_REGION", "ap-south-1")

NOW = datetime.datetime.now(datetime.timezone.utc)

SEVERITY_EMOJI = {"info": "ℹ️", "warn": "⚠️", "critical": "🔴"}


def pg_connect():
    return psycopg2.connect(PG_CONN)


# ─── MS Graph email ───────────────────────────────────────────────────────────

def get_ms_graph_token():
    app = msal.ConfidentialClientApplication(
        client_id=EMAIL_CLIENT_ID,
        client_credential=EMAIL_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{EMAIL_TENANT_ID}",
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"MS Graph auth failed: {result.get('error_description')}")
    return result["access_token"]


def _send_via_graph(token, subject, html_body, to):
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": False,
    }
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def _send_via_ses(subject, html_body, to):
    import boto3
    client = boto3.client(
        "ses", region_name=SES_REGION,
        aws_access_key_id=SES_ACCESS_KEY_ID,
        aws_secret_access_key=SES_SECRET_ACCESS_KEY,
    )
    client.send_email(
        Source=SENDER_EMAIL,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Html": {"Data": html_body}},
        },
    )


def send_email(token, subject, html_body, to_email=None):
    """Dispatch by EMAIL_TRANSPORT. `token` is the MS Graph token (None for SES)."""
    to = to_email or ALERT_EMAIL
    if EMAIL_TRANSPORT == "ses":
        _send_via_ses(subject, html_body, to)
    else:
        _send_via_graph(token, subject, html_body, to)


# ─── AI triage ────────────────────────────────────────────────────────────────

def get_ai_triage(alert, provider_status, recent_history):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""You are a DevOps on-call assistant. Analyze this alert and provide a concise triage.

Alert: {alert['message']}
Type: {alert['alert_type']}
Severity: {alert['severity']}
Context: {json.dumps(alert.get('context_json') or {})}

Provider status: {provider_status or 'No relevant provider outage detected.'}

Recent history (past 24h same service):
{recent_history or 'No prior alerts.'}

Respond in exactly 2-3 sentences: (1) most likely root cause, (2) single most important remediation step."""

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as e:
        log.warning("AI triage failed: %s", e)
        return None


# ─── Alert email builder ──────────────────────────────────────────────────────

def build_alert_email(alert, provider_context, triage):
    emoji = SEVERITY_EMOJI.get(alert["severity"], "⚠️")
    ctx = alert.get("context_json") or {}
    ctx_html = "".join(
        f"<tr><td style='padding:4px 8px;color:#666'>{k}</td>"
        f"<td style='padding:4px 8px'><b>{v}</b></td></tr>"
        for k, v in ctx.items()
    )
    provider_html = ""
    if provider_context:
        provider_html = f"""
        <div style='background:#fff3cd;border-left:4px solid #ffc107;padding:10px;margin:12px 0'>
            <b>Provider Status:</b> {provider_context}
        </div>"""

    triage_html = ""
    if triage:
        triage_html = f"""
        <div style='background:#e8f4fd;border-left:4px solid #0d6efd;padding:10px;margin:12px 0'>
            <b>AI Triage:</b> {triage}
        </div>"""

    return f"""
    <div style='font-family:sans-serif;max-width:600px'>
        <h2 style='color:{"#dc3545" if alert["severity"]=="critical" else "#ffc107"}'>{emoji} {alert["alert_type"].replace("_"," ").title()}</h2>
        <p style='font-size:16px'>{alert["message"]}</p>
        <table style='border-collapse:collapse;width:100%'>{ctx_html}</table>
        {provider_html}
        {triage_html}
        <hr style='margin-top:20px'>
        <small style='color:#999'>Control Tower · {NOW.strftime("%Y-%m-%d %H:%M UTC")} · Alert ID: {alert["alert_id"]}</small>
    </div>"""


# ─── Weekly digest ────────────────────────────────────────────────────────────

def send_weekly_digest(pg_cur, token):
    log.info("Building weekly cost digest...")
    pg_cur.execute("""
        WITH this_week AS (
            SELECT cost_source, SUM(cost_usd) AS total
            FROM control_tower.cost_metrics
            WHERE period_start >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY cost_source
        ),
        last_week AS (
            SELECT cost_source, SUM(cost_usd) AS total
            FROM control_tower.cost_metrics
            WHERE period_start >= CURRENT_DATE - INTERVAL '14 days'
              AND period_start < CURRENT_DATE - INTERVAL '7 days'
            GROUP BY cost_source
        ),
        budgets AS (
            SELECT cost_source, SUM(monthly_budget_usd) AS monthly_budget
            FROM control_tower.cost_budgets
            WHERE resource_name = '*' AND active = true
            GROUP BY cost_source
        )
        SELECT t.cost_source, t.total AS this_week,
               COALESCE(l.total, 0) AS last_week,
               b.monthly_budget
        FROM this_week t
        LEFT JOIN last_week l USING (cost_source)
        LEFT JOIN budgets b USING (cost_source)
        ORDER BY t.total DESC
    """)
    rows = pg_cur.fetchall()
    if not rows:
        log.info("No cost data for weekly digest.")
        return

    rows_html = ""
    for source, this_w, last_w, budget in rows:
        pct_change = ((this_w - last_w) / last_w * 100) if last_w else 0
        trend = f"▲ {pct_change:.0f}%" if pct_change > 5 else (
            f"▼ {abs(pct_change):.0f}%" if pct_change < -5 else "→ stable"
        )
        color = "#dc3545" if pct_change > 20 else ("#ffc107" if pct_change > 5 else "#198754")
        budget_note = f"/ ${budget:.0f}/mo" if budget else ""
        rows_html += f"""
        <tr>
            <td style='padding:8px 12px'>{source}</td>
            <td style='padding:8px 12px;text-align:right'>${this_w:.2f} {budget_note}</td>
            <td style='padding:8px 12px;text-align:right'>${last_w:.2f}</td>
            <td style='padding:8px 12px;color:{color}'>{trend}</td>
        </tr>"""

    total_this = sum(r[1] for r in rows)
    total_last = sum(r[2] for r in rows)

    html = f"""
    <div style='font-family:sans-serif;max-width:640px'>
        <h2>📊 Weekly Cost Digest — {NOW.strftime("%b %d, %Y")}</h2>
        <table style='border-collapse:collapse;width:100%;border:1px solid #dee2e6'>
            <thead style='background:#f8f9fa'>
                <tr>
                    <th style='padding:10px 12px;text-align:left'>Source</th>
                    <th style='padding:10px 12px;text-align:right'>This week</th>
                    <th style='padding:10px 12px;text-align:right'>Last week</th>
                    <th style='padding:10px 12px;text-align:left'>Trend</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
            <tfoot style='background:#f8f9fa;font-weight:bold'>
                <tr>
                    <td style='padding:10px 12px'>Total</td>
                    <td style='padding:10px 12px;text-align:right'>${total_this:.2f}</td>
                    <td style='padding:10px 12px;text-align:right'>${total_last:.2f}</td>
                    <td style='padding:10px 12px'></td>
                </tr>
            </tfoot>
        </table>
        <small style='color:#999'>Control Tower · {NOW.strftime("%Y-%m-%d %H:%M UTC")}</small>
    </div>"""

    send_email(token, f"📊 Weekly Cost Digest — {NOW.strftime('%b %d')}", html)
    log.info("Weekly digest sent.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("Alerter starting. weekly_digest=%s", WEEKLY_DIGEST)
    pg = pg_connect()

    token = None
    if EMAIL_TRANSPORT != "ses":
        try:
            token = get_ms_graph_token()
        except Exception as e:
            log.error("Cannot get MS Graph token, aborting: %s", e)
            pg.close()
            return

    try:
        with pg.cursor() as cur:
            if WEEKLY_DIGEST:
                send_weekly_digest(cur, token)
                pg.commit()
                return

            # Read unacknowledged alerts from last 2 hours
            cur.execute("""
                SELECT alert_id, alert_type, severity, service_name, provider,
                       message, context_json, fired_at
                FROM control_tower.alerts
                WHERE acknowledged_at IS NULL
                  AND fired_at > NOW() - INTERVAL '2 hours'
                ORDER BY
                    CASE severity WHEN 'critical' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END,
                    fired_at DESC
            """)
            alerts = [
                {
                    "alert_id": str(r[0]), "alert_type": r[1], "severity": r[2],
                    "service_name": r[3], "provider": r[4], "message": r[5],
                    "context_json": r[6], "fired_at": r[7],
                }
                for r in cur.fetchall()
            ]

            if not alerts:
                log.info("No unacknowledged alerts.")
                pg.commit()
                return

            log.info("Processing %d alerts", len(alerts))

            # Load current provider statuses
            cur.execute("""
                SELECT DISTINCT ON (provider) provider, overall_status, affected_components
                FROM control_tower.provider_status
                ORDER BY provider, polled_at DESC
            """)
            provider_statuses = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            acknowledged_ids = []
            for alert in alerts:
                provider = alert.get("provider") or (
                    alert["service_name"].split("-")[0] if alert.get("service_name") else None
                )
                provider_ctx = None
                if provider and provider in provider_statuses:
                    status, components = provider_statuses[provider]
                    if status != "operational":
                        provider_ctx = f"{provider} is {status}. Affected: {components}"

                # AI triage for critical only
                triage = None
                if alert["severity"] == "critical":
                    triage = get_ai_triage(alert, provider_ctx, None)

                html = build_alert_email(alert, provider_ctx, triage)
                subject = (
                    f"[{'CRITICAL' if alert['severity']=='critical' else 'WARN'}] "
                    f"{alert['alert_type'].replace('_', ' ').title()} — "
                    f"{alert.get('service_name') or alert.get('provider', 'Control Tower')}"
                )

                try:
                    send_email(token, subject, html)
                    acknowledged_ids.append(alert["alert_id"])
                    log.info("Sent: %s", subject)
                except Exception as e:
                    log.error("Failed to send alert %s: %s", alert["alert_id"], e)

            if acknowledged_ids:
                cur.execute(
                    """UPDATE control_tower.alerts
                       SET acknowledged_at = NOW(), ack_by = 'alerter'
                       WHERE alert_id = ANY(%s::uuid[])""",
                    (acknowledged_ids,),
                )
                log.info("Acknowledged %d alerts", len(acknowledged_ids))

            pg.commit()

    except Exception as e:
        pg.rollback()
        log.error("Alerter failed: %s", e)
        raise
    finally:
        pg.close()

    log.info("Alerter complete.")


if __name__ == "__main__":
    main()
