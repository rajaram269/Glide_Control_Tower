"""
Sentinel Discovery Loop — Control Tower.

Daily Cloud Run job. Introspects ClickHouse (READ-ONLY) and writes the catalog
overlay + monitor targets + reconciliation rules to Postgres schema `sentinel`.
Never alerts. Never writes to ClickHouse.

Steps (DATA_MONITOR_SPEC §10 / SENTINEL_BUILD_SPEC §3):
 1 advisory lock  2 enumerate  3 introspect+hash  4 incremental gate
 5 PII-safe sample  6 LLM pass (llm.py)  7 authority+conflict (I1-I4)
 8 drift/dropped  9 monitor_targets  10 reconciliation_rules  11 write  12 lock release
"""
import os, json, hashlib, logging, datetime
import psycopg2
from psycopg2.extras import execute_values, Json
import clickhouse_connect

import llm

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PG_CONN = os.environ["PG_CONN"]
CH_HOST = os.environ["CH_HOST"]
CH_USER = os.environ["CH_USER"]
CH_PASS = os.environ["CH_PASS"]
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

NOW = datetime.datetime.now(datetime.timezone.utc)
ADVISORY_LOCK_KEY = 0x53454E54  # "SENT"

# Progressive scanning: cap new/changed tables processed per run so a full pass over
# ~450 tables never risks the Cloud Run task timeout. Each run commits per-table and
# the incremental gate skips already-done tables, so consecutive runs (scheduled or
# stacked manual) fill the catalog over a few runs; steady-state only touches changes.
MAX_TABLES_PER_RUN = int(os.environ.get("SENTINEL_MAX_TABLES_PER_RUN", "80"))

# PII-safe sampling caps (§10.3)
MAX_DISTINCT = 50
MAX_STR_LEN = 64
# Never sample raw row values from these databases — schema + stats only (R5)
PII_DATABASES = {"recruitment_hr"}

# Deterministic source_type heuristics run BEFORE the LLM (§10.4). Returns a code
# or None (LLM decides). Order matters — first match wins.
def source_type_heuristic(db, table):
    t = table.lower()
    if table.startswith("BC_") or "salesinvoice" in t or db.lower() == "finance_data":
        return "erp"
    if "meta_insight" in t or "google_ads" in t or "performance_marketing" in t:
        return "perf_marketing"
    if "ga4" in t or "shopflo" in t or "web_analytics" in t or "scraper" in t:
        return "web_analytics"
    if t.endswith("_reference") or "pincode" in t or t.endswith("_lookup"):
        return "reference"
    # NOTE: no name-based "derived" rule. A _copy / _f_copy suffix says nothing about
    # a table's PURPOSE — a copy can be an enriched dimensional table, a stale mirror,
    # or a genuinely different-purpose extract. `derived` must be inferred from CONTENT
    # (columns/engine/samples) by the LLM, never from the table name.
    if "order" in t or "inventory" in t or "product" in t:
        return "sales_channel"
    return None


def pg_connect():
    return psycopg2.connect(PG_CONN)


# Hard per-query ceiling so one slow introspection/stat query can't wedge the run
# (see check_engine for the failure mode). Discovery already commits per table, so a
# raised query just skips that table; the next run retries it.
CH_MAX_EXECUTION_SECONDS = int(os.environ.get("SENTINEL_CH_QUERY_TIMEOUT", "45"))


def ch_connect():
    return clickhouse_connect.get_client(
        host=CH_HOST, user=CH_USER, password=CH_PASS, port=8443, secure=True,
        settings={"max_execution_time": CH_MAX_EXECUTION_SECONDS},
        send_receive_timeout=CH_MAX_EXECUTION_SECONDS + 15,
    )


# ─── 2. Enumerate ─────────────────────────────────────────────────────────────
# Same universe the legacy collector uses (gcp_collector auto_discover_watched_tables):
# skip raw-mirror staging, backups, and system databases.

def enumerate_tables(ch):
    result = ch.query("""
        SELECT database, name, engine, sorting_key, total_rows
        FROM system.tables
        WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema', 'default')
          AND name NOT LIKE '\\_peerdb\\_raw\\_mirror%'
          AND name NOT LIKE '%\\_backup\\_2%'
          AND engine NOT LIKE '%View%'
        ORDER BY database, name
    """)
    return [
        {"database_name": r[0], "table_name": r[1], "engine": r[2],
         "sorting_key": r[3], "total_rows": r[4]}
        for r in result.result_rows
    ]


# ─── 3. Introspect + structure hash ───────────────────────────────────────────

def introspect_columns(ch, db, table):
    result = ch.query(
        "SELECT name, type FROM system.columns "
        "WHERE database = {db:String} AND table = {tbl:String} ORDER BY position",
        parameters={"db": db, "tbl": table},
    )
    return [{"name": r[0], "type": r[1]} for r in result.result_rows]


