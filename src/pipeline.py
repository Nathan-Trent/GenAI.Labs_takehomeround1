from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from src.llm_client import OpenRouterLLMClient, build_default_llm_client
from src.observability import log_event, new_request_id
from src.schema import TABLE_NAME, build_schema_context
from src.types import (
    SQLValidationOutput,
    SQLExecutionOutput,
    PipelineOutput,
)


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "gaming_mental_health.sqlite"

MAX_RESULT_ROWS = 100
QUERY_TIMEOUT_S = 10.0


class SQLValidationError(Exception):
    pass


_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_WRITE_KEYWORDS_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|replace)\b",
    re.IGNORECASE,
)


class SQLValidator:
    """Two-layer validation of LLM-generated SQL.

    Layer 1 (policy): single statement, must start with SELECT/WITH, and no
    write/DDL keywords anywhere outside string literals. Keyword scan matters
    because SQLite allows e.g. `WITH x AS (...) DELETE ...`, so checking only
    the first keyword is not enough.

    Layer 2 (semantic): compile the statement via EXPLAIN on a read-only
    connection to the real database. This catches syntax errors, unknown
    columns/tables, and multi-statement injection — without executing anything.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def validate(self, sql: str | None) -> SQLValidationOutput:
        start = time.perf_counter()

        def _invalid(error: str) -> SQLValidationOutput:
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error=error,
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        if sql is None or not sql.strip():
            return _invalid("No SQL provided")

        cleaned = _COMMENT_RE.sub(" ", sql).strip().rstrip(";").strip()
        if not cleaned:
            return _invalid("SQL is empty after removing comments")

        first_word = cleaned.split(None, 1)[0].lower()
        if first_word not in ("select", "with"):
            return _invalid(
                f"Only read-only SELECT queries are allowed; statement starts with "
                f"'{first_word.upper()}'"
            )

        scan_target = _STRING_RE.sub("''", cleaned)
        match = _WRITE_KEYWORDS_RE.search(scan_target)
        if match:
            return _invalid(
                f"Only read-only SELECT queries are allowed; found forbidden "
                f"keyword '{match.group(0).upper()}'"
            )

        # An analytics query must actually read the analytics table. This
        # rejects constant-only dodges like "SELECT NULL AS placeholder" that
        # models sometimes emit instead of declining a question.
        if TABLE_NAME.lower() not in scan_target.lower():
            return _invalid(
                f"Query does not reference the {TABLE_NAME} table"
            )

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                # EXPLAIN compiles without executing the underlying query.
                # sqlite3 also rejects multiple statements here.
                conn.execute(f"EXPLAIN {cleaned}")
            finally:
                conn.close()
        except (sqlite3.Error, sqlite3.Warning) as exc:
            return _invalid(f"SQL failed to compile against the schema: {exc}")

        return SQLValidationOutput(
            is_valid=True,
            validated_sql=cleaned,
            error=None,
            timing_ms=(time.perf_counter() - start) * 1000,
        )


class SQLiteExecutor:
    """Executes validated SQL on a read-only connection with a wall-clock cap.

    Read-only mode is defense in depth: even a write statement that slipped
    past validation cannot modify the database file.

    Note: rows/row_count reflect the returned page (capped at MAX_RESULT_ROWS),
    not the full result cardinality.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def run(self, sql: str | None) -> SQLExecutionOutput:
        start = time.perf_counter()
        error = None
        rows = []
        row_count = 0

        if sql is None:
            return SQLExecutionOutput(
                rows=[],
                row_count=0,
                timing_ms=(time.perf_counter() - start) * 1000,
                error=None,
            )

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                deadline = time.monotonic() + QUERY_TIMEOUT_S
                conn.set_progress_handler(
                    lambda: 1 if time.monotonic() > deadline else 0, 100_000
                )
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql)
                rows = [dict(r) for r in cur.fetchmany(MAX_RESULT_ROWS)]
                row_count = len(rows)
            finally:
                conn.close()
        except Exception as exc:
            error = str(exc)
            if "interrupted" in error.lower():
                error = f"Query exceeded the {QUERY_TIMEOUT_S:.0f}s time limit and was cancelled."
            rows = []
            row_count = 0

        return SQLExecutionOutput(
            rows=rows,
            row_count=row_count,
            timing_ms=(time.perf_counter() - start) * 1000,
            error=error,
        )


