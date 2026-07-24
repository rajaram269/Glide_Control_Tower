"""
Sentinel Check Engine — Control Tower.

Daily tick Cloud Run job. Runs due monitor_targets (next_due_at <= now), executes
the 5 checks against ClickHouse (READ-ONLY), writes check_results + incidents, and
bridges each opened incident to control_tower.alerts so the live ct-alerter delivers
email. Never writes to ClickHouse.

Checks (DATA_MONITOR_SPEC §8 / SENTINEL_BUILD_SPEC §5-6):
 1 freshness   — system.parts primary, max(col) fallback (Lapse 4); dedup OFF
 2 volume      — sum(rows) over active parts; dedup OFF
 3 variable_coverage — active values missing from recent data
 4 schema_drift — columns vs stored structure_hash
 5 reconciliation — deduped metric compare; dedup ON
"""
import os, json, hashlib, logging, datetime
import psycopg2
from psycopg2.extras import execute_values, Json
import clickhouse_connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PG_CONN = os.environ["PG_CONN"]
CH_HOST = os.environ["CH_HOST"]
CH_USER = os.environ["CH_USER"]
CH_PASS = os.environ["CH_PASS"]
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")
# Lapse 4: let ops force the proven mechanism if system.parts proves unreliable
FRESHNESS_MECHANISM = os.environ.get("SENTINEL_FRESHNESS", "auto")  # auto|system_parts|max_col

NOW = datetime.datetime.now(datetime.timezone.utc)
ADVISORY_LOCK_KEY = 0x53454E43  # "SENC"


def pg_connect():
    return psycopg2.connect(PG_CONN)


def ch_connect():
    return clickhouse_connect.get_client(
        host=CH_HOST, user=CH_USER, password=CH_PASS, port=8443, secure=True
    )


# ─── 1. Freshness ──────────────────────────────────────────────────────────────

def _parts_freshness(ch, db, table):
    """Cheap metadata read — no data scan. Returns latest modification_time or None."""
    res = ch.query(
        "SELECT max(modification_time) FROM system.parts "
        "WHERE database = {db:String} AND table = {tbl:String} AND active",
        parameters={"db": db, "tbl": table},
    )
    r = res.result_rows
    return r[0][0] if r and r[0][0] else None


def _maxcol_freshness(ch, db, table, col):
    """Proven fallback (the legacy collector's mechanism)."""
    res = ch.query(f"SELECT toDateTime64(max(`{col}`), 3) FROM `{db}`.`{table}`")
    r = res.result_rows
    return r[0][0] if r and r[0][0] else None


def check_freshness(ch, target):
    db, table = target["database_name"], target["table_name"]
    col = target.get("freshness_column") or "_peerdb_synced_at"
    mechanism = FRESHNESS_MECHANISM
    last = None
    used = None
    if mechanism in ("auto", "system_parts"):
        try:
            last = _parts_freshness(ch, db, table)
            used = "system_parts"
        except Exception as e:
            log.warning("system.parts freshness failed %s.%s: %s", db, table, e)
    if last is None and mechanism in ("auto", "max_col"):
        try:
            last = _maxcol_freshness(ch, db, table, col)
            used = "max_col"
        except Exception as e:
            log.warning("max_col freshness failed %s.%s: %s", db, table, e)

    age_hours = None
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=datetime.timezone.utc)
        age_hours = round((NOW - last).total_seconds() / 3600, 1)

    # Static tables are intentionally frozen (fixed reference/lookup). Report freshness
    # as INFO — never stale/dead, never an incident — so they don't false-alarm.
    if target.get("is_static"):
        return "ok", {"sub_status": "static", "mechanism": used,
                      "last_write": last.isoformat() if last else None,
                      "age_hours": age_hours, "static": True}

    # Freshness tolerance = how often the DATA is expected to update
    # (expected_cadence_weeks), NOT how often the check runs (monitor_frequency_weeks).
    # Fall back to the check cadence only when discovery hasn't set an expectation yet.
    tolerance_weeks = target.get("expected_cadence_weeks") or target["monitor_frequency_weeks"]
    limit = datetime.timedelta(weeks=float(tolerance_weeks))
    if last is None:
        return "fail", {"sub_status": "dead", "mechanism": used, "last_write": None,
                        "tolerance_weeks": float(tolerance_weeks)}
    age = NOW - last
    if age <= limit:
        sub = "fresh"; status = "ok"
    elif age <= limit * 3:
        sub = "stale"; status = "warn"
    else:
        sub = "dead"; status = "fail"
    return status, {"sub_status": sub, "mechanism": used, "last_write": last.isoformat(),
                    "age_hours": age_hours, "tolerance_weeks": float(tolerance_weeks)}