def structure_hash(columns):
    canon = ";".join(f"{c['name']}:{c['type']}" for c in sorted(columns, key=lambda c: c["name"]))
    return hashlib.sha256(canon.encode()).hexdigest()


# ─── 5. PII-safe sampling ──────────────────────────────────────────────────────

_LOW_CARD_TYPES = ("LowCardinality", "Enum", "Bool", "UInt8")


def _is_sampleable(col):
    """Only low-cardinality-ish, non-sensitive-looking string/enum columns."""
    t = col["type"]
    if any(x in t for x in _LOW_CARD_TYPES):
        return True
    return False


def pii_safe_samples(ch, db, table, columns):
    """Return {column: [distinct values]} for low-cardinality columns only.
    Never touches PII databases (schema+stats only there)."""
    if db in PII_DATABASES:
        return {}
    samples = {}
    for col in columns:
        if not _is_sampleable(col):
            continue
        try:
            # Sample via toString() so stored values match the coverage check, which
            # also reads toString(col) — otherwise CH's `true`/`false` (bool) or `0`/`1`
            # (UInt8) never equal Python's str() `True`/`False`, and every boolean/enum
            # column false-flags as "all values missing" (caught in real E2E).
            res = ch.query(
                f"SELECT DISTINCT toString(`{col['name']}`) AS v FROM `{db}`.`{table}` "
                f"WHERE length(toString(`{col['name']}`)) <= {MAX_STR_LEN} "
                f"LIMIT {MAX_DISTINCT + 1}"
            )
            vals = [r[0] for r in res.result_rows]
            if len(vals) <= MAX_DISTINCT:  # genuinely low-cardinality
                samples[col["name"]] = vals
        except Exception as e:
            log.warning("sample failed %s.%s.%s: %s", db, table, col["name"], e)
    return samples


# Numeric/measure columns whose AGGREGATE stats (not row values) reveal semantics:
# magnitude distinguishes gross vs net (1T vs 2.6B), and a negative count reveals
# whether returns/refunds are represented (→ net is derivable). Stats are aggregates
# over the whole column — PII-safe, no individual row values leave ClickHouse.
_NUMERIC_TYPE_HINTS = ("Int", "UInt", "Float", "Decimal")
# money/measure-looking names worth profiling (keeps the stat query cheap + targeted)
_MEASURE_NAME_HINTS = ("sales", "mrp", "amount", "revenue", "value", "price",
                       "qty", "quantity", "net", "gross", "discount", "disc",
                       "tax", "margin", "bfd", "total", "cost",
                       # discount/markdown breakup columns — presence of these next to a
                       # gross measure is what makes net computable; profile them so the
                       # LLM sees they exist and hold real numbers.
                       "markdown", "rebate", "promo", "coupon", "gmv")


def _looks_numeric_measure(col):
    t = col["type"]
    n = col["name"].lower()
    type_numeric = any(h in t for h in _NUMERIC_TYPE_HINTS)
    name_measure = any(h in n for h in _MEASURE_NAME_HINTS)
    # profile if it's numerically typed, OR a measure-named string (values stored as
    # strings — common in these tables, e.g. Net_Sales String) that parses to a number
    return type_numeric or name_measure


def measure_stats(ch, db, table, columns):
    """{column: {sum, avg, min, max, negatives, nonnull}} for measure-like columns.
    Aggregates only — reveals gross-vs-net magnitude and presence of returns
    (negatives) without sending any row-level value. Skips PII databases."""
    if db in PII_DATABASES:
        return {}
    stats = {}
    for col in columns:
        name = col["name"]
        if name.startswith("_peerdb") or not _looks_numeric_measure(col):
            continue
        q = f"toFloat64OrNull(toString(`{name}`))"
        try:
            res = ch.query(
                f"SELECT sum({q}), avg({q}), min({q}), max({q}), "
                f"countIf({q} < 0), count({q}) FROM `{db}`.`{table}`"
            )
            s, a, mn, mx, neg, nn = res.result_rows[0]
            if nn and nn > 0:  # column actually holds numbers
                stats[name] = {
                    "sum": round(float(s), 2) if s is not None else None,
                    "avg": round(float(a), 4) if a is not None else None,
                    "min": round(float(mn), 2) if mn is not None else None,
                    "max": round(float(mx), 2) if mx is not None else None,
                    "negatives": int(neg or 0),
                    "nonnull": int(nn),
                }
        except Exception as e:
            log.warning("measure stats failed %s.%s.%s: %s", db, table, name, e)
    return stats


# ─── Overlay read/write ────────────────────────────────────────────────────────

