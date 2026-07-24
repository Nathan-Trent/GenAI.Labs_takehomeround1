"""Schema introspection: builds the table description given to the LLM.

The baseline passed an empty context dict to SQL generation, so the model had
to guess table and column names. This module reads the real schema from the
SQLite database once and renders it as a compact prompt block.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

TABLE_NAME = "gaming_mental_health"

# TEXT columns with at most this many distinct values are treated as
# categorical and their values are listed in the prompt.
_CATEGORICAL_MAX = 12

_cache: dict[str, str] = {}


def build_schema_context(db_path: str | Path) -> str:
    """Return a prompt-ready description of the table, cached per db path."""
    key = str(Path(db_path).resolve())
    if key in _cache:
        return _cache[key]

    conn = sqlite3.connect(f"file:{key}?mode=ro", uri=True)
    try:
        cols = conn.execute(f'PRAGMA table_info("{TABLE_NAME}")').fetchall()
        if not cols:
            raise RuntimeError(f"Table '{TABLE_NAME}' not found in {db_path}")

        parts = []
        for _cid, name, ctype, *_rest in cols:
            note = ""
            if ctype == "TEXT":
                vals = [
                    r[0]
                    for r in conn.execute(
                        f'SELECT DISTINCT "{name}" FROM "{TABLE_NAME}" '
                        f"LIMIT {_CATEGORICAL_MAX + 1}"
                    )
                ]
                if len(vals) <= _CATEGORICAL_MAX:
                    note = "(" + "|".join(str(v) for v in vals) + ")"
            parts.append(f"{name} {ctype}{note}")

        row_count = conn.execute(f'SELECT COUNT(*) FROM "{TABLE_NAME}"').fetchone()[0]
        age_min, age_max = conn.execute(
            f'SELECT MIN(age), MAX(age) FROM "{TABLE_NAME}"'
        ).fetchone()
    finally:
        conn.close()

    schema = (
        f"Table: {TABLE_NAME} ({row_count:,} rows; one survey respondent per row)\n"
        "Columns: " + ", ".join(parts) + "\n"
        f"Notes: age is a raw integer ({age_min}-{age_max}); there is NO age_group "
        "column. Score/level columns are numeric scales."
    )
    _cache[key] = schema
    return schema