# ─── 2. Volume ──────────────────────────────────────────────────────────────────

# A row-count DROP is the anomaly (PeerDB-mirrored tables are append/upsert, not
# truncate). Warn if the count fell vs the last recorded volume by more than the
# band; a modest drop can be legitimate dedup/compaction so the band avoids noise.
VOLUME_DROP_WARN_PCT = 5.0   # >5% fewer rows than last check → warn
VOLUME_DROP_FAIL_PCT = 30.0  # >30% fewer → fail (likely a real data loss)


def check_volume(ch, pg_cur, target):
    db, table = target["database_name"], target["table_name"]
    try:
        res = ch.query(
            "SELECT sum(rows) FROM system.parts "
            "WHERE database = {db:String} AND table = {tbl:String} AND active",
            parameters={"db": db, "tbl": table},
        )
        rows = int(res.result_rows[0][0] or 0)
    except Exception as e:
        return "warn", {"error": str(e)}

    # Baseline = most recent prior volume check_result for this target.
    pg_cur.execute(
        """SELECT (observed->>'row_count')::bigint
           FROM sentinel.check_results
           WHERE check_type = 'volume' AND database_name = %s AND table_name = %s
             AND COALESCE(variable,'') = %s AND observed ? 'row_count'
           ORDER BY run_ts DESC LIMIT 1""",
        (db, table, target.get("variable", "")),
    )
    r = pg_cur.fetchone()
    prev = r[0] if r and r[0] is not None else None
    if prev is None or prev == 0:
        return "ok", {"row_count": rows, "baseline": None, "note": "first observation"}

    drop_pct = (prev - rows) / prev * 100.0
    observed = {"row_count": rows, "baseline": prev, "drop_pct": round(drop_pct, 2)}
    if drop_pct >= VOLUME_DROP_FAIL_PCT:
        return "fail", observed
    if drop_pct >= VOLUME_DROP_WARN_PCT:
        return "warn", observed
    return "ok", observed


# ─── 4. Schema drift ─────────────────────────────────────────────────────────────

def check_schema_drift(ch, pg_cur, target):
    db, table = target["database_name"], target["table_name"]
    try:
        res = ch.query(
            "SELECT name, type FROM system.columns "
            "WHERE database = {db:String} AND table = {tbl:String} ORDER BY name",
            parameters={"db": db, "tbl": table},
        )
        cols = [{"name": r[0], "type": r[1]} for r in res.result_rows]
    except Exception as e:
        return "warn", {"error": str(e)}
    canon = ";".join(f"{c['name']}:{c['type']}" for c in cols)
    current_hash = hashlib.sha256(canon.encode()).hexdigest()
    pg_cur.execute(
        "SELECT structure_hash FROM sentinel.catalog_overlay "
        "WHERE database_name = %s AND table_name = %s",
        (db, table),
    )
    r = pg_cur.fetchone()
    stored = r[0] if r else None
    if stored is None:
        return "ok", {"note": "no stored hash yet"}
    if stored != current_hash:
        return "warn", {"drift": True, "stored_hash": stored[:12], "current_hash": current_hash[:12]}
    return "ok", {"drift": False}


# ─── 3. Variable coverage ───────────────────────────────────────────────────────
#
# For a per-variable target, compare the values seen recently in ClickHouse against
# the `active` value universe in variable_values (set-difference). Missing active
# values = a coverage gap (a segment stopped reporting). Signal read — dedup OFF.