def load_existing_overlay(pg_cur):
    pg_cur.execute(
        "SELECT database_name, table_name, structure_hash, updated_by, authoritative, "
        "       concept, scope, source_type "
        "FROM sentinel.catalog_overlay"
    )
    return {
        (r[0], r[1]): {"structure_hash": r[2], "updated_by": r[3], "authoritative": r[4],
                       "concept": r[5], "scope": r[6], "source_type": r[7]}
        for r in pg_cur.fetchall()
    }


def ensure_source_type(pg_cur, code):
    if not code:
        return
    pg_cur.execute(
        "INSERT INTO sentinel.source_type_vocab (code, label) VALUES (%s, %s) "
        "ON CONFLICT (code) DO NOTHING",
        (code, code.replace("_", " ").title()),
    )


def upsert_overlay(pg_cur, row):
    ensure_source_type(pg_cur, row.get("source_type"))
    pg_cur.execute(
        """INSERT INTO sentinel.catalog_overlay
           (database_name, table_name, description, summary, grain, concept,
            source_type, scope, authoritative, authority_confidence, use_instead,
            requires_dedup, dedup_method, dedup_key, version_col, delete_col, quirks,
            variables, relationships, conflict_type, review_status, review_reason, is_static,
            event_date_column, structure_hash, updated_by, updated_at, retired)
           VALUES (%(database_name)s, %(table_name)s, %(description)s, %(summary)s,
                   %(grain)s, %(concept)s, %(source_type)s, %(scope)s, %(authoritative)s,
                   %(authority_confidence)s, %(use_instead)s, %(requires_dedup)s,
                   %(dedup_method)s, %(dedup_key)s, %(version_col)s, %(delete_col)s,
                   %(quirks)s, %(variables)s, %(relationships)s, %(conflict_type)s,
                   %(review_status)s, %(review_reason)s, %(is_static)s,
                   %(event_date_column)s, %(structure_hash)s, 'llm', now(), false)
           ON CONFLICT (database_name, table_name) DO UPDATE SET
               description = EXCLUDED.description, review_reason = EXCLUDED.review_reason,
               event_date_column = EXCLUDED.event_date_column,
               summary = EXCLUDED.summary, grain = EXCLUDED.grain, is_static = EXCLUDED.is_static,
               concept = EXCLUDED.concept, source_type = EXCLUDED.source_type,
               scope = EXCLUDED.scope, authoritative = EXCLUDED.authoritative,
               authority_confidence = EXCLUDED.authority_confidence,
               use_instead = EXCLUDED.use_instead, requires_dedup = EXCLUDED.requires_dedup,
               dedup_method = EXCLUDED.dedup_method, dedup_key = EXCLUDED.dedup_key,
               version_col = EXCLUDED.version_col, delete_col = EXCLUDED.delete_col,
               quirks = EXCLUDED.quirks, variables = EXCLUDED.variables,
               relationships = EXCLUDED.relationships, conflict_type = EXCLUDED.conflict_type,
               review_status = EXCLUDED.review_status, structure_hash = EXCLUDED.structure_hash,
               updated_at = now(), retired = false
           WHERE sentinel.catalog_overlay.updated_by <> 'human'""",  # never overwrite pinned rows
        row,
    )


# ─── 6+7. LLM pass + authority ────────────────────────────────────────────────

def _cdc_cols(columns):
    names = {c["name"] for c in columns}
    version = "_peerdb_version" if "_peerdb_version" in names else None
    delete = "_peerdb_is_deleted" if "_peerdb_is_deleted" in names else None
    return version, delete


def build_overlay_row(meta, columns, inferred, review):
    version_col, delete_col = _cdc_cols(columns)
    # A CDC/Replacing table with no dedup key is an I3 violation → unresolved_dedup
    requires_dedup = bool(inferred["requires_dedup"]) or "Replacing" in (meta["engine"] or "")
    dedup_key = inferred.get("dedup_key") or []
    conflict = "none"
    if requires_dedup and not dedup_key:
        conflict = "unresolved_dedup"
    desc = inferred.get("description") or {}
    # summary is a compact derivative; prefer the model's, else what_it_is
    summary = inferred.get("summary") or desc.get("what_it_is")
    return {
        "database_name": meta["database_name"], "table_name": meta["table_name"],
        "description": Json(desc), "summary": summary, "grain": inferred.get("grain"),
        "concept": inferred.get("concept"), "source_type": inferred.get("source_type"),
        "scope": inferred.get("scope"),
        "authoritative": False,  # set in reconcile_authority (§7)
        "authority_confidence": inferred.get("authority_confidence"),
        "use_instead": None,
        "requires_dedup": requires_dedup,
        "dedup_method": inferred.get("dedup_method") or ("argmax" if version_col else "none"),
        "dedup_key": dedup_key,
        "version_col": version_col, "delete_col": delete_col,
        "quirks": inferred.get("quirks") or [],
        "variables": Json(inferred.get("variables") or []),
        "relationships": Json(inferred.get("relationships") or []),
        "conflict_type": conflict,
        "review_status": "needs_review" if review else "confirmed",
        "review_reason": inferred.get("_review_reason"),
        "event_date_column": inferred.get("event_date_column"),
        "is_static": bool(inferred.get("is_static")),
        "structure_hash": meta["structure_hash"],
        "_concept": inferred.get("concept"), "_scope": inferred.get("scope"),
        "_source_type": inferred.get("source_type"),
        "_confidence": float(inferred.get("authority_confidence") or 0),
    }


