"""
Sentinel Auto-Resolver — Control Tower.

Resolves needs_review catalog rows before any human looks at them. Most reviews are
model disagreements (source_type/dedup) or low confidence — an independent adjudicator
LLM, given the disagreement + the actual column/stat evidence, lands on a confident
answer for the majority. High-confidence adjudications are auto-confirmed (updated_by=
'resolver'); genuinely ambiguous ones are left for a human with the adjudicator's note.

Reuses the discovery introspection + LLM modules (copied into this image). Never
overrides human-pinned rows. Idempotent; advisory-locked.
"""
import os, json, logging, datetime
import psycopg2
from psycopg2.extras import Json

# discovery/main.py is copied into this image as discovery_lib.py; llm.py alongside.
import discovery_lib as disc
import llm

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

PG_CONN = os.environ["PG_CONN"]
CH_HOST = os.environ["CH_HOST"]
ADVISORY_LOCK_KEY = 0x53454E52  # "SENR"
MAX_RESOLVE_PER_RUN = int(os.environ.get("SENTINEL_MAX_RESOLVE_PER_RUN", "120"))


def load_review_rows(cur):
    """needs_review rows that are NOT human-pinned and not retired."""
    cur.execute("""
        SELECT database_name, table_name, source_type, concept, requires_dedup,
               dedup_method, dedup_key, review_reason, conflict_type
        FROM sentinel.catalog_overlay
        WHERE retired = false AND review_status = 'needs_review' AND updated_by <> 'human'
        ORDER BY authority_confidence NULLS FIRST
        LIMIT %s
    """, (MAX_RESOLVE_PER_RUN,))
    cols = ["database_name", "table_name", "source_type", "concept", "requires_dedup",
            "dedup_method", "dedup_key", "review_reason", "conflict_type"]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def apply_resolution(cur, row, adj):
    """Auto-confirm: update the correctness fields to the adjudicator's answer, mark
    confirmed, record who/why. updated_by='resolver' (not 'human') so discovery can
    still re-evaluate on a real structural change, but review_status stays confirmed."""
    reason = f"auto-resolved by {adj.get('_adjudicator')}: {adj.get('reasoning','')}"[:500]
    cur.execute("""
        UPDATE sentinel.catalog_overlay
        SET source_type = %s, concept = %s, requires_dedup = %s, dedup_method = %s,
            dedup_key = %s, review_status = 'confirmed', review_reason = %s,
            authority_confidence = GREATEST(authority_confidence, %s),
            updated_by = 'resolver', updated_at = now()
        WHERE database_name = %s AND table_name = %s AND updated_by <> 'human'
    """, (adj["source_type"], adj.get("concept") or row["concept"],
          bool(adj.get("requires_dedup")), adj.get("dedup_method") or row["dedup_method"],
          adj.get("dedup_key") or row["dedup_key"], reason, adj["confidence"],
          row["database_name"], row["table_name"]))
    # keep the target's source-type-derived fields consistent enough; discovery owns
    # the rest. Also propagate to source_type_vocab if a new code appeared.
    disc.ensure_source_type(cur, adj["source_type"])


def note_unresolved(cur, row, adj):
    """Left for a human — store the adjudicator's take so the human has a head start."""
    note = (f"needs human: adjudicator {adj.get('_adjudicator')} unsure "
            f"(conf {adj.get('confidence'):.2f}) — {adj.get('reasoning','')}")[:500]
    cur.execute("""
        UPDATE sentinel.catalog_overlay SET review_reason = %s, updated_at = now()
        WHERE database_name = %s AND table_name = %s AND updated_by <> 'human'
    """, (note, row["database_name"], row["table_name"]))


def main():
    log.info("Sentinel resolver starting.")
    pg = disc.pg_connect()
    ch = disc.ch_connect()
    resolved = escalated = failed = 0
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            if not cur.fetchone()[0]:
                log.warning("Another resolver run holds the lock. Exiting.")
                return
            rows = load_review_rows(cur)
            log.info("%d needs_review rows to adjudicate", len(rows))

            for row in rows:
                db, tbl = row["database_name"], row["table_name"]
                try:
                    columns = disc.introspect_columns(ch, db, tbl)
                    if not columns:
                        continue
                    payload = {
                        "database_name": db, "table_name": tbl, "engine": "?",
                        "sorting_key": "?", "total_rows": 0, "columns": columns,
                        "samples": disc.pii_safe_samples(ch, db, tbl, columns),
                        "measure_stats": disc.measure_stats(ch, db, tbl, columns),
                    }
                    adj = llm.adjudicate(payload, row, row.get("review_reason"))
                except Exception as e:
                    log.warning("adjudication failed for %s.%s: %s", db, tbl, e)
                    failed += 1
                    continue

                conf = float(adj.get("confidence") or 0)
                if conf >= llm.RESOLVE_THRESHOLD:
                    apply_resolution(cur, row, adj)
                    resolved += 1
                    log.info("RESOLVED %s.%s → source_type=%s conf=%.2f (%s)",
                             db, tbl, adj["source_type"], conf, adj.get("_adjudicator"))
                else:
                    note_unresolved(cur, row, adj)
                    escalated += 1
                    log.info("ESCALATE %s.%s (conf %.2f — left for human)", db, tbl, conf)
                pg.commit()  # per-row: timeout-safe, live progress

            # After resolving authority-related rows, re-run global authority so
            # use_instead/conflict_type reflect the new confirmed source_types.
            disc.reconcile_authority(cur, set())
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            pg.commit()
            log.info("Resolver done. resolved=%d escalated=%d failed=%d", resolved, escalated, failed)
    except Exception as e:
        pg.rollback()
        log.error("Resolver failed: %s", e)
        raise
    finally:
        pg.close()
        ch.close()


if __name__ == "__main__":
    main()
