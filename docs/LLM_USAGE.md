# LLM usage in Control Tower — which model does what, and what it costs

Last updated: 2026-10-18. This file is the single place that lists every LLM call the project
makes. If you add or change one, update this file in the same commit.

**There is no Sonnet or Opus anywhere.** Every Anthropic call uses `claude-haiku-5-5` (the
cheapest current Claude). OpenAI uses `gpt-4o-mini`, Gemini uses `gemini-2.5-flash`.

## 1. At a glance

| # | Where it runs | When | Model(s) | What the model is asked to do |
|---|---|---|---|---|
| 1 | **Sentinel discovery** — maker (`ct-sentinel-discovery`) | Weekly, Mon 06:45 IST, **once per table** (again only if its columns change, or `CLASSIFIER_VERSION` is bumped; human-pinned tables are skipped) | OpenAI `gpt-4o-mini` → falls back to Gemini `gemini-2.5-flash` → falls back to Claude `claude-haiku-5-5` | Reads a table's columns + data profiles and writes its catalog entry: description, grain, concept, source type, **monitored variables (segments)**, **event-date column**, expected update rhythm, dedup rule, static/not, confidence |
| 2 | **Sentinel discovery** — checker (same job) | Right after each maker call | A *different* provider than the maker (usually Gemini `gemini-2.5-flash`; Haiku if Gemini fails) | A second opinion on only two decisions: `source_type` and `requires_dedup`. Disagreement sends the table to **Needs Review** |
| 3 | **Sentinel resolver** (`ct-sentinel-resolver`) | Daily 07:10 IST, only for Needs Review rows | Same cascade as #1 | Adjudicates the disagreement from the evidence; auto-confirms only if confidence ≥ 0.75 |
| 4 | **Dashboard — Services tab "Description"** (`ct-ui`, `_describe_errors`) | When someone opens a service's endpoint breakdown and an API has an error | OpenAI `gpt-4o-mini` | Turns a raw error log line into one plain-English sentence (≤ 15 words). Only error text is sent; results are cached in memory |
| 5 | **Alerter** (`ct-alerter`) | Hourly at :45, for each new *critical* alert | Claude `claude-haiku-5-5` | 2–3 sentence triage: likely root cause + the one remediation step (max 200 output tokens) |

### Not LLM calls (they only *touch* the providers, no tokens)
* **Providers tab** health checks: `GET /v1/models` on OpenAI / Anthropic / Gemini (is the key alive), Cohere status page. Free.
* **Cost collector**: reads *spend* from OpenAI's organization-costs API, Anthropic's cost report (needs an admin key) and similar. Free.

## 2. Secrets (Secret Manager) that feed the models

| Secret | Used by |
|---|---|
| `ct-sentinel-openai-key`, `ct-sentinel-gemini-key`, `ct-sentinel-anthropic-key` | discovery + resolver (#1–#3) |
| `ct-openai-api-key` | dashboard error summaries (#4), provider checks |
| `ct-anthropic-api-key` | alerter triage (#5), provider check |
| `ct-gemini-api-key` | provider check |

Secrets must have **no trailing newline** (it makes the Authorization header illegal and every call fails).

## 3. How the discovery prompt is built (and why it is cheap)

> **Status (2026-10-18): this section describes the optimized design that is being validated, NOT what
> is live yet.** What is live: Haiku 5.5 as the Anthropic model, thinking off, one-time classification,
> and the old (longer) prompts. The rewrite cut cost by ~83% in a 15-table test but, without enforced
> output schemas, OpenAI sometimes omitted required fields and returned objects where strings were
> expected, so it is held back until structured outputs are validated side by side with the old prompt.
> Also: the OpenAI account ran out of credits on 2026-10-18 (`credit_balance_exhausted`); until it is
> topped up the cascade falls through to Gemini, then Haiku.

* **Static text first, table data last.** The system rules + output contract (~1,600 tokens) are
  identical for every table and come *first*, so OpenAI/Gemini automatic prompt caching can reuse
  them across consecutive tables. The per-table evidence goes at the end.
* **Compact evidence.** Columns as `name:type`, JSON without whitespace, empty sections omitted.
* **Cheap checker.** The checker no longer re-writes the whole catalog entry. It gets a short
  prompt (schema + engine + samples) and returns two fields (≤ 150 output tokens).
* **No hidden "thinking" cost.** Gemini's thinking budget is set to 0 and Haiku's thinking is
  disabled — thinking tokens are billed as (expensive) output and extraction does not need them.
* **Output length is bounded** (every string ≤ 30 words) because output tokens cost 4–5× input.
* **One-time work.** A table is classified once, then skipped until its structure changes.
* **Fewer retries.** Lenient JSON parsing (code fences, trailing commas) and a repair step for
  near-miss values (`ReplacingMergeTree` as dedup method) avoid a second call or a fall-through
  to a more expensive provider.
* **Visibility.** Every call writes one log line:
  `llm usage provider=… model=… in=… out=… [cached=… thoughts=…]` — search `llm usage` in the
  job logs to see real token spend.

## 4. List prices used for estimates (USD per million tokens — verify before relying on them)

| Model | Input | Output |
|---|---|---|
| OpenAI `gpt-4o-mini` | 0.15 | 0.60 |
| Gemini `gemini-2.5-flash` | 0.30 | 2.50 (thinking tokens count as output) |
| Claude `claude-haiku-5-5` | 0.10 | 0.50 |

## 5. Changing a model

Environment variables on the job (no code change): `SENTINEL_OPENAI_MODEL`,
`SENTINEL_GEMINI_MODEL`, `SENTINEL_ANTHROPIC_MODEL`, `OPENAI_SUMMARY_MODEL` (dashboard).
The alerter's model is set in `intelligence/alerter/main.py`.
**Haiku 5.5 rejects non-default sampling (`temperature`) and has thinking on by default** —
the code sends `thinking: disabled` through `extra_body` (the pinned Anthropic SDK predates it).

## 6. Where the code is
* `sentinel/discovery/llm.py` — prompts, providers, checker, adjudicator (copied into
  `sentinel/resolver/` by `scripts/deploy.sh`).
* `ui/backend/main.py` — `_describe_errors`.
* `intelligence/alerter/main.py` — `get_ai_triage`.