def reconcile_authority(pg_cur, touched_keys):
    """§7.1 I1/I2: for each (concept, scope, source_type) group, exactly one
    authoritative table. Highest authority_confidence wins; ties or zero-confidence
    → authority_gap/collision flagged (non-blocking). Non-authoritative get
    use_instead → the group's authoritative table (I2). Runs over ALL non-retired
    rows so re-evaluation is global, not just this run's touched set."""
    pg_cur.execute("""
        SELECT database_name, table_name, concept, scope, source_type,
               authority_confidence, updated_by
        FROM sentinel.catalog_overlay
        WHERE retired = false AND concept IS NOT NULL AND source_type IS NOT NULL
    """)
    rows = pg_cur.fetchall()
    groups = {}
    for db, tbl, concept, scope, stype, conf, updated_by in rows:
        groups.setdefault((concept, scope or "", stype), []).append(
            {"db": db, "tbl": tbl, "conf": conf or 0, "pinned": updated_by == "human"}
        )

    for (concept, scope, stype), members in groups.items():
        pinned = [m for m in members if m["pinned"] and _is_pinned_authoritative(pg_cur, m)]
        if pinned:
            winner = pinned[0]
            conflict = "authority_collision" if len(pinned) > 1 else "none"
        else:
            # Rank by confidence, then break ties DETERMINISTICALLY so we always pick a
            # single authority instead of flagging a collision. A confidence tie is NOT a
            # real conflict — we're *assigning* authority, so pick one (prefer a _BI mart
            # / *ALL rollup, then more rows, then name) and alias the rest. This avoids a
            # mass authority_collision flood when many same-concept tables tie on conf.
            def _tiebreak(m):
                n = m["tbl"].lower()
                mart = 1 if (n.endswith("bi") or n.endswith("all") or "_bi" in n) else 0
                return (m["conf"], mart, m["tbl"])   # higher conf, mart-ness, then name
            ranked = sorted(members, key=_tiebreak, reverse=True)
            winner = ranked[0]
            top_conf = ranked[0]["conf"]
            # only a genuine gap (nobody has any confidence) is a conflict now
            conflict = "authority_gap" if top_conf == 0 else "none"

        for m in members:
            is_auth = (m["db"], m["tbl"]) == (winner["db"], winner["tbl"]) and conflict != "authority_gap"
            use_instead = None if is_auth else f"{winner['db']}.{winner['tbl']}"
            # dangling_pointer: a non-authoritative row with no valid target
            row_conflict = conflict
            if not is_auth and conflict == "authority_gap":
                row_conflict = "dangling_pointer"
            # Re-flag needs_review on a real authority conflict — but NEVER un-confirm a
            # row a human or the resolver already confirmed (their confirmation stands;
            # an authority conflict is surfaced via conflict_type, not by reopening review).
            # A human- or resolver-confirmed row is settled: update authority pointers
            # but do NOT touch its conflict_type or review_status (else a re-computed
            # conflict reopens a row that was already decided → the review queue never
            # drains). For llm/unreviewed rows, flag conflicts + review as before.
            pg_cur.execute(
                """UPDATE sentinel.catalog_overlay
                   SET authoritative = %s,
                       use_instead = %s,
                       conflict_type = CASE
                           WHEN updated_by IN ('human','resolver') THEN conflict_type
                           WHEN conflict_type = 'unresolved_dedup' THEN conflict_type
                           ELSE %s END,
                       review_status = CASE
                           WHEN updated_by IN ('human','resolver') THEN review_status
                           WHEN %s <> 'none' THEN 'needs_review'
                           ELSE review_status END
                   WHERE database_name = %s AND table_name = %s
                     AND updated_by <> 'human'""",
                (is_auth, use_instead, row_conflict, row_conflict, m["db"], m["tbl"]),
            )