def check_variable_coverage(ch, pg_cur, target):
    db, table, variable = target["database_name"], target["table_name"], target.get("variable", "")
    if not variable:
        return "ok", {"note": "table-level target, no variable"}
    pg_cur.execute(
        """SELECT value FROM sentinel.variable_values
           WHERE database_name = %s AND table_name = %s AND variable = %s AND lifecycle = 'active'""",
        (db, table, variable),
    )
    expected = {r[0] for r in pg_cur.fetchall()}
    if not expected:
        return "ok", {"note": "no active values tracked yet"}
    try:
        res = ch.query(f"SELECT DISTINCT toString(`{variable}`) FROM `{db}`.`{table}`")
        present = {r[0] for r in res.result_rows}
    except Exception as e:
        return "warn", {"error": str(e)}
    missing = sorted(expected - present)
    if missing:
        status = "fail" if len(missing) >= max(3, len(expected) // 2) else "warn"
        return status, {"missing_values": missing[:50], "missing_count": len(missing),
                        "expected_count": len(expected)}
    return "ok", {"expected_count": len(expected), "missing_count": 0}


# ─── 5. Cross-table reconciliation (dedup ON) ────────────────────────────────────
#
# Runs the `active` + `observe` rules whose source_a is this table. Metrics are
# computed WITH overlay dedup so discrepancies are real, not CDC artifacts.
# observe rules log + accrue stable_runs (auto-promote); active rules can alert.

def _overlay_dedup(pg_cur, db, table):
    pg_cur.execute(
        "SELECT requires_dedup, dedup_key, version_col, delete_col FROM sentinel.catalog_overlay "
        "WHERE database_name = %s AND table_name = %s",
        (db, table),
    )
    r = pg_cur.fetchone()
    if not r or not r[0] or not r[1]:
        return None
    return {"key": r[1], "version": r[2], "delete": r[3]}


def _parse_ref(ref):
    """A rule source ref is 'db.table', 'db.table#dimension' (segment rules), or
    'db.table::measure_col' (cross-source measure rules). Returns (db, table, measure)."""
    measure = None
    if "::" in ref:
        ref, measure = ref.split("::", 1)
    ref = ref.split("#", 1)[0]
    db, table = ref.split(".", 1)
    return db, table, measure


def _dedup_inner(db, table, dedup):
    """Deduped row subquery (or plain table) per the overlay CDC pattern."""
    if dedup and dedup["key"]:
        keys = ", ".join(f"`{k}`" for k in dedup["key"])
        if dedup["version"] and dedup["delete"]:
            # keep latest non-deleted version per key, carry all cols for measure sum
            return (f"SELECT * FROM `{db}`.`{table}` "
                    f"WHERE (`{dedup['delete']}`, `{dedup['version']}`) IN "
                    f"(SELECT argMax(`{dedup['delete']}`, `{dedup['version']}`), "
                    f"max(`{dedup['version']}`) FROM `{db}`.`{table}` GROUP BY {keys})")
        return f"`{db}`.`{table}`"
    return f"`{db}`.`{table}`"


def _deduped_count(ch, ref, dedup):
    """COUNT of a ref, applying overlay dedup when set."""
    db, table, _ = _parse_ref(ref)
    if dedup and dedup["key"]:
        keys = ", ".join(f"`{k}`" for k in dedup["key"])
        if dedup["version"] and dedup["delete"]:
            inner = (f"SELECT {keys} FROM `{db}`.`{table}` GROUP BY {keys} "
                     f"HAVING argMax(`{dedup['delete']}`, `{dedup['version']}`) = 0")
        else:
            inner = f"SELECT DISTINCT {keys} FROM `{db}`.`{table}`"
        sql = f"SELECT count() FROM ({inner})"
    else:
        sql = f"SELECT count() FROM `{db}`.`{table}`"
    return float(ch.query(sql).result_rows[0][0] or 0)


def _deduped_sum(ch, ref, dedup):
    """SUM of a measure column (from ref's ::measure), deduped. This is what makes a
    gross-vs-net gap visible — a summed measure, not a row count."""
    db, table, measure = _parse_ref(ref)
    if not measure:
        return _deduped_count(ch, ref, dedup)  # no measure → fall back to count
    q = f"toFloat64OrNull(toString(`{measure}`))"
    # dedup on business key: sum the latest non-deleted version per key
    if dedup and dedup["key"] and dedup["version"] and dedup["delete"]:
        keys = ", ".join(f"`{k}`" for k in dedup["key"])
        sql = (f"SELECT sum(m) FROM (SELECT argMax({q}, `{dedup['version']}`) AS m "
               f"FROM `{db}`.`{table}` GROUP BY {keys} "
               f"HAVING argMax(`{dedup['delete']}`, `{dedup['version']}`) = 0)")
    else:
        sql = f"SELECT sum({q}) FROM `{db}`.`{table}`"
    return float(ch.query(sql).result_rows[0][0] or 0)


def run_reconciliation(ch, pg_cur, target):
    """Execute recon rules anchored on this table. Returns list of (rule_id, status,
    observed) results; caller writes check_results + handles incidents/promotion."""
    db, table = target["database_name"], target["table_name"]
    src_prefix = f"{db}.{table}"
    pg_cur.execute(
        """SELECT rule_id, concept, metric, source_a, source_b, dimension,
                  tolerance_pct, direction, status, stable_runs
           FROM sentinel.reconciliation_rules
           WHERE status IN ('observe', 'active')
             AND (source_a = %s OR source_a LIKE %s OR source_a LIKE %s)""",
        (src_prefix, src_prefix + "#%", src_prefix + "::%"),
    )
    rules = pg_cur.fetchall()
    results = []
    for rule_id, concept, metric, src_a, src_b, dim, tol, direction, rstatus, stable in rules:
        try:
            a_db, a_tbl, _ = _parse_ref(src_a)
            b_db, b_tbl, _ = _parse_ref(src_b)
            dedup_a = _overlay_dedup(pg_cur, a_db, a_tbl)
            dedup_b = _overlay_dedup(pg_cur, b_db, b_tbl)
            # cross-source agreement compares SUMMED MEASURE (gross vs net); others count
            if metric == "cross_source_agreement":
                a = _deduped_sum(ch, src_a, dedup_a)
                b = _deduped_sum(ch, src_b, dedup_b)
            else:
                a = _deduped_count(ch, src_a, dedup_a)
                b = _deduped_count(ch, src_b, dedup_b)
        except Exception as e:
            results.append((rule_id, rstatus, "warn", {"error": str(e)}, stable))
            continue

        if direction == "subset":
            ok = a <= b  # a's values ⊆ b (count proxy)
            diff_pct = 0.0 if b == 0 else max(0.0, (a - b) / b * 100.0)
        else:  # a≈b / a≥b
            base = max(b, 1)
            diff_pct = abs(a - b) / base * 100.0
            ok = (a >= b) if direction == "a≥b" else (diff_pct <= float(tol))
        verdict = "ok" if ok else ("warn" if rstatus == "observe" else "fail")
        observed = {"a": a, "b": b, "diff_pct": round(diff_pct, 2), "tolerance_pct": float(tol),
                    "direction": direction, "rule_status": rstatus}
        results.append((rule_id, rstatus, verdict, observed, stable))
    return results


# observe→active promotion: N consecutive in-tolerance runs promotes; any breach
# resets stable_runs to 0 (and keeps the rule in observe — flagged, not paging).
RECON_PROMOTE_AFTER = 3


def apply_recon_promotion(pg_cur, rule_id, rstatus, verdict, stable):
    if rstatus != "observe":
        return
    if verdict == "ok":
        new_stable = stable + 1
        if new_stable >= RECON_PROMOTE_AFTER:
            pg_cur.execute(
                "UPDATE sentinel.reconciliation_rules SET status='active', stable_runs=%s, "
                "updated_at=now() WHERE rule_id=%s", (new_stable, rule_id))
        else:
            pg_cur.execute(
                "UPDATE sentinel.reconciliation_rules SET stable_runs=%s, updated_at=now() "
                "WHERE rule_id=%s", (new_stable, rule_id))
    else:
        pg_cur.execute(
            "UPDATE sentinel.reconciliation_rules SET stable_runs=0, updated_at=now() "
            "WHERE rule_id=%s", (rule_id,))


# ─── check_results + incidents + alert bridge ────────────────────────────────────

def write_check_result(pg_cur, target, check_type, status, observed, rule_id=None):
    pg_cur.execute(
        """INSERT INTO sentinel.check_results
           (run_ts, database_name, table_name, variable, check_type, status,
            observed, monitor_frequency_weeks, rule_id)
           VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (target["database_name"], target["table_name"], target.get("variable", ""),
         check_type, status, Json(observed), target["monitor_frequency_weeks"], rule_id),
    )
    return pg_cur.fetchone()[0]


SEVERITY = {"warn": "warn", "fail": "critical"}


def open_or_update_incident(pg_cur, target, check_type, status, observed, check_result_id):
    """Open an incident if none open for this scope; bridge to control_tower.alerts
    on first open. Dedup: one open incident per (db, table, variable, check_type)."""
    db, table, variable = target["database_name"], target["table_name"], target.get("variable", "")
    scope = {"database_name": db, "table": table, "variable": variable, "check_type": check_type}
    pg_cur.execute(
        """SELECT id FROM sentinel.incidents
           WHERE resolved_at IS NULL AND check_type = %s
             AND scope->>'database_name' = %s AND scope->>'table' = %s
             AND COALESCE(scope->>'variable','') = %s""",
        (check_type, db, table, variable),
    )
    existing = pg_cur.fetchone()
    if existing:
        return existing[0]  # already open — dedup, no re-fire

    severity = SEVERITY.get(status, "warn")
    message = f"{check_type} {status} on {db}.{table}" + (f" [{variable}]" if variable else "")

    # Bridge: insert control_tower.alerts row → live ct-alerter emails it
    pg_cur.execute(
        """INSERT INTO control_tower.alerts
           (alert_type, severity, service_name, message, context_json, fired_at)
           VALUES (%s, %s, %s, %s, %s, now())
           RETURNING alert_id""",
        (f"sentinel_{check_type}", severity, f"{db}.{table}", message,
         Json({"scope": scope, "observed": observed})),
    )
    alert_id = pg_cur.fetchone()[0]

    pg_cur.execute(
        """INSERT INTO sentinel.incidents
           (opened_at, scope, scope_level, check_type, severity, message,
            check_result_id, alert_id)
           VALUES (now(), %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (Json(scope), "variable" if variable else "table", check_type, severity,
         message, check_result_id, alert_id),
    )
    return pg_cur.fetchone()[0]


def resolve_incident_if_open(pg_cur, target, check_type):
    db, table, variable = target["database_name"], target["table_name"], target.get("variable", "")
    pg_cur.execute(
        """UPDATE sentinel.incidents SET resolved_at = now()
           WHERE resolved_at IS NULL AND check_type = %s
             AND scope->>'database_name' = %s AND scope->>'table' = %s
             AND COALESCE(scope->>'variable','') = %s""",
        (check_type, db, table, variable),
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def due_targets(pg_cur):
    pg_cur.execute("""
        SELECT t.id, t.database_name, t.table_name, t.variable,
               t.monitor_frequency_weeks, t.expected_cadence_weeks, t.cheap_source,
               t.is_static, w.freshness_column
        FROM sentinel.monitor_targets t
        LEFT JOIN control_tower.watched_tables w
               ON w.database_name = t.database_name AND w.table_name = t.table_name
        WHERE t.status = 'active' AND (t.next_due_at IS NULL OR t.next_due_at <= now())
        ORDER BY t.next_due_at NULLS FIRST
    """)
    return [
        {"id": r[0], "database_name": r[1], "table_name": r[2], "variable": r[3],
         "monitor_frequency_weeks": r[4], "expected_cadence_weeks": r[5],
         "cheap_source": r[6], "is_static": r[7], "freshness_column": r[8]}
        for r in pg_cur.fetchall()
    ]


# Table-level checks (run once per target). Reconciliation is per-rule (handled
# separately). Coverage only fires for variable targets. Each entry: (check_type, fn)
# where fn(ch, pg_cur, target) -> (status, observed).
SINGLE_CHECKS = [
    ("freshness", lambda ch, cur, t: check_freshness(ch, t)),
    ("volume", check_volume),
    ("schema_drift", check_schema_drift),
    ("variable_coverage", check_variable_coverage),
]


def run_single_checks(cur, ch, target):
    for check_type, fn in SINGLE_CHECKS:
        t0 = datetime.datetime.now(datetime.timezone.utc)
        try:
            status, observed = fn(ch, cur, target)
        except Exception as e:
            status, observed = "warn", {"error": str(e)}
        observed["duration_ms"] = int(
            (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds() * 1000)
        crid = write_check_result(cur, target, check_type, status, observed)
        if status in ("warn", "fail"):
            open_or_update_incident(cur, target, check_type, status, observed, crid)
        else:
            resolve_incident_if_open(cur, target, check_type)


def run_recon_checks(cur, ch, target):
    """Reconciliation is per-rule: run, write a result per rule, drive observe→active
    promotion, and only page for ACTIVE rules that fail (observe rules log + flag)."""
    for rule_id, rstatus, verdict, observed, stable in run_reconciliation(ch, cur, target):
        crid = write_check_result(cur, target, "reconciliation", verdict, observed, rule_id=rule_id)
        apply_recon_promotion(cur, rule_id, rstatus, verdict, stable)
        if verdict == "fail" and rstatus == "active":
            open_or_update_incident(cur, {**target, "variable": f"rule:{rule_id}"},
                                    "reconciliation", verdict, observed, crid)
        elif verdict == "ok":
            resolve_incident_if_open(cur, {**target, "variable": f"rule:{rule_id}"}, "reconciliation")


def roll_up_incidents(cur):
    """Suppress floods: when >=3 open, unparented table-level incidents share a
    database_name, group them under a synthetic per-database parent so the alert
    bridge/consumer sees one cluster instead of N. Parent carries rolled_up_children."""
    cur.execute("""
        SELECT scope->>'database_name' AS db, array_agg(id) AS ids
        FROM sentinel.incidents
        WHERE resolved_at IS NULL AND parent_incident_id IS NULL
          AND scope_level = 'table'
        GROUP BY scope->>'database_name'
        HAVING count(*) >= 3
    """)
    for db, ids in cur.fetchall():
        cur.execute(
            """INSERT INTO sentinel.incidents
               (opened_at, scope, scope_level, check_type, severity, message, rolled_up_children)
               VALUES (now(), %s, 'schema', 'rollup', 'warn', %s, %s)
               RETURNING id""",
            (Json({"database_name": db}), f"{len(ids)} open incidents in {db}", len(ids)),
        )
        parent_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE sentinel.incidents SET parent_incident_id = %s WHERE id = ANY(%s)",
            (parent_id, ids),
        )


def main():
    log.info("Sentinel check engine starting @ %s", NOW)
    pg = pg_connect()
    ch = ch_connect()
    ran = 0

    try:
        with pg.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            if not cur.fetchone()[0]:
                log.warning("Another check run holds the lock. Exiting.")
                return

            targets = due_targets(cur)
            log.info("%d due targets", len(targets))

            # Reconciliation rules anchor on a table, not a variable — run them once
            # per table even when several of its variable targets are due this pass.
            recon_done = set()
            for target in targets:
                run_single_checks(cur, ch, target)
                tbl_key = (target["database_name"], target["table_name"])
                if tbl_key not in recon_done:
                    run_recon_checks(cur, ch, target)
                    recon_done.add(tbl_key)
                # advance schedule
                cur.execute(
                    """UPDATE sentinel.monitor_targets
                       SET last_checked_at = now(),
                           next_due_at = now() + (monitor_frequency_weeks || ' weeks')::interval
                       WHERE id = %s""",
                    (target["id"],),
                )
                ran += 1

            roll_up_incidents(cur)

            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            pg.commit()
            log.info("Check engine committed. Ran %d targets.", ran)

    except Exception as e:
        pg.rollback()
        log.error("Check engine failed: %s", e)
        raise
    finally:
        pg.close()
        ch.close()

    if HEALTHCHECK_URL:
        try:
            import requests
            requests.get(HEALTHCHECK_URL, timeout=10)
        except Exception as e:
            log.warning("Healthcheck ping failed: %s", e)
    log.info("Sentinel check engine complete.")


if __name__ == "__main__":
    main()
