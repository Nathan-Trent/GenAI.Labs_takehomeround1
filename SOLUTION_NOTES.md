# Solution Notes

## TL;DR

The baseline pipeline had a 0% success rate — it never produced a single SQL
query. Root cause was found by probing the live API, not by reading the code:
`gpt-5-nano` is a reasoning model whose hidden reasoning tokens count against
`max_tokens`, and the baseline's cap of 240 was fully consumed before any
visible output. Combined with an empty schema context, stage 1 was doubly dead.

After the fixes: **100% benchmark success, 33% lower cold latency, 65% lower
warm latency, and measured token/cost accounting** (951 tokens/request cold,
316 warm, ~$0.0001/question).

The load-bearing design principle throughout: **the LLM never grades its own
homework.** Prompts are hints; deterministic validators are contracts. LLM
judgment is used only where being wrong is cheap; only code decides where
being wrong is expensive.

## What I changed and why

### 1. Measured first (evidence: `docs/FINDINGS.md`)

Committed the untouched baseline, fixed only the benchmark's crash
(`result["status"]` subscripting a dataclass — the shipped benchmark could
never have run), and recorded real numbers. The README's reference metrics
(~2900ms, working pipeline) turned out to be unreproducible: the actual
baseline was 5742ms at 0% success. Every claim below has a before/after number
because of this step.

### 2. Made stage 1 able to speak, then able to aim

Two independent fatal flaws, found in this order:

- **Token starvation** (found via API probe): the response came back
  `finish_reason="length"`, `content=None`, all 192 completion tokens spent on
  reasoning. Fix: `reasoning_effort="minimal"` + `response_format=json_object`
  + a realistic cap. Result: ~60-token responses, clean JSON.
- **No schema** (the README-hinted gap): the pipeline passed `{}` as context.
  Fix: `src/schema.py` introspects the real database (columns, types,
  categorical values, age range) and injects it into the prompt. Introspected
  rather than hard-coded so it cannot drift from the actual schema.

The prompt also teaches SQLite's dialect limits (no STDDEV/MEDIAN — a real
failure caught in benchmarking) and that "age groups" must be bucketed with
CASE because no age_group column exists.

### 3. Validation as the enforcement point (not the prompt)

The generation prompt deliberately translates *any* request into SQL — even
"delete all rows". Policy lives in deterministic code, in two layers:

1. **Policy scan:** single statement, SELECT/WITH only, no write/DDL keywords
   outside string literals (catches `WITH x AS (...) DELETE`), and the query
   must reference the analytics table.
2. **Compile check:** `EXPLAIN` against the real schema on a read-only
   connection — unknown columns, syntax errors, and multi-statement injection
   all fail here without executing anything.

Why not just prompt the model to refuse destructive requests? Because a prompt
is a suggestion to a probabilistic system, and a crafted input can talk a model
out of its instructions. A regex cannot be sweet-talked; SQLite's compiler
cannot be persuaded that `zodiac_sign` exists. Deterministic validation is
immune to prompt injection by construction.

**Defense in depth:** the executor also opens the database read-only, so even
a write that somehow passed validation physically cannot modify the file.
There's a unit test that bypasses validation and proves the DELETE fails and
the data survives.