def _is_pinned_authoritative(pg_cur, m):
    pg_cur.execute(
        "SELECT authoritative FROM sentinel.catalog_overlay "
        "WHERE database_name = %s AND table_name = %s",
        (m["db"], m["tbl"]),
    )
    r = pg_cur.fetchone()
    return bool(r and r[0])


# ─── 9. Monitor targets ────────────────────────────────────────────────────────

def upsert_target(pg_cur, meta, inferred):
    weeks = inferred.get("monitor_frequency_weeks", 1)          # how often the CHECK runs
    expected = inferred.get("expected_cadence_weeks")           # how stale = bad (freshness)
    static = bool(inferred.get("is_static"))
    event_col = inferred.get("event_date_column")               # business date → data freshness
    # system.parts is validated at check time; default cheap_source = system_parts,
    # check engine falls back to light_scan if unreliable (Lapse 4).
    pg_cur.execute(
        """INSERT INTO sentinel.monitor_targets
           (database_name, table_name, variable, monitor_frequency_weeks,
            expected_cadence_weeks, is_static, event_date_column, cheap_source, status,
            next_due_at, selected_by, iteration)
           VALUES (%s, %s, '', %s, %s, %s, %s, 'system_parts', 'active', now(), 'llm', 0)
           ON CONFLICT (database_name, table_name, variable) DO UPDATE SET
               monitor_frequency_weeks = EXCLUDED.monitor_frequency_weeks,
               expected_cadence_weeks = EXCLUDED.expected_cadence_weeks,
               is_static = EXCLUDED.is_static,
               event_date_column = EXCLUDED.event_date_column,
               status = CASE WHEN sentinel.monitor_targets.status = 'retired'
                             THEN 'active' ELSE sentinel.monitor_targets.status END,
               iteration = sentinel.monitor_targets.iteration + 1""",
        (meta["database_name"], meta["table_name"], weeks, expected, static, event_col),
    )


# ─── 9b. Variable values + per-variable targets (coverage bootstrap) ────────────
#
# A tracked "variable" = a BUSINESS SEGMENTATION dimension (brand, channel, region,
# category) whose value-set we snapshot so coverage can flag a value that later
# disappears. NOT ids, free-text, flags, measures, or CDC columns.
#
# Selection (business-dimensions-only, capped at 3):
#   1. LLM labels role='segment' and orders variables by monitoring priority.
#   2. Code gate drops anything non-business: CDC/technical cols, and (defensively)
#      any that slipped through with a non-segment role.
#   3. A live cardinality probe keeps only genuine segments (MIN_CARD..MAX_DISTINCT
#      distinct values) — excludes 1-2 value booleans and runaway high-cardinality.
#   4. Take the first MAX_TRACKED_VARIABLES that pass, in the LLM's priority order.

MAX_TRACKED_VARIABLES = 3     # cap per table
MIN_SEGMENT_CARD = 3          # a real segment has >=3 distinct values (excludes bool flags)

# Technical / CDC columns are never business segments.
_CDC_PREFIXES = ("_peerdb", "_sign", "_version", "_ver", "__")


def _is_business_segment_col(col_name, role):
    if role != "segment":
        return False
    lc = col_name.lower()
    if any(lc.startswith(p) for p in _CDC_PREFIXES):
        return False
    return True


def _segment_cardinality(ch, db, tbl, col):
    """Cheap distinct-count probe. Returns int or None on error."""
    try:
        r = ch.query(f"SELECT uniqExact(`{col}`) FROM `{db}`.`{tbl}`")
        return int(r.result_rows[0][0] or 0)
    except Exception as e:
        log.warning("cardinality probe failed %s.%s.%s: %s", db, tbl, col, e)
        return None


def _segment_values(ch, db, tbl, col):
    """Fetch the current distinct value set (as toString, matching the coverage
    check). Bounded by MAX_DISTINCT. Reuses cached samples when present."""
    r = ch.query(
        f"SELECT DISTINCT toString(`{col}`) FROM `{db}`.`{tbl}` "
        f"WHERE length(toString(`{col}`)) <= {MAX_STR_LEN} LIMIT {MAX_DISTINCT}"
    )
    return {row[0] for row in r.result_rows}


