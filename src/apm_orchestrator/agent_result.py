"""Turn a Claude Agent SDK run that didn't finish into an explicit error.

Every agent call site here (`run_supervisor`, `run_order_renewal`,
`run_customer_onboarding`) sets `max_turns` and `max_budget_usd`. When
either limit stops a run -- or the run fails for any other reason -- the
CLI ends it with a `ResultMessage` whose `subtype` isn't `"success"`, or
whose `is_error` is true (an API failure on the last turn still reports
`subtype="success"`). Current SDK versions then raise `ResultError` once
the CLI exits non-zero, but that is an SDK implementation detail this
repo's `claude-agent-sdk>=0.1` floor doesn't guarantee, and it arrives
only after the call site has already folded the error result's text into
its "final answer".

`raise_if_stopped` checks the result message itself, so a stopped run is
always an `AgentRunStopped` -- never partial streamed text returned as if
it were a finished answer.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import ResultMessage


class AgentRunStopped(RuntimeError):
    """An agent run ended without a successful result (a turn or budget
    limit, an API error, or an error during execution)."""

    def __init__(self, agent: str, message: ResultMessage) -> None:
        self.agent = agent
        self.subtype = message.subtype
        self.terminal_reason = getattr(message, "terminal_reason", None)
        self.num_turns = message.num_turns
        self.total_cost_usd = message.total_cost_usd
        self.errors = list(getattr(message, "errors", None) or [])
        reason = self.terminal_reason or self.subtype
        cost = f"${self.total_cost_usd:.2f}" if self.total_cost_usd is not None else "unknown cost"
        super().__init__(
            f"{agent} stopped early ({reason}) after {self.num_turns} turn(s), {cost}"
            + (f": {'; '.join(self.errors)}" if self.errors else "")
        )


def raise_if_stopped(agent: str, message: ResultMessage) -> None:
    """Raise `AgentRunStopped` unless `message` is a clean success."""
    if message.subtype != "success" or message.is_error:
        raise AgentRunStopped(agent, message)


def stopped_tool_result(exc: AgentRunStopped) -> dict[str, Any]:
    """An MCP tool error result for a delegate whose agent stopped early,
    so the Supervisor reports the failure instead of summarizing partial
    work as if it had finished."""
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"{exc}. The agent did not finish; any pending action_ids it "
                    "proposed before stopping are still awaiting human review. "
                    "Report this as an incomplete run, not a result."
                ),
            }
        ],
        "is_error": True,
    }
