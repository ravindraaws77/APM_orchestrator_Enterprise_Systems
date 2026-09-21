"""Captures the Claude Agent SDK's own cost/usage/duration reporting
(`ResultMessage`) instead of letting it fall on the floor. Every `run_*`
loop in this repo (order_renewal, customer_onboarding, supervisor) reads
only `.subtype`/`.result` off `ResultMessage` and discards everything
else -- there's nowhere today to answer "what did this case/turn cost"
or "how long did the model actually take", even though the SDK hands
that back on every single run.

This logs rather than persists to a case's own state on purpose: the
Claude Agent SDK loops here (`agent.py`, `supervisor.py`) are a
stateless, one-shot `query()` per call with no `case_id` of their own
(unlike `case_graph.py`'s durable, checkpointed nodes) -- see each
`agent.py`'s module docstring for that distinction. Structured logging
(logging_config.py, added alongside this) is what makes a log line an
actual place for this to live, queryable like any other field on it.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import ResultMessage

logger = logging.getLogger("apm_orchestrator.sdk")


def log_result(agent: str, message: ResultMessage, **context: Any) -> None:
    """Call once per `ResultMessage` a `query()` loop receives --
    including a failed/errored one, since cost and duration are still
    worth capturing when a run didn't succeed. `context` is any extra
    structured field the caller wants alongside it (e.g. `mode="report"`).
    """
    logger.info(
        "agent run complete",
        extra={
            "agent": agent,
            "subtype": message.subtype,
            "is_error": message.is_error,
            "duration_ms": message.duration_ms,
            "duration_api_ms": message.duration_api_ms,
            "num_turns": message.num_turns,
            "total_cost_usd": message.total_cost_usd,
            "usage": message.usage,
            "session_id": message.session_id,
            **context,
        },
    )