def populate_variable_values(pg_cur, ch, meta, inferred, samples):
    db, tbl = meta["database_name"], meta["table_name"]
    tracked = []
    for var in (inferred.get("variables") or []):     # already priority-ordered by LLM
        if len(tracked) >= MAX_TRACKED_VARIABLES:
            break
        col = var.get("column")
        role = (var.get("role") or "").lower()
        if not col or not _is_business_segment_col(col, role):
            continue
        # cardinality probe: keep genuine segments only (excludes bool flags + runaway)
        card = _segment_cardinality(ch, db, tbl, col)
        if card is None or card < MIN_SEGMENT_CARD or card > MAX_DISTINCT:
            log.info("skip variable %s.%s.%s (cardinality=%s not in [%d,%d])",
                     db, tbl, col, card, MIN_SEGMENT_CARD, MAX_DISTINCT)
            continue
        try:
            seen_now = set(samples[col]) if col in samples else _segment_values(ch, db, tbl, col)
        except Exception as e:
            log.warning("value fetch failed %s.%s.%s: %s", db, tbl, col, e)
            continue
        if not seen_now:
            continue
        tracked.append(col)

        for value in seen_now:
            pg_cur.execute(
                """INSERT INTO sentinel.variable_values
                   (database_name, table_name, variable, value, first_seen, last_seen, lifecycle)
                   VALUES (%s, %s, %s, %s, now(), now(), 'active')
                   ON CONFLICT (database_name, table_name, variable, value) DO UPDATE SET
                       last_seen = now(), lifecycle = 'active', retired_at = NULL""",
                (db, tbl, col, value),
            )
        pg_cur.execute(
            """UPDATE sentinel.variable_values
               SET lifecycle = 'retired', retired_at = now()
               WHERE database_name = %s AND table_name = %s AND variable = %s
                 AND lifecycle = 'active' AND value <> ALL(%s)""",
            (db, tbl, col, list(seen_now)),
        )
        pg_cur.execute(
            """INSERT INTO sentinel.monitor_targets
               (database_name, table_name, variable, monitor_frequency_weeks,
                expected_cadence_weeks, cheap_source, status, next_due_at, selected_by, iteration)
               VALUES (%s, %s, %s, %s, %s, 'light_scan', 'active', now(), 'llm', 0)
               ON CONFLICT (database_name, table_name, variable) DO UPDATE SET
                   monitor_frequency_weeks = EXCLUDED.monitor_frequency_weeks,
                   expected_cadence_weeks = EXCLUDED.expected_cadence_weeks,
                   status = CASE WHEN sentinel.monitor_targets.status = 'retired'
                                 THEN 'active' ELSE sentinel.monitor_targets.status END,
                   iteration = sentinel.monitor_targets.iteration + 1""",
            (db, tbl, col, inferred.get("monitor_frequency_weeks", 1),
             inferred.get("expected_cadence_weeks")),
        )
    log.info("%s.%s tracked %d variables: %s", db, tbl, len(tracked), tracked)
    return tracked


# ─── 10. Reconciliation rules (observe) ────────────────────────────────────────

def _insert_rule(pg_cur, concept, metric, source_a, source_b, direction, tol, dimension=None):
    pg_cur.execute(
        """INSERT INTO sentinel.reconciliation_rules
           (concept, metric, source_a, source_b, dimension, direction, tolerance_pct, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'observe')
           ON CONFLICT (concept, metric, source_a, source_b, COALESCE(dimension, ''))
           DO NOTHING""",
        (concept or "unknown", metric, source_a, source_b, dimension, direction, tol),
    )


def generate_recon_rules(pg_cur, overlay_row, inferred, tracked_vars):
    """Emit rules in 'observe' (§9). All four kinds:
    1 segment-vs-total   — Σ over a tracked dimension ≈ table total
    2 referential integrity — a relationship's join column ⊆ the referenced table
    3 duplicate-concept divergence — authoritative vs its use_instead counterpart
    4 cross-source agreement — same concept, different source_type, roughly agree
    observe→active promotion is the safeguard; discovery only proposes."""
    db, tbl = overlay_row["database_name"], overlay_row["table_name"]
    concept = overlay_row.get("_concept")
    stype = overlay_row.get("_source_type")
    src = f"{db}.{tbl}"

    # 1. segment-vs-total: for each tracked dimension, Σ(by value) ≈ table total
    for col in tracked_vars:
        _insert_rule(pg_cur, concept, "segment_vs_total", f"{src}#{col}", src, "a≈b", 1.0, dimension=col)

    # 2. referential integrity from relationships
    for rel in (inferred.get("relationships") or []):
        to, on = rel.get("to"), rel.get("on")
        if to and on:
            _insert_rule(pg_cur, concept, "referential_integrity", src, to, "subset", 0.0, dimension=on)

    # 3. duplicate-concept divergence: authoritative vs its use_instead (both still exist)
    ui = overlay_row.get("use_instead")
    if ui and not overlay_row.get("authoritative"):
        _insert_rule(pg_cur, concept, "duplicate_concept_divergence", src, ui, "a≈b", 2.0)

    # Rule kind 4 (cross-source agreement) is generated AFTER authority is assigned —
    # see generate_cross_source_rules(). Doing it here fails: authority is set at the
    # end of the run, so mid-loop every row is authoritative=false and the pairing
    # query matches nothing.


