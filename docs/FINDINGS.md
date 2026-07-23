# Investigation Findings — Baseline Analysis

Working log of what was discovered before changing any pipeline code, in the
order it was discovered. Source material for `SOLUTION_NOTES.md`.

---

## Phase 1 — Understanding the system (code reading, no execution)

### What the system is

A natural-language analytics pipeline over a single SQLite table
(`gaming_mental_health`, survey data). A user question flows through four
stages:

1. **SQL Generation** (LLM call #1) — English question → SQL query
2. **SQL Validation** (rules, no LLM) — approve/reject the generated SQL
3. **SQL Execution** (SQLite, no LLM) — run approved SQL, get raw rows
4. **Answer Generation** (LLM call #2) — question + rows → plain-English answer

`src/types.py` defines the grading contract (`PipelineOutput` + four stage
outputs); `src/pipeline.py` orchestrates; `src/llm_client.py` holds both LLM
calls (OpenRouter, default model `openai/gpt-5-nano`).

### Deliberate gaps found by reading the code

| # | Gap | Where | Consequence |
|---|-----|-------|-------------|
| 1 | **No schema in the prompt.** `generate_sql(question, {})` passes an empty context dict; the model is never told the table or column names. | `pipeline.py:95`, `llm_client.py:65-71` | Model must guess column names → broken or fictional SQL |
| 2 | **Validator is a stub.** Approves everything except `None`; body is a TODO. | `pipeline.py:23-44` | Destructive SQL (DELETE/DROP) would pass validation |
| 3 | **Token counting not implemented.** `_stats` initialized but never incremented; `pop_stats()` always returns zeros. | `llm_client.py:36-37, 149-152` | Efficiency metrics permanently report 0 (hard requirement unmet) |

### Additional weaknesses found by reading

- **DB opened read-write and commits on success** (`pipeline.py:66`): combined
  with gap #2, a generated DELETE would really execute and persist. No query
  timeout, no LIMIT enforcement.
- **Fragile SQL extraction** (`llm_client.py:48-63`): accepts either a JSON
  object with a `"sql"` key or "everything after the first `select `" —
  markdown fences and trailing prose pass straight through to the executor.
  Accidental quirk: a plain `DELETE ...` response contains no "select", so it
  is dropped by luck — but the same DELETE wrapped in JSON passes. The system
  is *accidentally* safe, not *designed* safe.
- **Dishonest failure answer**: if execution errors, rows is `[]`, so stage 4
  answers "Query executed, but no rows were returned" — false; it failed.
- **`row_count` is the truncated fetch count** (`fetchmany(100)`), not true
  result cardinality.
- **`request_id` accepted but unused** — no logging, metrics, or tracing
  anywhere in the codebase (zero observability).
- **`python-dotenv` is in requirements.txt but `load_dotenv()` is never
  called** — the `.env` file does nothing as shipped.
- **`scripts/benchmark.py` crashes on first run**: `result["status"]`
  subscripts a dataclass (TypeError). The README's "reference metrics" could
  not have been produced by this script as-is.
- **No retries/timeouts/backoff** on LLM calls; one transient API failure =
  failed request.
- **`answer` prompt embeds up to 30 raw JSON rows** (39 columns each) —
  token-heavy for typically small aggregate results.

---

## Phase 2 — Baseline reality check (measured, nothing changed except the benchmark one-liner)

### Finding 1 (headline): the baseline generates no SQL at all — ever

A probe call replicating stage 1 exactly (same model, prompts,
`max_tokens=240`) returned:

```
finish_reason: "length"
content: None
usage: completion_tokens=192, reasoning_tokens=192
```

`gpt-5-nano` is a **reasoning model**: internal reasoning tokens count against
`max_tokens`. With a 240-token cap the model spends its entire budget thinking
and emits **zero visible text**. Every question therefore degrades to
status="unanswerable" with the canned "cannot answer" message.

**Implication: stage 1 has two independent fatal flaws — no schema AND no room
to answer. Fixing only the schema would fix nothing.**

### Finding 2: where token usage actually lives

The OpenRouter SDK response (`ChatResult`) carries everything needed:

```
res.usage.prompt_tokens
res.usage.completion_tokens          # includes reasoning tokens
res.usage.total_tokens
res.usage.completion_tokens_details.reasoning_tokens
res.usage.cost                       # actual dollar cost
```

Token counting (hard requirement) is a straightforward wire-up.

### Finding 3: measured baseline numbers ("before" evidence)

| Metric | Baseline |
|---|---|
| Benchmark success rate | **0%** (12/12 prompts failed) |
| Average latency | 5,742 ms |
| p50 latency | 5,782 ms |
| p95 latency | 6,477 ms |
| Public tests | 3 of 5 pass |
| Tokens reported | 0 (counting unimplemented); actual ≈260 wasted/question |

Failing tests: `test_answerable_prompt_returns_sql_and_answer`
(status "unanswerable" ≠ "success") and `test_invalid_sql_is_rejected`
(status "unanswerable" ≠ "invalid_sql"). Both are downstream symptoms of
Finding 1.

README reference metrics (~2,900 ms avg, ~600 tokens/request) do not match
measured reality (~5,700 ms, 0% success) — further evidence the shipped
baseline was never run end-to-end in this configuration.

### Finding 4: the data

- **1,000,000 rows**, not 10M (the CSV filename says 10M; the file contains
  1M rows and all were loaded — verified against CSV line count).
- 39 columns; key ones: `age` INTEGER (13–59), `gender` TEXT
  (Male 48.1% / Female 48.0% / Other 4.0%), `addiction_level` REAL,
  `anxiety_score` REAL.
- **No `age_group` column exists.** Questions about "age groups" require the
  SQL itself to bucket raw `age` (e.g. CASE expressions). The schema prompt
  must teach the model this.
- DB build script's "failure" on Windows is cosmetic: it loaded all rows,
  then crashed printing a ✓ character the console encoding can't render.

### Fix applied in this phase

- `scripts/benchmark.py`: `result["status"]` → `result.status` (one line;
  benchmark is not a protected file). Without it no measurement is possible.

---

## Prioritized fix plan derived from these findings

1. **Stage 1 — make the model able to speak, then able to aim**: raise/rethink
   the token budget for a reasoning model, put the real schema (table, columns,
   types, category values, age-bucketing guidance) into the prompt, demand a
   strict output format for reliable extraction.
2. **Stage 2 — real validation**: single-statement, SELECT-only allowlist
   (makes the delete test pass by design); schema-aware column/table checks
   (makes unanswerable questions fail fast without an LLM error round-trip).
3. **Stage 3 — seatbelts**: open SQLite read-only (defense in depth), enforce
   LIMIT, add a query timeout.
4. **Token counting** from `res.usage` (hard requirement).
5. **Honest failure answers** (no "no rows returned" when the query errored).
6. **Observability**: structured logs with request_id through all stages,
   per-stage timings (already present), token/cost metrics.
7. **Resilience**: timeout + retry with backoff on LLM calls.
8. **Efficiency**: trim answer-prompt row payload, skip LLM call #2 for
   trivial results, benchmark before/after.
