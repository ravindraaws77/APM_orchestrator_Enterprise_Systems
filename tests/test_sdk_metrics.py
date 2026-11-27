"""Tests for sdk_metrics.log_result -- the Claude Agent SDK's own
ResultMessage carries cost/usage/duration that every run_* loop
(order_renewal, customer_onboarding, supervisor) used to discard
entirely (it only ever read .subtype/.result). This proves the captured
fields actually land on the log record as structured data, not just
folded into a message string.
"""

from __future__ import annotations

import logging

from claude_agent_sdk import ResultMessage

from apm_orchestrator.sdk_metrics import log_result


def _result_message(**overrides) -> ResultMessage:
    defaults = dict(
        subtype="success",
        duration_ms=1234,
        duration_api_ms=1000,
        is_error=False,
        num_turns=3,
        session_id="sess-1",
        total_cost_usd=0.042,
        usage={"input_tokens": 500, "output_tokens": 120},
        result="done",
        model_usage={
            "claude-sonnet-5": {
                "inputTokens": 500,
                "outputTokens": 120,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.042,
                "contextWindow": 200000,
                "maxOutputTokens": 8192,
            }
        },
    )
    defaults.update(overrides)
    return ResultMessage(**defaults)


def test_log_result_captures_cost_usage_duration(caplog) -> None:
    message = _result_message()

    with caplog.at_level(logging.INFO, logger="apm_orchestrator.sdk"):
        log_result("order_renewal", message, mode="workflow")

    record = caplog.records[0]
    assert record.agent == "order_renewal"
    assert record.mode == "workflow"
    assert record.subtype == "success"
    assert record.total_cost_usd == 0.042
    assert record.usage == {"input_tokens": 500, "output_tokens": 120}
    assert record.duration_ms == 1234
    assert record.duration_api_ms == 1000
    assert record.num_turns == 3
    assert record.session_id == "sess-1"


def test_log_result_captures_which_model_served_the_run(caplog) -> None:
    """The first input agent-drift detection needs (item 8, stage 1): a
    model upgrade behind the scenes can change behavior even with an
    unchanged prompt/policy, so which model(s) actually ran must be on
    the log line, not just cost/duration."""
    message = _result_message()

    with caplog.at_level(logging.INFO, logger="apm_orchestrator.sdk"):
        log_result("order_renewal", message)

    record = caplog.records[0]
    assert record.models == ["claude-sonnet-5"]
    assert record.model_usage["claude-sonnet-5"]["costUSD"] == 0.042


def test_log_result_handles_no_model_usage_reported(caplog) -> None:
    """Older CLI versions (or a result that bypassed the query loop) may
    not report model_usage at all -- must not crash, and `models` should
    read as "nothing to report" rather than blowing up on None."""
    message = _result_message(model_usage=None)

    with caplog.at_level(logging.INFO, logger="apm_orchestrator.sdk"):
        log_result("order_renewal", message)

    record = caplog.records[0]
    assert record.models == []
    assert record.model_usage is None


def test_log_result_captures_a_failed_run_too(caplog) -> None:
    message = _result_message(subtype="error_max_turns", is_error=True, result=None)

    with caplog.at_level(logging.INFO, logger="apm_orchestrator.sdk"):
        log_result("customer_onboarding", message)

    record = caplog.records[0]
    assert record.agent == "customer_onboarding"
    assert record.is_error is True
    assert record.subtype == "error_max_turns"
