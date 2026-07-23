from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from src.types import SQLGenerationOutput, AnswerGenerationOutput

DEFAULT_MODEL = "openai/gpt-5-nano"

# gpt-5-nano is a reasoning model: hidden reasoning tokens count against
# max_tokens. The baseline's cap of 240 was fully consumed by reasoning,
# so the model never emitted any visible text (measured: finish_reason=
# "length", content=None). We request minimal reasoning effort and keep a
# generous cap as a safety margin.
SQL_GEN_MAX_TOKENS = 2000
ANSWER_MAX_TOKENS = 1200
LLM_TIMEOUT_MS = 45_000

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|```$", re.MULTILINE)


class OpenRouterLLMClient:
    """LLM client using the OpenRouter SDK for chat completions."""

    provider_name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
        self._stats = self._empty_stats()

    @staticmethod
    def _empty_stats() -> dict[str, Any]:
        return {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _chat(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_response: bool = False,
    ) -> str:
        res = self._client.chat.send(
            messages=messages,
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort="minimal",
            response_format={"type": "json_object"} if json_response else None,
            timeout_ms=LLM_TIMEOUT_MS,
            stream=False,
        )

        # Token accounting (required for efficiency evaluation).
        usage = getattr(res, "usage", None)
        self._stats["llm_calls"] += 1
        if usage is not None:
            self._stats["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            self._stats["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
            self._stats["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)

        choices = getattr(res, "choices", None) or []
        if not choices:
            raise RuntimeError("OpenRouter response contained no choices.")
        finish_reason = getattr(choices[0], "finish_reason", None)
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(
                f"OpenRouter returned empty content (finish_reason={finish_reason!r}). "
                "For reasoning models this usually means max_tokens was consumed "
                "by hidden reasoning."
            )
        return content.strip()

    @staticmethod
    def _extract_sql(text: str) -> tuple[str | None, str | None]:
        """Parse the model response. Returns (sql, refusal_reason).

        Primary contract is a JSON object {"sql": "..."} or
        {"sql": null, "reason": "..."}; fenced/prose fallbacks keep us robust
        to models that ignore response_format.
        """
        cleaned = _FENCE_RE.sub("", text.strip()).strip()

        if cleaned.startswith("{"):
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and "sql" in parsed:
                sql = parsed.get("sql")
                if isinstance(sql, str) and sql.strip():
                    return sql.strip(), None
                reason = parsed.get("reason")
                return None, reason if isinstance(reason, str) else "Model declined to generate SQL."

        # Fallback: take from the first SELECT/WITH keyword onward.
        match = re.search(r"\b(select|with)\b", cleaned, re.IGNORECASE)
        if match:
            return cleaned[match.start():].strip(), None
        return None, None

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput:
        schema = context.get("schema", "") if isinstance(context, dict) else ""
        system_prompt = (
            "You translate natural-language questions into a single SQLite query "
            "for the table described below.\n\n"
            + schema
            + "\n\nRules:\n"
            "- Translate the request faithfully into SQL, even if it asks to modify "
            "data; a downstream validator enforces read-only policy.\n"
            "- If the question asks about data that does not exist in this table "
            "(a concept with no matching column), you MUST return "
            '{"sql": null, "reason": "<why>"}. Never substitute a different '
            "column, and never write a SQL query that returns an explanation "
            "string — the refusal belongs in the JSON, not in SQL.\n"
            "- For \"age group\" questions, bucket the raw age column with CASE "
            "(e.g. '13-19', '20-29', '30-39', '40-49', '50-59') unless the user "
            "specifies buckets.\n"
            "- Prefer aggregate queries; add LIMIT 100 to row-level queries.\n"
            "- SQLite only: use built-in functions (COUNT, AVG, SUM, MIN, MAX, "
            "ROUND, CASE); there is NO STDDEV, VARIANCE, MEDIAN or PERCENTILE.\n"
            "Respond with JSON only: "
            '{"sql": "<query>"} or {"sql": null, "reason": "<why>"}'
        )

        repair_error = context.get("repair_error") if isinstance(context, dict) else None
        user_content = question
        if repair_error:
            user_content = (
                f"{question}\n\n"
                f"Your previous query failed validation:\n{context.get('previous_sql')}\n"
                f"Error: {repair_error}\n"
                "Return a corrected query. If the error is that a column for the "
                "asked concept does not exist, return {\"sql\": null, \"reason\": ...} "
                "instead of substituting a different column."
            )

        start = time.perf_counter()
        error = None
        sql = None
        refusal_reason = None
        raw_text = None

        try:
            raw_text = self._chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.0,
                max_tokens=SQL_GEN_MAX_TOKENS,
                json_response=True,
            )
            sql, refusal_reason = self._extract_sql(raw_text)
        except Exception as exc:
            error = str(exc)

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return SQLGenerationOutput(
            sql=sql,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            intermediate_outputs=[
                {
                    "raw_response": (raw_text[:2000] if raw_text else None),
                    "refusal_reason": refusal_reason,
                }
            ],
            error=error,
        )

    def generate_answer(
        self,
        question: str,
        sql: str | None,
        rows: list[dict[str, Any]],
        execution_error: str | None = None,
        no_sql_reason: str | None = None,
    ) -> AnswerGenerationOutput:
        """Produce the user-facing answer.

        Non-LLM paths (no SQL, failed execution, empty or trivial results) are
        answered honestly with templates — no tokens spent, no invented data.
        """
        no_llm_stats = {**self._empty_stats(), "model": self.model}

        if not sql:
            reason = f" ({no_sql_reason})" if no_sql_reason else ""
            return AnswerGenerationOutput(
                answer=(
                    "I cannot answer this with the available table and schema"
                    + reason
                    + ". Please rephrase using known survey fields."
                ),
                timing_ms=0.0,
                llm_stats=no_llm_stats,
                error=None,
            )

        if execution_error:
            return AnswerGenerationOutput(
                answer=(
                    "The query could not be executed against the database, so I "
                    f"cannot answer this question. Error: {execution_error}"
                ),
                timing_ms=0.0,
                llm_stats=no_llm_stats,
                error=None,
            )

        if not rows:
            return AnswerGenerationOutput(
                answer="The query executed successfully but matched no rows in the dataset.",
                timing_ms=0.0,
                llm_stats=no_llm_stats,
                error=None,
            )

        # Trivial result (single value): a template is exact and costs nothing.
        if len(rows) == 1 and len(rows[0]) == 1:
            ((col, val),) = rows[0].items()
            formatted = f"{val:,}" if isinstance(val, int) else (f"{val:,.2f}" if isinstance(val, float) else str(val))
            return AnswerGenerationOutput(
                answer=f"The result is {formatted} ({col.replace('_', ' ')}).",
                timing_ms=0.0,
                llm_stats=no_llm_stats,
                error=None,
            )

        system_prompt = (
            "You are a concise analytics assistant. "
            "Use only the provided SQL results. Do not invent data."
        )
        user_prompt = (
            f"Question:\n{question}\n\nSQL:\n{sql}\n\n"
            f"Rows (JSON):\n{json.dumps(rows[:20], ensure_ascii=True, default=str)}\n\n"
            "Write a concise answer in plain English."
        )

        start = time.perf_counter()
        error = None
        answer = ""

        try:
            answer = self._chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=ANSWER_MAX_TOKENS,
            )
        except Exception as exc:
            error = str(exc)
            answer = (
                "The query succeeded but the answer could not be generated. "
                f"Error: {error}"
            )

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            error=error,
        )

    def pop_stats(self) -> dict[str, Any]:
        out = dict(self._stats or {})
        self._stats = self._empty_stats()
        return out


def build_default_llm_client() -> OpenRouterLLMClient:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    return OpenRouterLLMClient(api_key=api_key)
