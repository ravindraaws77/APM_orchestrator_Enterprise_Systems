"""Renewal-readiness triage agent on Strands Agents.

Given one account name, the agent looks up the Salesforce Opportunity
relevant to a renewal, checks Jira for blocking tickets, optionally
proposes one follow-up ticket (a paused action for a human to approve),
and returns a typed ``RenewalTriageReport`` through Strands' structured
output.

How it maps to the rest of this repo:

- **Model is pinned, never inherited.** ``model_id`` comes from
  ``load_settings().business_agent_model``, the same setting the Claude
  Agent SDK agents use, with an explicit ``max_tokens`` per call.
- **Least-privilege toolbelt.** Only the four tools in ``toolbelt.py``.
  ``ToolPolicyGuard`` also refuses anything else by name at run time.
- **Budget backstop.** ``ToolPolicyGuard`` caps model calls, tool calls,
  proposals and total tokens per run (see ``guardrails.py``).
- **Approval boundary unchanged.** No tool can approve an action. The
  report carries the top-level ``action_id`` for a human reviewer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field
from strands import Agent
from strands.models import Model

from apm_orchestrator.agents.order_renewal.policy import OrderRenewalPolicy, load_policy
from apm_orchestrator.config import Settings, load_settings
from apm_orchestrator.connectors_client import ConnectorsClient
from apm_orchestrator.strands_spike.guardrails import GuardrailLimits, ToolPolicyGuard
from apm_orchestrator.strands_spike.toolbelt import PROPOSAL_TOOLS, TOOLBELT, build_toolbelt

MAX_TOKENS_PER_CALL = 4096


class RenewalTriageReport(BaseModel):
    """The triage verdict for one account, for a human renewal owner."""

    account_name: str
    recommendation: Literal["proceed", "hold_for_blockers", "escalate", "outside_window", "needs_human_review"] = (
        Field(
            description=(
                "proceed: in window, no blockers. hold_for_blockers: blocking tickets are open. "
                "escalate: in window but close date within the escalation threshold or already past. "
                "outside_window: close date outside the renewal window. "
                "needs_human_review: account not found, ambiguous, or a lookup failed."
            )
        )
    )
    opportunity_id: str | None = None
    close_date: str | None = None
    days_to_close: int | None = None
    blocking_ticket_keys: list[str] = Field(default_factory=list)
    stale_open_opportunities: list[str] = Field(default_factory=list)
    proposed_follow_up_action_id: str | None = Field(
        default=None,
        description="Top-level action_id returned by propose_follow_up_ticket, if one was proposed.",
    )
    rationale: str = Field(description="Two or three sentences citing the evidence behind the recommendation.")


SYSTEM_PROMPT = """You are the renewal-readiness triage agent for APM.

For the account you are given:
1. Call lookup_renewal_opportunity once.
2. If an Opportunity was found, call find_blocking_tickets once.
3. Only if there are blocking tickets, or the renewal needs escalation,
   call propose_follow_up_ticket once. It only proposes: a human approves
   or rejects it later. Never claim the ticket was created.
4. Return a RenewalTriageReport. Base every field on tool results, never
   on assumptions. If a lookup failed or the account was ambiguous or not
   found, use needs_human_review and say why.

Escalation rule: an in-window renewal whose days_to_close is below
escalation_threshold_days (including negative values, already past)
is "escalate" unless blockers make it "hold_for_blockers".
"""


@dataclass
class TriageRun:
    report: RenewalTriageReport | None
    stop_reason: str
    audit: list[dict[str, Any]]
    usage: dict[str, Any]
    error: str | None = None


def guardrail_limits_from_env() -> GuardrailLimits:
    defaults = GuardrailLimits()
    return GuardrailLimits(
        max_tool_calls=int(os.environ.get("STRANDS_MAX_TOOL_CALLS", defaults.max_tool_calls)),
        max_proposals=int(os.environ.get("STRANDS_MAX_PROPOSALS", defaults.max_proposals)),
        max_model_calls=int(os.environ.get("STRANDS_MAX_MODEL_CALLS", defaults.max_model_calls)),
        max_total_tokens=int(os.environ.get("STRANDS_MAX_TOTAL_TOKENS", defaults.max_total_tokens)),
    )


def build_model(settings: Settings) -> Model:
    """The pinned Anthropic model for this agent."""
    from strands.models.anthropic import AnthropicModel

    client_args = {"api_key": settings.anthropic_api_key} if settings.anthropic_api_key else None
    return AnthropicModel(
        client_args=client_args,
        model_id=settings.business_agent_model,
        max_tokens=MAX_TOKENS_PER_CALL,
    )


def build_triage_agent(
    client: ConnectorsClient,
    *,
    model: Model | None = None,
    policy: OrderRenewalPolicy | None = None,
    settings: Settings | None = None,
    limits: GuardrailLimits | None = None,
    process_id: str | None = None,
) -> tuple[Agent, ToolPolicyGuard]:
    """Builds the agent and returns it with its guard (whose ``audit``
    holds the run's tool and model decisions). ``model`` is injectable
    so tests can run the full Strands loop with a scripted fake model."""
    settings = settings or load_settings()
    policy = policy or load_policy(settings.order_renewal_policy_path)
    guard = ToolPolicyGuard(
        toolbelt=TOOLBELT,
        proposal_tools=PROPOSAL_TOOLS,
        limits=limits or guardrail_limits_from_env(),
        exempt_tools=frozenset({RenewalTriageReport.__name__}),
    )
    agent = Agent(
        name="renewal_triage",
        model=model or build_model(settings),
        tools=build_toolbelt(client, policy, process_id=process_id),
        system_prompt=SYSTEM_PROMPT,
        hooks=[guard],
        callback_handler=None,
    )
    return agent, guard


async def run_triage(
    account_name: str,
    client: ConnectorsClient,
    *,
    model: Model | None = None,
    policy: OrderRenewalPolicy | None = None,
    settings: Settings | None = None,
    limits: GuardrailLimits | None = None,
    process_id: str | None = None,
) -> TriageRun:
    """Runs one triage and never raises for a guardrail stop: a run cut
    short by a limit comes back with ``report=None`` and the reason, so
    the caller can route it to a human."""
    agent, guard = build_triage_agent(
        client, model=model, policy=policy, settings=settings, limits=limits, process_id=process_id
    )
    prompt = f"Triage renewal readiness for account: {account_name}"
    try:
        result = await agent.invoke_async(prompt, structured_output_model=RenewalTriageReport)
    except Exception as exc:  # Strands raises if the loop ends without structured output.
        return TriageRun(
            report=None,
            stop_reason="no_report",
            audit=list(guard.audit),
            usage=dict(agent.event_loop_metrics.accumulated_usage),
            error=f"{type(exc).__name__}: {exc}",
        )
    return TriageRun(
        report=result.structured_output,
        stop_reason=str(result.stop_reason),
        audit=list(guard.audit),
        usage=dict(result.metrics.accumulated_usage),
    )
