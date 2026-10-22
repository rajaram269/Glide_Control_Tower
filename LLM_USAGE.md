# LLM usage in Control Tower — which model does what

Last updated: 2026-10-18. This file lists **every** LLM call the project makes. If you add or
change one, update this file in the same commit.

* **No Sonnet and no Opus anywhere.** Anthropic is used only as `claude-haiku-5-5` (the cheapest current Claude).
* **OpenAI uses `gpt-5-mini`** (replaces `gpt-4o-mini`, which OpenAI retires in January 2027).
* Fallback order for the catalog work is always: **OpenAI `gpt-5-mini` → Google `gemini-2.5-flash` → Anthropic `claude-haiku-5-5`**.

## 1. Which model is used for which task

| # | Task (where it runs) | 1st choice | If that fails | Then | What it produces |
|---|---|---|---|---|---|
| 1 | **Catalog a table** — "maker" (Sentinel discovery job) | OpenAI `gpt-5-mini` | Gemini `gemini-2.5-flash` | Claude `claude-haiku-5-5` | The table's catalog entry: description, grain, concept, source type, **monitored variables (segments)**, **event-date column**, expected update rhythm, dedup rule, static or not, confidence |
| 2 | **Second opinion** — "checker" (same job) | The *next provider after the maker* — normally Gemini `gemini-2.5-flash` | OpenAI `gpt-5-mini` or Claude `claude-haiku-5-5` (whichever is left) | — | Only two answers: `source_type` and `requires_dedup`. A disagreement sends the table to **Needs Review** |
| 3 | **Settle a disagreement** — "adjudicator" (Sentinel resolver job) | OpenAI `gpt-5-mini` | Gemini `gemini-2.5-flash` | Claude `claude-haiku-5-5` | A decision on source type / dedup / concept with a confidence; auto-confirmed only when confidence ≥ 0.75 |
| 4 | **Explain an API error in plain English** (dashboard, Services tab → endpoint "Description") | OpenAI `gpt-5-mini` | *none* — shows the raw error line | — | One sentence (≤ 15 words) per failing API. Only the error text is sent; cached in memory |
| 5 | **Triage a critical alert** (Alerter job, in the alert email) | Claude `claude-haiku-5-5` | *none* — email is sent without the AI paragraph | — | 2–3 sentences: likely cause + the one fix to try (max 200 output tokens) |

### Calls that touch the providers but are **not** model calls (no tokens, no cost)
* **Providers tab** health checks — `GET /v1/models` on OpenAI, Anthropic and Gemini (is the key alive); Cohere status page.
* **Cost collector** — reads *spend* from OpenAI's organization-costs API and Anthropic's cost report (needs an admin key).

## 2. When they run

| Task | Job | Schedule (IST) | Notes |
|---|---|---|---|
| 1 + 2 | `ct-sentinel-discovery` | **Daily 06:45** | Each table is classified **once**; re-done only if its columns change, `CLASSIFIER_VERSION` is bumped, or it is new. Max 80 tables per run; human-pinned tables are skipped |
| 3 | `ct-sentinel-resolver` | **Daily 07:10** | Only touches rows in Needs Review |
| 4 | `ct-ui` (service) | When someone opens a service's endpoint breakdown | Only if an API has errors |
| 5 | `ct-alerter` | **Hourly at :45** | Only for new *critical* alerts |

Full schedule of every Control Tower job: see [SCHEDULES.md](SCHEDULES.md).

## 3. Models, prices and special settings

| Model | Used for | Input $/M tokens | Output $/M tokens | Special handling in the code |
|---|---|---|---|---|
| OpenAI `gpt-5-mini` | tasks 1–4 (first choice) | 0.25 | 2.00 | **Reasoning model**: rejects `temperature`; output is capped with `max_completion_tokens`; its hidden *reasoning tokens are billed as output*, so `reasoning_effort` is set to the lowest level (`minimal`) |
| Google `gemini-2.5-flash` | tasks 1–3 (fallback / checker) | 0.30 | 2.50 | Also "thinks" by default and bills thinking as output — the optimized design sets the thinking budget to 0 |
| Anthropic `claude-haiku-5-5` | tasks 1–3 (last fallback), task 5 | 0.10 | 0.50 | Rejects non-default `temperature`; thinking is switched **off** (`thinking: disabled`, sent via `extra_body` because the pinned SDK predates it) |

*Prices are list prices recalled when this was written — verify on each provider's pricing page before relying on them.*

**Cost effect of 4o-mini → 5-mini:** input is about 1.7× dearer and output about 3.3× dearer
(`gpt-4o-mini` was $0.15 / $0.60). For one table that is roughly $0.002 → $0.006 on the OpenAI
maker call. Output tokens dominate, which is why the prompt-optimization work (below) matters more now.

Newer models exist on the account (for example `gpt-5.4-mini`); switch by setting `SENTINEL_OPENAI_MODEL`
(discovery/resolver) or `OPENAI_SUMMARY_MODEL` (dashboard) — no code change needed.

## 4. Secrets that feed the models

| Secret | Used by |
|---|---|
| `ct-sentinel-openai-key`, `ct-sentinel-gemini-key`, `ct-sentinel-anthropic-key` | discovery + resolver (tasks 1–3) |
| `ct-openai-api-key` | dashboard error explanations (task 4) and provider checks |
| `ct-anthropic-api-key` | alerter triage (task 5) and provider check |
| `ct-gemini-api-key` | provider check |

A secret must have **no trailing newline** (it makes the Authorization header invalid and every call fails).

## 5. Status as of 2026-10-18 (read this before relying on the numbers)

* **OpenAI credits are exhausted** (`credit_balance_exhausted`, 2026-10-18). Until it is recharged, tasks 1–3 fall through to Gemini then Haiku, and task 4 shows raw error text. The switch to `gpt-5-mini` is deployed but **not yet tested with real calls** — test after the recharge (planned Monday).
* **Prompt optimization is built but NOT live.** A shorter prompt, a compact checker, "static text first" ordering and thinking-off cut cost by about 83% in a 15-table test, but without enforced output schemas OpenAI sometimes omitted required fields and returned objects where text was expected. The fix (enforced JSON schemas / structured outputs) is written and is being validated side by side with the current prompt. Until then production uses the current, longer prompts.
* **Visibility:** once the optimized version ships, every call logs one line `llm usage provider=… model=… in=… out=…` so real spend can be read from the job logs.

## 6. Where the code is
* `sentinel/discovery/llm.py` — prompts, providers, checker, adjudicator (copied into `sentinel/resolver/` by `scripts/deploy.sh`).
* `ui/backend/main.py` — `_describe_errors` (task 4).
* `intelligence/alerter/main.py` — `get_ai_triage` (task 5).
