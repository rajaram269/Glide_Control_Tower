"""
Sentinel discovery — LLM module.

Provider cascade (maker):   OpenAI → Gemini → Claude  (on error/timeout).
Maker-checker:              for low-confidence or authority/dedup decisions, a
                            *different* provider re-derives the same fields; agree
                            → confirm, disagree → needs_review (non-blocking, §7.4).

All calls are structured JSON output, validated against OVERLAY_SCHEMA before use.
The prompt builder receives only a PII-safe payload (schema + stats + low-card
DISTINCT samples). It has no path to raw rows; recruitment_hr is schema+stats only,
asserted by the caller (see sample.py / main.py). — DATA_MONITOR_SPEC §10.3, §7.4
"""
import os, json, logging

log = logging.getLogger(__name__)

CONFIRM_THRESHOLD = float(os.environ.get("SENTINEL_CONFIRM_THRESHOLD", "0.80"))
# Org default currency for money measures. The LLM cannot know currency from schema —
# it must NOT guess (it hallucinated "USD" for Meta ad spend). Assume this unless a
# column name/sample explicitly says otherwise (e.g. a currencyCode column, a *_usd name).
DEFAULT_CURRENCY = os.environ.get("SENTINEL_DEFAULT_CURRENCY", "INR")

# Provider order for the maker cascade. Each entry: (name, callable factory).
# Checker uses the *next* provider in this list (different from the maker).
_PROVIDER_ORDER = ["openai", "gemini", "anthropic"]

# Fields the LLM fills. Deterministic source_type heuristics (main.py) may pre-fill
# source_type; the LLM confirms/overrides only when the heuristic abstains.
OVERLAY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": "object", "properties": {
            "what_it_is":         {"type": "string"},
            "purpose":            {"type": "string"},
            "key_facts":          {"type": "array", "items": {"type": "string"}},
            "when_to_use":        {"type": "string"},
            "when_not_to_use":    {"type": "string"},
            "computable_metrics": {"type": "array", "items": {"type": "string"}},
            "not_computable":     {"type": "array", "items": {"type": "string"}},
        }},
        "summary":     {"type": "string"},
        "grain":       {"type": "string"},
        "concept":     {"type": "string"},
        "source_type": {"type": "string"},
        "scope":       {"type": ["string", "null"]},
        "variables":   {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "role":   {"type": "string"},
                "note":   {"type": ["string", "null"]},
            },
            "required": ["column", "role"],
        }},
        "relationships": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "to":      {"type": "string"},
                "on":      {"type": "string"},
                "purpose": {"type": ["string", "null"]},
            },
            "required": ["to", "on"],
        }},
        "quirks":       {"type": "array", "items": {"type": "string"}},
        "monitor_frequency_weeks": {"type": "integer", "minimum": 1, "maximum": 4},
        # How often the DATA is expected to update, in decimal weeks. Drives the
        # freshness verdict (fresh <= this, stale <= 3x, dead beyond). Sub-week is
        # expressible: daily = 0.1428, 2h = 0.0119, weekly = 1.0, monthly = 4.0.
        "expected_cadence_weeks": {"type": "number", "minimum": 0.001, "maximum": 8},
        "requires_dedup": {"type": "boolean"},
        "dedup_method":   {"type": "string", "enum": ["argmax", "final", "none"]},
        "dedup_key":      {"type": "array", "items": {"type": "string"}},
        "authority_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "is_static":      {"type": "boolean"},
    },
    "required": [
        "description", "summary", "grain", "concept", "source_type", "variables",
        "monitor_frequency_weeks", "expected_cadence_weeks", "requires_dedup",
        "dedup_method", "dedup_key", "authority_confidence", "is_static",
    ],
}

