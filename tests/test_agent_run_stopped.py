"""A run stopped by `max_turns`/`max_budget_usd` (or any other non-success
result) must surface as `AgentRunStopped`, never as the partial text that
had streamed before the stop. No live Claude API: `query` is replaced with
a scripted message stream.
"""

from __future__ import annotations

from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from apm_orchestrator import supervisor
from apm_orchestrator.agent_result import AgentRunStopped, raise_if_stopped
from apm_orchestrator.agents.order_renewal import agent as order_renewal_agent


def _result(subtype: str = "success", *, is_error: bool = False, result: str | None = "done") -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=5,
        session_id="s",
        total_cost_usd=0.42,
        result=result,
    )


def _fake_query(*messages: Any):
    async def fake(*, prompt: str, options: Any):
        for m in messages:
            yield m

    return fake


PARTIAL = AssistantMessage(content=[TextBlock(text="Checking Salesforce for Acme...")], model="m")


def test_success_passes():
    raise_if_stopped("X", _result())


@pytest.mark.parametrize(
    ("subtype", "is_error"),
    [("error_max_turns", True), ("error_max_budget_usd", True), ("error_during_execution", True),
     # An API failure on the last turn still reports subtype "success".
     ("success", True)],
)
def test_non_success_raises(subtype, is_error):
    with pytest.raises(AgentRunStopped) as exc_info:
        raise_if_stopped("Supervisor", _result(subtype, is_error=is_error, result=None))
    assert exc_info.value.subtype == subtype
    assert "Supervisor stopped early" in str(exc_info.value)
    assert "5 turn(s)" in str(exc_info.value) and "$0.42" in str(exc_info.value)


async def test_supervisor_raises_instead_of_returning_partial_text(monkeypatch):
    monkeypatch.setattr(
        supervisor, "query", _fake_query(PARTIAL, _result("error_max_turns", is_error=True, result=None))
    )
    with pytest.raises(AgentRunStopped):
        await supervisor.run_supervisor("Is Acme up for renewal?")


async def test_supervisor_success_returns_result(monkeypatch):
    monkeypatch.setattr(supervisor, "query", _fake_query(PARTIAL, _result(result="Routed to Order-Renewal.")))
    assert await supervisor.run_supervisor("Is Acme up for renewal?") == "Routed to Order-Renewal."


async def test_order_renewal_agent_raises_when_stopped(monkeypatch):
    monkeypatch.setattr(
        order_renewal_agent,
        "query",
        _fake_query(PARTIAL, _result("error_max_budget_usd", is_error=True, result=None)),
    )
    with pytest.raises(AgentRunStopped):
        await order_renewal_agent.run_order_renewal("Renew Acme")


async def test_delegate_tool_reports_stopped_agent_as_tool_error(monkeypatch):
    async def stopped(*args: Any, **kwargs: Any) -> str:
        raise AgentRunStopped("Order-Renewal agent", _result("error_max_turns", is_error=True, result=None))

    monkeypatch.setattr(supervisor, "run_order_renewal", stopped)
    out = await supervisor.delegate_to_order_renewal.handler(
        {"renewal_context": "Renew Acme", "confidence": "high", "rationale": "Renewal request."}
    )
    assert out["is_error"] is True
    text = out["content"][0]["text"]
    assert "Order-Renewal agent stopped early" in text
    assert "not a result" in text


async def test_supervisor_still_logs_routing_when_stopped_after_delegating(monkeypatch):
    from claude_agent_sdk import ToolUseBlock

    delegated = AssistantMessage(
        content=[
            ToolUseBlock(
                id="t1",
                name=supervisor.DELEGATE_PREFIX + "order_renewal",
                input={"renewal_context": "Renew Acme", "confidence": "low", "rationale": "Could be onboarding."},
            )
        ],
        model="m",
    )
    monkeypatch.setattr(
        supervisor, "query", _fake_query(delegated, _result("error_max_turns", is_error=True, result=None))
    )

    class FakeLog:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def record(self, **kwargs: Any) -> None:
            self.rows.append(kwargs)

    log = FakeLog()
    with pytest.raises(AgentRunStopped):
        await supervisor.run_supervisor("Renew Acme", routing_log=log)  # type: ignore[arg-type]
    assert log.rows == [
        {"delegate": "order_renewal", "confidence": "low", "rationale": "Could be onboarding.",
         "request_excerpt": "Renew Acme"}
    ]
