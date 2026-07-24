# Production Readiness Checklist

**Instructions:** Complete all sections below. Check the box when an item is implemented, and provide descriptions where requested. This checklist is a required deliverable.

---

## Approach

Describe how you approached this assignment and what key problems you identified and solved.

- [x] **System works correctly end-to-end**

**What were the main challenges you identified?**
```
I measured before changing anything, and the baseline turned out to be worse than advertised: 0% benchmark success (not the ~2900ms working baseline the README implies), because SQL generation had TWO independent fatal flaws:

1. Token starvation: gpt-5-nano is a reasoning model, so hidden reasoning tokens
   count against max_tokens. At the baseline's cap of 240, the model spent its
   entire budget reasoning and returned empty content (finish_reason="length",
   content=None) on every single call. I found this by probing the live API, not
   from the README.
2. No schema: the pipeline passed an empty context dict, so the model was asked
   to write SQL against a database it knew nothing about.

Fixing only the hinted one (schema) would have changed nothing — the model
couldn't emit any text at all.

Beyond that: the validator was a stub that approved everything, the DB was
opened read-write (a generated DELETE would really execute and commit), token
counting returned zeros, execution errors produced the dishonest answer "no
rows were returned", there was zero logging, and the shipped benchmark script
crashed on its first iteration (result["status"] on a dataclass).
```

**What was your approach?**
```
Phased, evidence-first:
1. Commit the untouched baseline, fix only the benchmark crash, and measure —
   so every later claim has a "before" number.
2. Fix correctness: schema-aware generation (schema introspected from the live
   DB), minimal reasoning effort + JSON response contract, two-layer SQL
   validation, read-only execution, real token counting.
3. Observability: structured JSON logs with request_id tracing through all
   four stages; per-call token/cost metrics.
4. Efficiency: two-layer response cache (question->SQL and SQL->answer),
   template answers for trivial results, schema prompt compaction.

Core architectural principle: the LLM never grades its own homework. Generation
translates intent faithfully (even destructive requests); deterministic code —
regex policy checks, SQLite's own compiler, a read-only connection — enforces
all policy. Prompts are hints; validators are contracts. LLM judgment is used
only where being wrong is cheap (declining a question), never where being wrong
is expensive (executing SQL).
```

---

## Observability

- [x] **Logging**
  - Description: Structured JSON logs (stdlib only — the format log aggregators
    ingest, with zero added dependencies). One event per pipeline stage plus one
    per LLM call, each carrying request_id, timings, and outcome. LOG_LEVEL /
    LOG_DISABLED env controls. Failures log at WARNING with the error and
    whether it was retried.