_SYSTEM = (
    "You are a data-catalog analyst. Given a ClickHouse table's schema, engine, and "
    "PII-safe stats/samples, infer non-derivable metadata. Never invent column names "
    "not present. If a name is fixable at source, still describe meaning. Return ONLY "
    "JSON matching the given schema.\n"
    "CRITICAL — trust EVIDENCE over column NAMES. A column's name is a claim, not a "
    "fact; verify it against the aggregate stats before believing it:\n"
    "  • A column named 'net'/'net_sales' with negatives≈0 (no returns represented) is "
    "NOT true net — it is unadjusted GROSS mislabeled. Put this in not_computable "
    "('net sales: <col> named net but negatives≈0, no returns/discount breakup, so true "
    "net is NOT derivable') and do NOT list net in computable_metrics.\n"
    "  • Net sales is only computable when returns/discounts are actually represented "
    "(a returns/refund transaction type AND signed negatives, or an explicit discount "
    "column). Cite the specific columns/stats that make it possible.\n"
    "  • If the same-named measure has a very different magnitude than typical (e.g. an "
    "avg orders-of-magnitude off), say so — it signals the column means something else.\n"
    f"  • CURRENCY: money measures are in {DEFAULT_CURRENCY} by DEFAULT. Do NOT guess a "
    f"currency from the platform or table name (e.g. do not assume Meta/ad spend is USD). "
    f"State '{DEFAULT_CURRENCY}' unless a column name or sample explicitly indicates another "
    f"currency (a currencyCode column, a name ending _usd/_eur, a currency symbol in "
    f"samples). If a currencyCode column exists, say amounts are in the currency it "
    f"specifies rather than naming one.\n"
    "Be conservative with authority_confidence: it is "
    "how sure you are this table is the single canonical source for its "
    "(concept, scope, source_type). Set expected_cadence_weeks to how often the DATA "
    "in this table is expected to update — infer from the table's purpose (a live "
    "orders/inventory feed updates daily or faster ~0.02-0.14; a reference/lookup "
    "table monthly ~4; marketing insight tables daily ~0.14). Decimal weeks: "
    "daily=0.1428, hourly=0.006, weekly=1.0, monthly=4.0. This is the freshness "
    "expectation, NOT how often to run the check."
)


# Exact output contract given to the model. Field names MUST match OVERLAY_SCHEMA —
# without this the model invents its own keys (short_description, table_id, …) and
# every validation fails, so discovery would skip every table.
_OUTPUT_SPEC = """Return a JSON object with EXACTLY these keys (use these exact names):
  "description": object — a CONCRETE, actionable description for a downstream
      consumer (an LLM or service that must decide whether to query this table and
      how to query it correctly). Be specific to THIS table's columns and data —
      never generic. Object shape:
        {
          "what_it_is": string — one precise line naming exactly what a row represents
                        and its origin system (e.g. "ERP accounts-payable ledger from
                        Business Central, one row per posted AP entry"). Not "a table
                        of records".
          "purpose": string — the concrete business questions this table answers
                     (e.g. "what we owe vendors, invoice aging, payables by vendor").
          "key_facts": array of strings — the things a query author MUST know: the
                       grain, update cadence, unit/currency of measures, dedup
                       requirement, and any surprising column semantics. 3-6 bullets,
                       each specific (e.g. "amounts are in INR", "requires dedup on
                       (id,entryNo) via ReplacingMergeTree or rows double-count",
                       "status columns are booleans, not lifecycle stages").
          "when_to_use": string — the questions/joins this table is the RIGHT source for.
          "when_not_to_use": string — concrete traps: wrong-source cases, what breaks
                       if you skip dedup, columns that look useful but aren't.
          "computable_metrics": array of strings — metrics this table CAN correctly
                       compute, each with WHY it's possible from the actual columns/stats
                       (e.g. "net sales: has Transaction_type/Final_Transaction_type
                       returns and signed negatives, so returns/discounts net out"; or
                       "gross sales, quantity, MRP"). MAY be empty [] for a pure
                       reference/lookup/dimension table that holds no measures.
          "not_computable": array of strings — metrics that CANNOT be correctly computed
                       here and WHY, especially where a column NAME is misleading (e.g.
                       "net sales: the 'Net sales' column is unadjusted gross — no return
                       or discount breakup (negatives ~0), so true net is NOT derivable").
                       Empty array if none. This is the most important field for choosing
                       between two same-concept tables — be precise and evidence-based.
        }
      Ground every field in the actual columns/engine/samples/measure-stats provided.
      Use the measure stats to decide computable vs not_computable (negatives present =
      returns representable = net derivable; magnitude tells gross from net). If you are
      unsure of a fact, say so briefly rather than inventing specifics.
  "summary": string — ONE line, derived from description.what_it_is, for compact lists
  "grain": string — precise "one row per ..." (name the business key, e.g.
      "one row per AP entry, keyed by (id, entryNo)")
  "concept": string — business concept (sales, inventory, spend, orders, ...)
  "source_type": string — one of: erp, sales_channel, perf_marketing, web_analytics, warehouse_ops, finance, reference, derived
  "scope": string or null — business subset (B2B, B2C, a brand) or null
  "variables": array of {"column": string, "role": string, "note": string or null}.
      role is one of: segment, measure, key, timestamp, other.
      Use "segment" ONLY for a business-partitioning dimension a stakeholder would
      group/filter reporting by AND expect a stable set of values — e.g. brand,
      sales channel, region, category, division, customer_type. A segment must be
      categorical with a small stable value set (roughly 3-50 distinct values).
      Do NOT label as segment: identifiers (id, sku, order_name, customer_id),
      free-text or high-cardinality strings (product_title, city, postal_code),
      boolean/status flags (open, onHold, is_active), monetary/numeric measures,
      or CDC/technical columns (anything starting with _peerdb, _sign, _version).
      Those get role measure / key / timestamp / other as appropriate.
      ORDER the variables array by monitoring priority: put the MOST important
      business segments FIRST (brand/channel/region before minor ones). Only the
      top few segments are monitored, so ordering matters.
  "relationships": array of {"to": "db.table", "on": "column", "purpose": string or null}
  "quirks": array of strings (may be empty)
  "monitor_frequency_weeks": integer 1-4 — how often to RUN the check
  "expected_cadence_weeks": number — how often the DATA updates (decimal: daily=0.1428, hourly=0.006, weekly=1.0, monthly=4.0)
  "requires_dedup": boolean — true for ReplacingMergeTree / CDC tables
  "dedup_method": string — one of: argmax, final, none
  "dedup_key": array of strings — business key columns (empty if none)
  "authority_confidence": number 0.0-1.0
  "is_static": boolean — true if this table is INTENTIONALLY frozen: a fixed
      reference/lookup/master loaded once and rarely or never refreshed (e.g. a pincode
      master, country list, chart-of-accounts), with no event/transaction timestamp that
      should keep advancing. false for any table that receives ongoing rows (sales,
      orders, marketing insights, inventory snapshots). When true, freshness will not be
      alarmed on this table.
Do not add, rename, or omit keys. Do not wrap in markdown."""


