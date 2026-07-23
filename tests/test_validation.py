"""Unit tests for SQL validation and response parsing. No API key needed."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.llm_client import OpenRouterLLMClient
from src.pipeline import SQLValidator, SQLiteExecutor


class SQLValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "test.sqlite"
        conn = sqlite3.connect(cls.db_path)
        conn.execute(
            "CREATE TABLE gaming_mental_health ("
            "age INTEGER, gender TEXT, addiction_level REAL, anxiety_score REAL)"
        )
        conn.execute(
            "INSERT INTO gaming_mental_health VALUES (25, 'Male', 3.2, 40.5)"
        )
        conn.commit()
        conn.close()
        cls.validator = SQLValidator(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_valid_select_passes(self) -> None:
        out = self.validator.validate(
            "SELECT gender, AVG(addiction_level) FROM gaming_mental_health GROUP BY gender"
        )
        self.assertTrue(out.is_valid)
        self.assertIsNone(out.error)

    def test_trailing_semicolon_is_stripped(self) -> None:
        out = self.validator.validate("SELECT COUNT(*) FROM gaming_mental_health;")
        self.assertTrue(out.is_valid)
        self.assertFalse(out.validated_sql.endswith(";"))

    def test_none_rejected(self) -> None:
        out = self.validator.validate(None)
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_delete_rejected(self) -> None:
        out = self.validator.validate("DELETE FROM gaming_mental_health")
        self.assertFalse(out.is_valid)
        self.assertIn("read-only", out.error)

    def test_drop_rejected(self) -> None:
        out = self.validator.validate("DROP TABLE gaming_mental_health")
        self.assertFalse(out.is_valid)

    def test_cte_hiding_delete_rejected(self) -> None:
        out = self.validator.validate(
            "WITH x AS (SELECT 1) DELETE FROM gaming_mental_health"
        )
        self.assertFalse(out.is_valid)
        self.assertIn("DELETE", out.error)

    def test_comment_obfuscated_write_rejected(self) -> None:
        out = self.validator.validate("SELECT 1 /* hide */; DROP TABLE gaming_mental_health")
        self.assertFalse(out.is_valid)

    def test_multi_statement_rejected(self) -> None:
        out = self.validator.validate(
            "SELECT 1; SELECT 2"
        )
        self.assertFalse(out.is_valid)

    def test_unknown_column_rejected(self) -> None:
        out = self.validator.validate(
            "SELECT zodiac_sign FROM gaming_mental_health"
        )
        self.assertFalse(out.is_valid)
        self.assertIn("zodiac_sign", out.error)

    def test_unknown_table_rejected(self) -> None:
        out = self.validator.validate("SELECT * FROM users")
        self.assertFalse(out.is_valid)

    def test_constant_only_query_rejected(self) -> None:
        out = self.validator.validate("SELECT NULL AS placeholder")
        self.assertFalse(out.is_valid)
        self.assertIn("does not reference", out.error)

    def test_write_word_inside_string_literal_allowed(self) -> None:
        out = self.validator.validate(
            "SELECT COUNT(*) FROM gaming_mental_health WHERE gender = 'delete'"
        )
        self.assertTrue(out.is_valid, out.error)


class ReadOnlyExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "test.sqlite"
        conn = sqlite3.connect(cls.db_path)
        conn.execute("CREATE TABLE gaming_mental_health (age INTEGER)")
        conn.execute("INSERT INTO gaming_mental_health VALUES (25)")
        conn.commit()
        conn.close()
        cls.executor = SQLiteExecutor(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_select_returns_rows(self) -> None:
        out = self.executor.run("SELECT age FROM gaming_mental_health")
        self.assertIsNone(out.error)
        self.assertEqual(out.rows, [{"age": 25}])

    def test_write_fails_on_readonly_connection(self) -> None:
        """Defense in depth: even if validation were bypassed, writes fail."""
        out = self.executor.run("DELETE FROM gaming_mental_health")
        self.assertIsNotNone(out.error)
        # Data untouched
        check = self.executor.run("SELECT COUNT(*) AS n FROM gaming_mental_health")
        self.assertEqual(check.rows[0]["n"], 1)


class ExtractSQLTests(unittest.TestCase):
    extract = staticmethod(OpenRouterLLMClient._extract_sql)

    def test_json_contract(self) -> None:
        sql, reason = self.extract('{"sql": "SELECT 1"}')
        self.assertEqual(sql, "SELECT 1")
        self.assertIsNone(reason)

    def test_json_null_with_reason(self) -> None:
        sql, reason = self.extract('{"sql": null, "reason": "no zodiac column"}')
        self.assertIsNone(sql)
        self.assertEqual(reason, "no zodiac column")

    def test_fenced_json(self) -> None:
        sql, _ = self.extract('```json\n{"sql": "SELECT 2"}\n```')
        self.assertEqual(sql, "SELECT 2")

    def test_fenced_plain_sql(self) -> None:
        sql, _ = self.extract("```sql\nSELECT age FROM t\n```")
        self.assertEqual(sql, "SELECT age FROM t")

    def test_prose_with_sql(self) -> None:
        sql, _ = self.extract("Here is the query: SELECT 3")
        self.assertEqual(sql, "SELECT 3")

    def test_no_sql_at_all(self) -> None:
        sql, reason = self.extract("I refuse.")
        self.assertIsNone(sql)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
