"""Tests for the structured (JSON) logging setup that poller.py and
case_graph.py's _guarded use -- proves the formatter produces valid,
parseable JSON with the fields callers rely on (extra fields, exception
tracebacks), not Python's own logging plumbing.
"""

from __future__ import annotations

import json
import logging
import sys

from apm_orchestrator.logging_config import JsonFormatter, configure_logging


def _make_record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="apm_orchestrator.poller",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="resumed case",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formats_valid_json_with_core_fields() -> None:
    record = _make_record()
    parsed = json.loads(JsonFormatter().format(record))

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "apm_orchestrator.poller"
    assert parsed["message"] == "resumed case"
    assert "timestamp" in parsed


def test_extra_fields_surface_as_top_level_keys() -> None:
    record = _make_record(case_id="order-1", agent="order_renewal", done=True)
    parsed = json.loads(JsonFormatter().format(record))

    assert parsed["case_id"] == "order-1"
    assert parsed["agent"] == "order_renewal"
    assert parsed["done"] is True


def test_exception_info_is_captured() -> None:
    try:
        raise RuntimeError("simulated connector failure")
    except RuntimeError:
        record = logging.LogRecord(
            name="apm_orchestrator.case_graph",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="case_graph step failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    parsed = json.loads(JsonFormatter().format(record))

    assert "simulated connector failure" in parsed["exception"]


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    configure_logging()
    root = logging.getLogger()

    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