def _shared_measure(pg_cur, a_db, a_tbl, b_db, b_tbl):
    """Pick a measure column present (by name, case-insensitive) in BOTH tables'
    variables, preferring a 'sales'/'net'/'gross'-type measure. Returns the column
    name to sum on each side, or None. The cross-source rule sums this measure so a
    gross-vs-net gap shows as a large diff — a row COUNT would miss it."""
    def measures(db, tbl):
        pg_cur.execute(
            "SELECT variables FROM sentinel.catalog_overlay "
            "WHERE database_name=%s AND table_name=%s", (db, tbl))
        r = pg_cur.fetchone()
        cols = []
        for v in (r[0] if r and r[0] else []):
            if (v.get("role") or "").lower() == "measure" and v.get("column"):
                cols.append(v["column"])
        return cols
    a_m = {m.lower().replace(" ", "_"): m for m in measures(a_db, a_tbl)}
    b_m = {m.lower().replace(" ", "_"): m for m in measures(b_db, b_tbl)}
    shared = set(a_m) & set(b_m)
    if not shared:
        return None
    # prefer a sales/net/gross measure for a meaningful reconciliation
    for pref in ("net_sales", "netsales", "gross_sales", "sales", "mrp_sales", "amount"):
        for k in shared:
            if pref in k:
                return a_m[k], b_m[k]
    k = sorted(shared)[0]
    return a_m[k], b_m[k]