- [x] **Metrics**
  - Description: Per-request: per-stage timing_ms, tokens (prompt/completion/
    total), LLM call count, and real dollar cost (from the API's usage.cost)
    aggregated into total_llm_stats. The benchmark reports success rate,
    avg/p50/p95 latency, avg tokens/request, avg LLM calls/request, and total
    cost.

- [x] **Tracing**
  - Description: Every request gets a request_id (auto-generated if not
    supplied) threaded through all four stages and every LLM call. One grep of
    the request_id reconstructs the full lifecycle of a question: start ->
    generation (tokens, cost, finish_reason) -> validation verdict ->
    execution row count -> answer path -> end status.

---

## Validation & Quality Assurance

- [x] **SQL validation**
  - Description: Two layers, both deterministic code — never the LLM assessing
    itself. Layer 1 (policy): single statement, must start with SELECT/WITH, no
    write/DDL keywords anywhere outside string literals (catches `WITH x AS
    (...) DELETE`), and the query must reference the analytics table (added
    after observing the model emit `SELECT NULL AS placeholder` to dodge an
    unanswerable question — valid SQL that "succeeds" while answering nothing).
    Layer 2 (semantic): EXPLAIN-compile against the real schema on a read-only
    connection — catches unknown columns/tables, syntax errors, and
    multi-statement injection without executing anything.

- [x] **Answer quality**
  - Description: Schema-aware generation with category values and bucketing
    guidance; strict JSON output contract with fenced/prose fallback parsing;
    one-shot repair loop for compile errors only (policy violations and
    missing-concept questions are never retried — retrying "no such column"
    pressures the model to substitute a column that exists, converting refusals
    into hallucinations). Trivial single-value results get exact template
    answers (verified: the high-addiction count answer matches the ETL's own
    load-time statistics).

- [x] **Result consistency**
  - Description: temperature=0 for SQL generation; the dataset is static and
    the DB read-only, so identical SQL always yields identical rows, which is
    also the safety argument for the answer cache. The unanswerable path is
    deterministic by validation (constant-only queries rejected) rather than
    relying on the model refusing consistently.

- [x] **Error handling**
  - Description: Every failure path produces an honest answer: validation
    rejection and unanswerable questions say "I cannot answer" with the reason;
    execution failures say the query failed (the baseline claimed "no rows were
    returned" — false); LLM outages surface as status="error" after bounded
    retries. No failure path raises out of run(); the output contract is always
    honored.

---

## Maintainability

- [x] **Code organization**
  - Description: One concern per module: schema.py (introspection), llm_client.py
    (LLM calls + parsing), pipeline.py (validator, executor, orchestration,
    caching), observability.py (logging/tracing), types.py (untouched contract).

- [x] **Configuration**
  - Description: .env loaded via python-dotenv (the baseline shipped the
    dependency but never called load_dotenv — the .env file did nothing).
    OPENROUTER_API_KEY, OPENROUTER_MODEL, LOG_LEVEL, LOG_DISABLED via
    environment. Tunables (timeouts, retry counts, cache size, row caps) are
    named module constants.

- [x] **Error handling**
  - Description: Transient LLM failures retry twice with backoff; auth errors
    fail fast (retrying a 401 only delays the same failure). Query wall-clock
    timeout (10s) via SQLite progress handler. Empty-content responses from
    reasoning models raise a descriptive error naming the likely cause.

- [x] **Documentation**
  - Description: docs/FINDINGS.md is the step-by-step investigation log
    (what was discovered, in what order, with evidence). SOLUTION_NOTES.md
    covers changes, rationale, measured impact, and tradeoffs. Code comments
    explain the non-obvious "why" (e.g. why the cache is safe here, why the
    repair loop has a policy boundary).

---

## LLM Efficiency

- [x] **Token usage optimization**
  - Description: Token counting implemented from response usage (hard
    requirement — baseline reported zeros). reasoning_effort=minimal stops the
    reasoning-token burn. Compact schema prompt. Answer prompt capped at 20
    rows. Template answers for single-value results spend zero tokens. Real
    dollar cost tracked per request (~$0.0001/question cold).

- [x] **Efficient LLM requests**
  - Description: Two-layer LRU cache (256 entries, O(1) lookup, bounded so it
    cannot grow or slow down): question->SQL for exact repeats (0 LLM calls),
    and SQL->answer keyed on the generated SQL, so paraphrases that converge to
    the same query skip the answer call (1 call instead of 2). Safe precisely
    because the dataset is static and the DB read-only — cached results can
    never go stale. Only fully validated, successfully executed requests are
    cached: failures always re-attempt, so cached failures can't freeze bugs
    in place, and no LRU slot is wasted on garbage. Measured warm workload:
    0.61 LLM calls/request.

---

## Testing

- [x] **Unit tests**
  - Description: 23 unit tests requiring no API key: validator (policy,
    semantic, injection attempts, string-literal false positives), read-only
    enforcement (a DELETE physically cannot modify the DB even if validation
    were bypassed), response parsing (JSON/fenced/prose/refusal), and caching
    (hit/miss/paraphrase/failure-not-cached) via a stub LLM client.

- [x] **Integration tests**
  - Description: The 5 public tests (unmodified, all passing) exercise the
    full pipeline against the live LLM: answerable, unanswerable, destructive
    request rejection, timings, and the output contract.

- [x] **Performance tests**
  - Description: scripts/benchmark.py (crash fixed; extended to report tokens,
    LLM calls, and cost). Cold and warm numbers below.

- [x] **Edge case coverage**
  - Description: Destructive requests (rejected by validator, not by hoping
    the model refuses), unanswerable concepts (deterministic via the
    table-reference rule), CTE-wrapped writes, comment-obfuscated writes,
    multi-statement injection, write keywords inside string literals (allowed
    — not false-positived), unknown columns/tables, query timeout, empty
    results, LLM outage (bounded retry then honest error).

---

## Optional: Multi-Turn Conversation Support

**Only complete this section if you implemented the optional follow-up questions feature.**

- [ ] **Intent detection for follow-ups**
  - Description: Not implemented — see summary below.

- [ ] **Context-aware SQL generation**
  - Description: Not implemented.

- [ ] **Context persistence**
  - Description: Not implemented.

- [ ] **Ambiguity resolution**
  - Description: Not implemented.

**Approach summary:**
```
Deliberately not attempted. Within the 4-6 hour timebox I prioritized making
the required pipeline correct, safe, observable, and measured over adding an
optional feature on top of an unfinished core. The architecture leaves a clean
seam for it: generate_sql already takes a context dict, so conversation history
would slot in as an additional context key plus a rewrite step that resolves
references ("what about males?") into a self-contained question before the
existing pipeline runs unchanged.
```

---

## Production Readiness Summary

**What makes your solution production-ready?**
```
- Safety is enforced by deterministic code, not prompts: SELECT-only policy
  validation, compile checks against the real schema, and a read-only database
  connection as defense in depth (unit-tested: a DELETE cannot modify the file
  even with validation bypassed).
- Every failure mode has a designed, honest response — no path crashes out of
  run(), and no path lies about what happened.
- Resilient to transient LLM failures (bounded retry with backoff, fail-fast
  on auth errors) and runaway queries (10s wall-clock timeout).
- Fully observable: JSON logs, request_id tracing across all stages, token and
  dollar-cost metrics per request.
- Bounded resource usage everywhere: LRU caches, row caps, retry caps, one-shot
  repair. Nothing can grow or loop without limit.
```

**Key improvements over baseline:**
```
- Success rate: 0% -> 100% (the baseline never produced any SQL: reasoning-token
  starvation at max_tokens=240 plus no schema in the prompt)
- Latency: 5742ms avg -> 3857ms cold / 2037ms warm (p50 1227ms warm)
- Tokens: unmeasured (reported 0) -> measured, 951/request cold, 316 warm
- Safety: destructive SQL executed and committed -> rejected by two independent
  layers
- Honesty: execution errors reported as "no rows returned" -> every failure
  path states what actually happened
- Observability: none -> full structured logging, tracing, and cost metrics
```

**Known limitations or future work:**
```
- Answer cache returns the answer phrased for the original question's wording;
  paraphrases get a semantically correct but not re-tailored sentence. Accepted
  for ~50% token savings on paraphrases.
- Failures are never cached, so a repeatedly-asked unanswerable question pays
  the generation call each time. A short-TTL negative cache would fix this;
  skipped as marginal at this scale.
- Cache is in-process; at multi-instance scale it would externalize to Redis
  (a backend swap behind the same small class, not a redesign). The static
  dataset means no TTL/invalidation is needed — that complexity arrives only
  if the data goes live.
- Semantic question matching (embeddings) could catch paraphrases before the
  generation call; rejected as more system than the timebox justifies.
- row_count reflects the returned page (capped at 100 rows), not full result
  cardinality — documented in code.
- Multi-turn support not attempted (see optional section).
```

---

## Benchmark Results

Include your before/after benchmark results here.

**Baseline (if you measured):**
- Average latency: `5742 ms`
- p50 latency: `5782 ms`
- p95 latency: `6477 ms`
- Success rate: `0 %` (12/12 prompts failed; the model returned empty content on every call)

**Your solution:**
- Average latency: `3857 ms` cold / `2037 ms` warm (3 runs, cache active)
- p50 latency: `4218 ms` cold / `1227 ms` warm
- p95 latency: `5354 ms` cold / `4486 ms` warm
- Success rate: `100 %` (cold and warm)

**LLM efficiency:**
- Average tokens per request: `951` cold / `316` warm
- Average LLM calls per request: `1.83` cold / `0.61` warm
  (measured cost: ~$0.0013 per 12-question cold run)

---

**Completed by:** Nathan Ogogo
**Date:** 2026-07-24
**Time spent:** 3-4 hours — the majority on architecture, investigation, and
measuring the baseline before building; implementation itself was the smaller
share once the root causes were understood.
