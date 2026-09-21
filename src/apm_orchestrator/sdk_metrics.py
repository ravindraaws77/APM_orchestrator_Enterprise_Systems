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

Also captures `model_usage` -- which model(s) actually served this run,
and each one's own token/cost breakdown -- the first input agent-drift
detection needs (a model upgrade behind the scenes can change behavior
even with an unchanged prompt/policy); see the observability doc's
"Item 8: Agent & policy drift", stage 1.
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
            # Which model(s) actually served this run, keyed by model id --
            # `models` is a convenience for filtering/grouping without
            # parsing the nested dict; `model_usage` keeps the full
            # per-model token/cost breakdown the SDK reports.
            "models": sorted(message.model_usage) if message.model_usage else [],
            "model_usage": message.model_usage,
            **context,
        },
    )
