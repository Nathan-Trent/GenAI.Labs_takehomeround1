"""Structured logging and request tracing for the analytics pipeline.

Stdlib-only. Emits one JSON object per line so logs are machine-parseable
(ELK/Datadog/CloudWatch-ready) while staying readable in a terminal.

Every pipeline request gets a request_id; every stage logs an event carrying
it, so a single question can be traced end-to-end:

    {"ts": "...", "level": "INFO", "event": "stage.sql_generation",
     "request_id": "a1b2c3d4e5f6", "ms": 812.4, "tokens": 143, ...}

Set LOG_LEVEL (default INFO) or LOG_DISABLED=1 in the environment.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

_LOGGER_NAME = "analytics_pipeline"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=True, default=str)


def get_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
        if os.getenv("LOG_DISABLED", "").strip() in ("1", "true", "yes"):
            logger.disabled = True
    return logger


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured log line. Non-serializable values become strings."""
    get_logger().log(level, event, extra={"fields": fields})


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
