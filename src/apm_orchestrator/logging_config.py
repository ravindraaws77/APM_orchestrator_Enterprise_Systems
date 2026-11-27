"""Structured (JSON) logging setup, shared by the poller and any other
entry point (cli.py, scripts/) that wants machine-parseable logs instead
of the plain-text formatting poller.py used before this. This repo and
apm_connectors each keep their own copy -- neither imports the other's
internals (see CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

# Every attribute a bare LogRecord already carries, so `extra=...`
# fields are the only thing added on top.
_RESERVED_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger name, message,
    the exception traceback (if any), and any `extra={...}` fields a
    caller attached to the record -- e.g.
    `logger.error("...", extra={"case_id": case_id, "step": step_name})`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    """Installs the JSON formatter on the root logger. Idempotent -- safe
    to call more than once without stacking duplicate handlers. `level`
    defaults to the `APM_LOG_LEVEL` env var, then "INFO".
    """
    root = logging.getLogger()
    root.setLevel(level or os.environ.get("APM_LOG_LEVEL", "INFO"))
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