def _prompt(payload):
    return (
        f"{_SYSTEM}\n\n"
        f"Table: {payload['database_name']}.{payload['table_name']}\n"
        f"Engine: {payload.get('engine')}\n"
        f"ORDER BY / sorting key: {payload.get('sorting_key')}\n"
        f"Approx rows: {payload.get('total_rows')}\n"
        f"Columns (name: type):\n"
        + "\n".join(f"  {c['name']}: {c['type']}" for c in payload["columns"])
        + "\n\nLow-cardinality sample values (PII-safe only):\n"
        + json.dumps(payload.get("samples", {}), ensure_ascii=False)
        + "\n\nAggregate stats for measure columns (sum/avg/min/max/negatives over the "
          "whole column — use these to judge WHAT a measure really is and what can be "
          "computed). A 'negatives' count > 0 means returns/refunds are represented, so "
          "a net figure is derivable; negatives ~0 means returns are absent (likely "
          "gross-only). Very different magnitudes for a same-named measure across tables "
          "mean they are NOT the same metric.\n"
        + json.dumps(payload.get("measure_stats", {}), ensure_ascii=False)
        + "\n\n" + _OUTPUT_SPEC
    )


# ─── Provider callables ───────────────────────────────────────────────────────
# Each returns a dict (parsed JSON) or raises. Lazy imports so a missing SDK for
# one provider doesn't break the others.

def _call_openai(prompt):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=os.environ.get("SENTINEL_OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
        timeout=60,
    )
    return json.loads(resp.choices[0].message.content)


def _call_gemini(prompt):
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=os.environ.get("SENTINEL_GEMINI_MODEL", "gemini-2.5-flash"),
        contents=prompt + "\n\nRespond with a single JSON object only, no markdown.",
        config={"response_mime_type": "application/json", "temperature": 0},
    )
    return json.loads(resp.text)


