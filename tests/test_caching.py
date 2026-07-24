"""Unit tests for the two-layer cache. Uses a stub LLM client — no API key."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("LOG_DISABLED", "1")

from src.pipeline import AnalyticsPipeline
from src.types import SQLGenerationOutput, AnswerGenerationOutput


class StubLLMClient:
    """Deterministic stand-in for OpenRouterLLMClient. Counts calls."""

    model = "stub-model"

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.sql_calls = 0
        self.answer_calls = 0

    def _stats(self, calls: int) -> dict:
        return {
            "llm_calls": calls,
            "prompt_tokens": 100 * calls,
            "completion_tokens": 20 * calls,
            "total_tokens": 120 * calls,
            "model": self.model,
        }

    def generate_sql(self, question, context, request_id=None):
        self.sql_calls += 1
        return SQLGenerationOutput(
            sql=self.sql, timing_ms=1.0, llm_stats=self._stats(1)
        )

    def generate_answer(self, question, sql, rows, execution_error=None,
                        no_sql_reason=None, request_id=None):
        self.answer_calls += 1
        return AnswerGenerationOutput(
            answer="Stub answer.", timing_ms=1.0, llm_stats=self._stats(1)
        )


class CachingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "test.sqlite"
        conn = sqlite3.connect(cls.db_path)
        conn.execute(
            "CREATE TABLE gaming_mental_health (age INTEGER, gender TEXT, addiction_level REAL)"
        )
        conn.executemany(
            "INSERT INTO gaming_mental_health VALUES (?, ?, ?)",
            [(20, "Male", 3.0), (30, "Female", 4.0)],
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _pipeline(self, sql: str):
        stub = StubLLMClient(sql)
        return AnalyticsPipeline(db_path=self.db_path, llm_client=stub), stub

    def test_repeat_question_uses_zero_llm_calls(self) -> None:
        pipeline, stub = self._pipeline(
            "SELECT gender, AVG(addiction_level) AS a FROM gaming_mental_health GROUP BY gender"
        )
        first = pipeline.run("addiction by gender?")
        self.assertEqual(first.status, "success")
        self.assertEqual(first.total_llm_stats["llm_calls"], 2)

        second = pipeline.run("Addiction   BY gender?")  # case/whitespace-insensitive
        self.assertEqual(second.status, "success")
        self.assertEqual(second.total_llm_stats["llm_calls"], 0)
        self.assertEqual(second.answer, first.answer)

    def test_paraphrase_same_sql_skips_answer_call(self) -> None:
        pipeline, stub = self._pipeline(
            "SELECT gender, AVG(addiction_level) AS a FROM gaming_mental_health GROUP BY gender"
        )
        pipeline.run("addiction by gender?")
        result = pipeline.run("how does addiction differ between genders?")
        # Different question -> SQL cache miss -> generation runs again...
        self.assertEqual(stub.sql_calls, 2)
        # ...but same SQL -> answer cache hit -> only 1 LLM call total.
        self.assertEqual(stub.answer_calls, 1)
        self.assertEqual(result.total_llm_stats["llm_calls"], 1)

    def test_failed_queries_are_not_cached(self) -> None:
        pipeline, stub = self._pipeline("SELECT nope FROM gaming_mental_health")
        first = pipeline.run("bad question")
        self.assertEqual(first.status, "invalid_sql")
        pipeline.run("bad question")
        # No cache: both runs attempt generation plus one compile-repair each
        # (2 runs x 2 calls). A cached failure would have made run 2 free.
        self.assertEqual(stub.sql_calls, 4)


if __name__ == "__main__":
    unittest.main()