def generate_cross_source_rules(pg_cur):
    """§9.4 (generalized) — two tables of the SAME concept that share a measure should
    roughly agree on its SUM. Covers both the classic case (ERP-invoiced vs channel-
    reported sales, different source_type) AND same-source variants (a gross table vs a
    net table). A large diff on a shared measure = they measure different things
    (gross vs net) or one is wrong — surface it. Runs AFTER authority; reconciles the
    SUMMED MEASURE (not row count) so magnitude gaps show. Emits in observe.

    Pairs any two same-concept, non-retired tables that share a measure — NOT gated on
    source_type (the gross-vs-net pair is same source_type) and NOT gated on both being
    authoritative (the whole point is to compare a table against its concept peers)."""
    pg_cur.execute("""
        SELECT database_name, table_name, concept
        FROM sentinel.catalog_overlay
        WHERE retired = false AND concept IS NOT NULL
    """)
    rows = pg_cur.fetchall()
    by_concept = {}
    for db, tbl, concept in rows:
        by_concept.setdefault(concept, []).append((db, tbl))
    MAX_PAIRS_PER_CONCEPT = 20  # guard against N^2 blowup on a huge concept group
    for concept, members in by_concept.items():
        pairs = 0
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if pairs >= MAX_PAIRS_PER_CONCEPT:
                    log.info("cross-source: concept %s capped at %d pairs", concept, MAX_PAIRS_PER_CONCEPT)
                    break
                (adb, atbl), (bdb, btbl) = members[i], members[j]
                sm = _shared_measure(pg_cur, adb, atbl, bdb, btbl)
                if not sm:
                    continue  # no comparable measure → can't reconcile values
                a_col, b_col = sm
                a_ref, b_ref = f"{adb}.{atbl}::{a_col}", f"{bdb}.{btbl}::{b_col}"
                a, b = sorted([a_ref, b_ref])
                _insert_rule(pg_cur, concept, "cross_source_agreement", a, b, "a≈b", 5.0)
                pairs += 1
                log.info("cross-source rule: %s.%s vs %s.%s on measure %s/%s",
                         adb, atbl, bdb, btbl, a_col, b_col)


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Sentinel discovery starting @ %s", NOW)
    pg = pg_connect()
    ch = ch_connect()
    llm_count = skip_count = 0

    try:
        with pg.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            if not cur.fetchone()[0]:
                log.warning("Another discovery run holds the lock. Exiting.")
                return

            existing = load_existing_overlay(cur)
            tables = enumerate_tables(ch)
            log.info("Enumerated %d ClickHouse tables", len(tables))

            seen_keys = set()
            full_pass = True   # False if we break early on the batch cap
            for meta in tables:
                db, tbl = meta["database_name"], meta["table_name"]
                key = (db, tbl)
                seen_keys.add(key)
                columns = introspect_columns(ch, db, tbl)
                if not columns:
                    continue
                meta["structure_hash"] = structure_hash(columns)

                prior = existing.get(key)
                # 4. Incremental gate — unchanged structure skips the LLM. This is what
                # makes PROGRESSIVE scanning work: tables catalogued in an earlier run
                # are skipped here, so each run advances the frontier of new/changed
                # tables until the whole catalog is filled, then steady-state is cheap.
                if prior and prior["structure_hash"] == meta["structure_hash"]:
                    skip_count += 1
                    continue

                # Per-run batch cap: only process up to MAX_TABLES_PER_RUN new/changed
                # tables, so a run over 450 tables never risks the Cloud Run task timeout.
                # The next scheduled (or stacked manual) run picks up the rest.
                if llm_count >= MAX_TABLES_PER_RUN:
                    log.info("Batch cap reached (%d tables this run); remaining tables "
                             "will be picked up next run.", MAX_TABLES_PER_RUN)
                    full_pass = False
                    break

                # 5. PII-safe sample
                samples = pii_safe_samples(ch, db, tbl, columns)
                mstats = measure_stats(ch, db, tbl, columns)
                payload = {**meta, "columns": columns, "samples": samples,
                           "measure_stats": mstats}

                # 6. LLM pass + heuristic pre-fill for source_type
                try:
                    inferred, maker = llm.infer_overlay(payload)
                except Exception as e:
                    log.error("LLM failed for %s.%s, skipping: %s", db, tbl, e)
                    continue
                heur = source_type_heuristic(db, tbl)
                if heur:
                    inferred["source_type"] = heur
                # Guard the LLM's event_date_column — must be a real column, and never a
                # sync/CDC column (those are sync freshness, not data freshness).
                edc = inferred.get("event_date_column")
                colset = {c["name"] for c in columns}
                if edc and (edc not in colset or edc.startswith("_peerdb") or edc in ("_sign",)):
                    log.info("dropping bad event_date_column %r for %s.%s", edc, db, tbl)
                    inferred["event_date_column"] = None
                llm_count += 1

                # maker-checker — only material disagreements (source_type/dedup) flag review.
                # When a deterministic name heuristic set source_type, it is authoritative —
                # exclude source_type from the checker comparison (else the checker's free
                # guess always "disagrees" with the heuristic and flags every table).
                agree, checker, disagree_reason = llm.check(
                    payload, inferred, maker, skip_source_type=bool(heur))
                review = llm.needs_review(inferred, agree)
                conf = float(inferred.get("authority_confidence") or 0)
                # human-readable reason for the UI (why this row needs review)
                if not review:
                    review_reason = None
                elif not agree:
                    review_reason = f"maker/checker disagree — {disagree_reason}"
                else:
                    review_reason = f"low confidence ({conf:.2f} < {llm.CONFIRM_THRESHOLD})"
                inferred["_review_reason"] = review_reason
                log.info("%s.%s inferred by %s (checker=%s agree=%s review=%s conf=%.2f)",
                         db, tbl, maker, checker, agree, review, conf)

                overlay_row = build_overlay_row(meta, columns, inferred, review)
                upsert_overlay(cur, overlay_row)
                upsert_target(cur, meta, inferred)
                tracked_vars = populate_variable_values(cur, ch, meta, inferred, samples)
                # authority (use_instead) is set later in reconcile_authority, so
                # duplicate-concept rules for this run use the prior authority state;
                # they converge on the next run — acceptable for observe-stage rules.
                generate_recon_rules(cur, overlay_row, inferred, tracked_vars)

                # Commit each table immediately: catalog populates live (visible in the
                # UI as it goes), and a timeout/crash never loses completed work — the
                # incremental gate skips them on the next run. Advisory lock is
                # session-scoped so it survives these commits.
                pg.commit()

            # 8. Dropped tables → retire — ONLY on a full pass. On a capped run,
            # unvisited tables are absent from seen_keys and would be wrongly retired.
            if full_pass:
                gone = set(existing) - seen_keys
                for db, tbl in gone:
                    cur.execute(
                        "UPDATE sentinel.catalog_overlay SET retired = true, updated_at = now() "
                        "WHERE database_name = %s AND table_name = %s AND updated_by <> 'human'",
                        (db, tbl),
                    )
                    cur.execute(
                        "UPDATE sentinel.monitor_targets SET status = 'retired' "
                        "WHERE database_name = %s AND table_name = %s",
                        (db, tbl),
                    )
                if gone:
                    log.info("Retired %d dropped tables", len(gone))
            else:
                log.info("Partial run (batch cap) — skipping dropped-table retirement.")

            # 7. Global authority reconciliation (I1/I2) after all upserts
            reconcile_authority(cur, seen_keys)

            # 8. Cross-source agreement rules — MUST run after authority is set (§9.4)
            generate_cross_source_rules(cur)

            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            pg.commit()
            log.info("Discovery committed. LLM ran on %d tables, skipped %d unchanged.",
                     llm_count, skip_count)

    except Exception as e:
        pg.rollback()
        log.error("Discovery failed: %s", e)
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
    log.info("Sentinel discovery complete.")


if __name__ == "__main__":
    main()