def _call_anthropic(prompt):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=os.environ.get("SENTINEL_ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=1024,
        temperature=0,
        messages=[{"role": "user", "content": prompt + "\n\nReturn a single JSON object only."}],
    )
    text = msg.content[0].text.strip()
    # strip a ```json fence if present
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip()
    return json.loads(text)


_CALLERS = {"openai": _call_openai, "gemini": _call_gemini, "anthropic": _call_anthropic}


def _validate(obj):
    """Minimal structural validation — required keys present + basic types.
    Kept dependency-free (no jsonschema) to match the repo's thin style."""
    for k in OVERLAY_SCHEMA["required"]:
        if k not in obj:
            raise ValueError(f"missing required field: {k}")
    desc = obj["description"]
    if not isinstance(desc, dict):
        raise ValueError("description must be an object")
    for dk in ("what_it_is", "purpose", "when_to_use", "when_not_to_use"):
        if not (isinstance(desc.get(dk), str) and desc.get(dk).strip()):
            raise ValueError(f"description.{dk} must be a non-empty string")
    if not isinstance(desc.get("key_facts"), list) or not desc["key_facts"]:
        raise ValueError("description.key_facts must be a non-empty array")
    # both may be empty: a pure reference/lookup table has no computable metrics,
    # and a complete table has nothing in not_computable. Only require the arrays exist.
    if not isinstance(desc.get("computable_metrics"), list):
        raise ValueError("description.computable_metrics must be an array")
    if not isinstance(desc.get("not_computable"), list):
        raise ValueError("description.not_computable must be an array")
    if not isinstance(obj["variables"], list):
        raise ValueError("variables must be a list")
    if obj["dedup_method"] not in ("argmax", "final", "none"):
        raise ValueError(f"bad dedup_method: {obj['dedup_method']}")
    w = obj["monitor_frequency_weeks"]
    if not (isinstance(w, int) and 1 <= w <= 4):
        raise ValueError(f"monitor_frequency_weeks out of range: {w}")
    ec = obj["expected_cadence_weeks"]
    if not (isinstance(ec, (int, float)) and 0.001 <= ec <= 8):
        raise ValueError(f"expected_cadence_weeks out of range: {ec}")
    if not isinstance(obj["is_static"], bool):
        raise ValueError(f"is_static must be boolean: {obj['is_static']}")
    return obj


def _run(provider, prompt):
    """One provider call + validation, retrying invalid JSON once."""
    caller = _CALLERS[provider]
    for attempt in (1, 2):
        try:
            return _validate(caller(prompt))
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("%s returned invalid output (attempt %d): %s", provider, attempt, e)
    raise RuntimeError(f"{provider} failed validation twice")


def infer_overlay(payload):
    """Maker: try providers in order; return (result, maker_provider).
    Raises RuntimeError only if ALL providers fail."""
    last_err = None
    for provider in _PROVIDER_ORDER:
        try:
            return _run(provider, _prompt(payload)), provider
        except Exception as e:
            last_err = e
            log.warning("maker %s failed, falling through: %s", provider, e)
    raise RuntimeError(f"all LLM providers failed: {last_err}")


def check(payload, maker_result, maker_provider):
    """Maker-checker: a DIFFERENT provider re-derives the fields. Returns
    (agree: bool, checker_provider|None). Agreement = same concept + source_type +
    requires_dedup and dedup_key set-equal (the correctness-critical fields).
    Returns (True, None) if no other provider is available (fail-open to maker's
    result, since the confidence gate already flagged low-confidence rows)."""
    checkers = [p for p in _PROVIDER_ORDER if p != maker_provider]
    for provider in checkers:
        try:
            other = _run(provider, _prompt(payload))
        except Exception as e:
            log.warning("checker %s failed: %s", provider, e)
            continue
        agree = (
            other["concept"] == maker_result["concept"]
            and other["source_type"] == maker_result["source_type"]
            and bool(other["requires_dedup"]) == bool(maker_result["requires_dedup"])
            and set(other.get("dedup_key") or []) == set(maker_result.get("dedup_key") or [])
        )
        return agree, provider
    return True, None  # no checker available — don't block


def needs_review(maker_result, checker_agreed):
    """Confidence gate (§7.4): needs_review if low confidence OR checker disagreed."""
    conf = float(maker_result.get("authority_confidence") or 0)
    return conf < CONFIRM_THRESHOLD or not checker_agreed
