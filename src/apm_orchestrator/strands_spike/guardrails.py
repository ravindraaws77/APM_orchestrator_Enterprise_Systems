"""Run-time guardrails for the Strands triage agent, as Strands hooks.

The Claude Agent SDK gives this repo ``allowed_tools``, ``max_turns`` and
``max_budget_usd`` as options. Strands has no direct equivalents, so the
same limits are enforced here through its hook system:

- ``BeforeToolCallEvent``: refuse any tool outside the toolbelt, cap the
  number of tool calls, and allow at most ``max_proposals`` write
  proposals per run.
- ``BeforeModelCallEvent``: stop the loop once it has used
  ``max_model_calls`` model calls or ``max_total_tokens`` tokens. This is
  the budget backstop: a token ceiling rather than a dollar one, because
  Strands reports usage in tokens and a hard-coded price table would go
  stale.

Every decision is appended to ``audit`` so a run can be reviewed after
the fact. None of this replaces apm_connectors' approval gate, which is
enforced server-side whatever happens here. These limits only stop a
misbehaving loop early and cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands.hooks import (
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)


@dataclass
class GuardrailLimits:
    max_tool_calls: int = 8
    max_proposals: int = 1
    max_model_calls: int = 10
    max_total_tokens: int = 60_000


@dataclass
class ToolPolicyGuard(HookProvider):
    """Enforces the toolbelt and run limits for one agent.

    ``exempt_tools`` are local tools with no external effect (Strands'
    structured-output tool), allowed even after the tool-call cap is hit
    so the agent can still return its report.
    """

    toolbelt: frozenset[str]
    proposal_tools: frozenset[str]
    limits: GuardrailLimits = field(default_factory=GuardrailLimits)
    exempt_tools: frozenset[str] = frozenset()
    audit: list[dict[str, Any]] = field(default_factory=list)
    _tool_calls: int = 0
    _proposals: int = 0
    _model_calls: int = 0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._reset)
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(BeforeModelCallEvent, self._before_model)

    def _reset(self, _event: BeforeInvocationEvent) -> None:
        self._tool_calls = self._proposals = self._model_calls = 0
        self.audit.clear()

    def _record(self, kind: str, name: str, allowed: bool, reason: str = "") -> None:
        self.audit.append({"kind": kind, "name": name, "allowed": allowed, "reason": reason})

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name in self.exempt_tools:
            self._record("tool", name, True, "exempt")
            return

        reason = ""
        if name not in self.toolbelt:
            reason = f"Tool {name!r} is not in this agent's toolbelt."
        elif self._tool_calls >= self.limits.max_tool_calls:
            reason = f"Tool-call limit ({self.limits.max_tool_calls}) reached. Finish with what you have."
        elif name in self.proposal_tools and self._proposals >= self.limits.max_proposals:
            reason = f"Only {self.limits.max_proposals} proposal(s) allowed per run."

        if reason:
            event.cancel_tool = reason
            self._record("tool", name, False, reason)
            return

        self._tool_calls += 1
        if name in self.proposal_tools:
            self._proposals += 1
        self._record("tool", name, True)

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        used = event.agent.event_loop_metrics.accumulated_usage.get("totalTokens", 0)
        reason = ""
        if self._model_calls >= self.limits.max_model_calls:
            reason = f"Model-call limit ({self.limits.max_model_calls}) reached."
        elif used >= self.limits.max_total_tokens:
            reason = f"Token budget ({self.limits.max_total_tokens}) used up ({used} tokens)."

        if reason:
            event.cancel = reason
            self._record("model", "model_call", False, reason)
            return
        self._model_calls += 1
        self._record("model", "model_call", True)