class AnalyticsPipeline:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, llm_client: OpenRouterLLMClient | None = None) -> None:
        self.db_path = Path(db_path)
        self.llm = llm_client or build_default_llm_client()
        self.validator = SQLValidator(self.db_path)
        self.executor = SQLiteExecutor(self.db_path)
        self._schema_context = build_schema_context(self.db_path)

    @staticmethod
    def _merge_generation_outputs(first, repair):
        """Fold a repair attempt into the generation stage output.

        The type contract aggregates all generation LLM calls into one
        SQLGenerationOutput; per-call details go to intermediate_outputs.
        """
        merged_stats = dict(repair.llm_stats)
        for key in ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"):
            merged_stats[key] = first.llm_stats.get(key, 0) + repair.llm_stats.get(key, 0)
        repair.llm_stats = merged_stats
        repair.timing_ms += first.timing_ms
        repair.intermediate_outputs = first.intermediate_outputs + repair.intermediate_outputs
        return repair

    def run(self, question: str, request_id: str | None = None) -> PipelineOutput:
        start = time.perf_counter()
        request_id = request_id or new_request_id()
        log_event("pipeline.request.start", request_id=request_id, question=question[:300])

        # Stage 1: SQL Generation (schema-aware)
        sql_gen_output = self.llm.generate_sql(
            question, {"schema": self._schema_context}, request_id=request_id
        )
        generated_sql = sql_gen_output.sql
        log_event(
            "stage.sql_generation",
            request_id=request_id,
            ms=round(sql_gen_output.timing_ms, 1),
            got_sql=generated_sql is not None,
            error=sql_gen_output.error,
        )

        # Stage 2: SQL Validation, with a single repair attempt for compile
        # errors (unknown function/column, syntax). Policy violations (write
        # statements) are never retried — rejection is the correct outcome.
        validation_output = self.validator.validate(generated_sql)
        if (
            not validation_output.is_valid
            and generated_sql is not None
            and (validation_output.error or "").startswith("SQL failed to compile")
        ):
            log_event(
                "stage.sql_repair.attempt",
                request_id=request_id,
                compile_error=validation_output.error,
            )
            repair_output = self.llm.generate_sql(
                question,
                {
                    "schema": self._schema_context,
                    "previous_sql": generated_sql,
                    "repair_error": validation_output.error,
                },
                request_id=request_id,
            )
            sql_gen_output = self._merge_generation_outputs(sql_gen_output, repair_output)
            generated_sql = sql_gen_output.sql
            revalidation = self.validator.validate(generated_sql)
            revalidation.timing_ms += validation_output.timing_ms
            validation_output = revalidation

        refusal_reason = None
        if sql_gen_output.intermediate_outputs:
            refusal_reason = sql_gen_output.intermediate_outputs[-1].get("refusal_reason")
        sql = validation_output.validated_sql if validation_output.is_valid else None

        log_event(
            "stage.sql_validation",
            request_id=request_id,
            ms=round(validation_output.timing_ms, 1),
            is_valid=validation_output.is_valid,
            error=validation_output.error,
        )

        # Stage 3: SQL Execution
        execution_output = self.executor.run(sql)
        rows = execution_output.rows
        log_event(
            "stage.sql_execution",
            request_id=request_id,
            ms=round(execution_output.timing_ms, 1),
            row_count=execution_output.row_count,
            error=execution_output.error,
        )

        # Stage 4: Answer Generation (honest about upstream failures)
        answer_output = self.llm.generate_answer(
            question,
            sql,
            rows,
            execution_error=execution_output.error,
            no_sql_reason=(
                refusal_reason
                if generated_sql is None
                else (validation_output.error if not validation_output.is_valid else None)
            ),
            request_id=request_id,
        )
        log_event(
            "stage.answer_generation",
            request_id=request_id,
            ms=round(answer_output.timing_ms, 1),
            used_llm=answer_output.llm_stats.get("llm_calls", 0) > 0,
            error=answer_output.error,
        )

        # Determine status
        if sql_gen_output.error:
            status = "error"  # LLM call itself failed (network, empty content, ...)
        elif generated_sql is None:
            status = "unanswerable"  # model could not map question to the schema
        elif not validation_output.is_valid:
            status = "invalid_sql"  # generated SQL rejected by policy/schema checks
        elif execution_output.error:
            status = "error"
        else:
            status = "success"

        timings = {
            "sql_generation_ms": sql_gen_output.timing_ms,
            "sql_validation_ms": validation_output.timing_ms,
            "sql_execution_ms": execution_output.timing_ms,
            "answer_generation_ms": answer_output.timing_ms,
            "total_ms": (time.perf_counter() - start) * 1000,
        }

        total_llm_stats = {
            "llm_calls": sql_gen_output.llm_stats.get("llm_calls", 0) + answer_output.llm_stats.get("llm_calls", 0),
            "prompt_tokens": sql_gen_output.llm_stats.get("prompt_tokens", 0) + answer_output.llm_stats.get("prompt_tokens", 0),
            "completion_tokens": sql_gen_output.llm_stats.get("completion_tokens", 0) + answer_output.llm_stats.get("completion_tokens", 0),
            "total_tokens": sql_gen_output.llm_stats.get("total_tokens", 0) + answer_output.llm_stats.get("total_tokens", 0),
            "model": sql_gen_output.llm_stats.get("model", "unknown"),
            "cost_usd": round(
                sql_gen_output.llm_stats.get("cost_usd", 0.0)
                + answer_output.llm_stats.get("cost_usd", 0.0),
                8,
            ),
        }

        log_event(
            "pipeline.request.end",
            request_id=request_id,
            status=status,
            total_ms=round(timings["total_ms"], 1),
            llm_calls=total_llm_stats["llm_calls"],
            total_tokens=total_llm_stats["total_tokens"],
            cost_usd=total_llm_stats["cost_usd"],
        )

        return PipelineOutput(
            status=status,
            question=question,
            request_id=request_id,
            sql_generation=sql_gen_output,
            sql_validation=validation_output,
            sql_execution=execution_output,
            answer_generation=answer_output,
            sql=sql,
            rows=rows,
            answer=answer_output.answer,
            timings=timings,
            total_llm_stats=total_llm_stats,
        )