**The model will cheat — validate semantics, not just syntax.** During testing,
asked about zodiac signs (which don't exist in the data), the model sometimes
emitted `SELECT NULL AS placeholder` — valid SQL that "succeeds" while
answering nothing. Prompt tightening left it flaky at roughly 1-in-3; the
validator rule "must reference the analytics table" made it deterministic.
This is the prompts-vs-validators principle proven by experiment.

### 4. Repair loop with a policy boundary

If generated SQL fails to *compile* (unknown function, syntax), the error is
fed back for exactly one retry — this is what fixed the STDDEV failure.

But two classes of failure are deliberately never retried:

- **Policy violations** (writes): rejection is the correct final outcome.
- **Missing-concept errors** ("no such column: zodiac_sign"): retrying
  pressures the model to substitute a column that *does* exist — we observed
  exactly this, a repair attempt converting an unanswerable question into
  confident garbage. A retry loop without a policy boundary converts refusals
  into hallucinations. Retry only errors for which a correct answer exists.

### 5. Honest answers on every path

The baseline answered "Query executed, but no rows were returned" when the
query had actually *failed*. Now: validation rejections and unanswerable
questions say why they can't be answered; execution failures say the query
failed and how; LLM outages surface as errors after bounded retries. In an
analytics tool a confident wrong answer is worse than an error, because
someone makes a decision with it.

Single-value results (counts, averages) are answered by template with zero
LLM calls — exact, instant, free. Sanity check: the "high addiction (>=5)"
count answer, 147,802, matches the ETL script's independent load-time
statistics.

### 6. Observability (stdlib-only)

JSON-lines structured logs — the format log aggregators ingest, at zero added
dependencies. Every request gets a `request_id` threaded through all four
stages and every LLM call; one grep reconstructs a question's full lifecycle
including per-stage timings, token counts, dollar cost (from the API's usage
data), validation verdicts, and cache hits. Transient LLM failures retry twice
with backoff and log at WARNING; auth errors fail fast because retrying a 401
only delays the same failure.

### 7. Two-layer response cache

- **question -> SQL** (normalized exact match): repeat questions skip the
  generation call entirely.
- **SQL -> answer** (keyed on the generated SQL): differently-phrased
  questions that converge to the same query skip the answer call. The SQL is
  effectively a canonical form of the question's intent — paraphrases can't be
  matched at the question level but converge at the SQL level.

Safety argument: the dataset is static and the DB read-only, so identical SQL
returns identical rows forever — a cached answer can never go stale. This
aggressive caching would be wrong on a live database; here it's free.

Discipline: bounded LRU (256 entries, O(1) hash lookup — cannot grow or slow
down), and **only fully validated, successfully executed requests are
cached**. Failures always re-attempt: caching them would waste LRU slots and,
worse, freeze bugs in place — a cached failure would keep failing even after
the prompt or validator improved. Successes are safe to freeze; failures must
stay retryable.

## Measured impact

| Metric | Baseline | After (cold) | After (warm) |
|---|---|---|---|
| Success rate | **0%** | **100%** | **100%** |
| Avg latency | 5742 ms | 3857 ms | 2037 ms |
| p50 latency | 5782 ms | 4218 ms | 1227 ms |
| p95 latency | 6477 ms | 5354 ms | 4486 ms |
| Tokens/request | 0 reported (~260 wasted) | 951 | 316 |
| LLM calls/request | unmeasured | 1.83 | 0.61 |
| Cost/12 questions | unmeasured | ~$0.0013 | ~$0.0000 marginal |

(Cold = every question seen for the first time; warm = 3 benchmark repetitions
with cache active. Public tests: 5/5. Unit tests: 23, no API key required.)

## Tradeoffs and next steps

- **Cached answers aren't re-tailored:** a paraphrase gets the answer phrased
  for the original wording — semantically correct, not custom-phrased. Accepted
  for ~50% token savings on paraphrases.
- **No negative cache:** unanswerable questions pay the generation call on
  every ask. A short-TTL negative cache is the fix; marginal at this scale.
- **In-process cache:** at multi-instance scale this externalizes to Redis —
  a backend swap behind the same small class, not a redesign. TTL/invalidation
  complexity is deliberately absent and only becomes necessary if the dataset
  goes live.
- **Semantic question matching** (embeddings) could catch paraphrases before
  the generation call; rejected as more system than the timebox justifies.
- **Multi-turn support** not attempted: I prioritized a correct, safe,
  measured core over an optional feature on top of an unfinished one. The
  seam exists — `generate_sql` already takes a context dict; history would
  slot in as a context key plus a question-rewrite step, leaving the pipeline
  unchanged.
- **Model portability:** tuned against the default `openai/gpt-5-nano`;
  `OPENROUTER_MODEL` is configurable but other models' quirks (reasoning
  budgets, JSON compliance) would need the same empirical verification pass.
