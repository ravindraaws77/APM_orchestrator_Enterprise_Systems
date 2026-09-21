"""The Supervisor: business-intent routing only, never connector routing.

Per apm_connectors' docs/roadmap.md, the Supervisor decides *which*
business process a request belongs to (e.g. "this is a renewal") and
hands off to the one specialized agent that owns that process end to
end -- it never calls a connector tool itself. Two specialized agents
exist today (Order-Renewal, Customer-Onboarding); more (Churn
Prevention, ...) arrive as new delegate tools here, each a business
capability, not a per-connector agent.

Every delegation carries a `confidence` alongside the agent it picked --
see SYSTEM_PROMPT. This is the answer to "a request that plausibly fits
both agents, routes to the wrong one, and comes back looking fine": the
adversarial case (fits neither agent) was already covered by a clean
decline, but a plausible-but-wrong routing produces no error and no
decline -- nothing to notice, unless the ambiguity itself is captured at
decision time. `run_supervisor`'s optional `routing_log` persists every
decision (confidence included) to `SupervisorRoutingLog` (db.py) so a
low-confidence routing can be reviewed after the fact instead of
depending on someone noticing.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from apm_orchestrator.agents.customer_onboarding.agent import run_customer_onboarding
from apm_orchestrator.agents.order_renewal.agent import run_order_renewal
from apm_orchestrator.db import SupervisorRoutingLog

DELEGATE_PREFIX = "mcp__supervisor_delegates__delegate_to_"

_CONFIDENCE_PROPERTIES: dict[str, Any] = {
    "confidence": {
        "type": "string",
        "enum": ["high", "low"],
        "description": (
            "How close this call was -- not whether you're willing to make it. "
            "'low' if the request has signals that could plausibly fit either "
            "specialized agent (see SYSTEM_PROMPT's example); 'high' otherwise. "
            "Route to your best judgment either way; this only records how "
            "confident that judgment was, for later review."
        ),
    },
    "rationale": {
        "type": "string",
        "description": "One sentence: what in the request pointed to this agent specifically.",
    },
}


@tool(
    "delegate_to_order_renewal",
    "Hand this request to the Order-Renewal business agent. Use only "
    "when the request's business intent is renewing, extending, or "
    "re-upping an existing customer contract, license, or subscription "
    "-- a business-intent match, never a match on some tool the request "
    "happens to mention (Gmail, Salesforce, ...).",
    {
        "type": "object",
        "properties": {
            "renewal_context": {
                "type": "string",
                "description": (
                    "The original request plus any context useful to the "
                    "Order-Renewal agent (account name, email excerpt, etc.)"
                ),
            },
            **_CONFIDENCE_PROPERTIES,
        },
        "required": ["renewal_context", "confidence", "rationale"],
    },
)
async def delegate_to_order_renewal(args: dict[str, Any]) -> dict[str, Any]:
    result_text = await run_order_renewal(args["renewal_context"], mode="workflow")
    return {"content": [{"type": "text", "text": result_text}]}


@tool(
    "delegate_to_customer_onboarding",
    "Hand this request to the Customer-Onboarding business agent. Use "
    "only when the business intent is kicking off onboarding for a "
    "newly closed-won new-business customer -- a business-intent match, "
    "never a match on some tool the request happens to mention "
    "(Salesforce, Calendar, ...). Never use this for a renewal, even one "
    "that also involves scheduling a call or sending an email.",
    {
        "type": "object",
        "properties": {
            "onboarding_context": {
                "type": "string",
                "description": (
                    "The original request plus any context useful to the "
                    "Customer-Onboarding agent (account name, deal details, etc.)"
                ),
            },
            **_CONFIDENCE_PROPERTIES,
        },
        "required": ["onboarding_context", "confidence", "rationale"],
    },
)
async def delegate_to_customer_onboarding(args: dict[str, Any]) -> dict[str, Any]:
    result_text = await run_customer_onboarding(args["onboarding_context"])
    return {"content": [{"type": "text", "text": result_text}]}


supervisor_server = create_sdk_mcp_server(
    name="supervisor_delegates",
    version="0.1.0",
    tools=[delegate_to_order_renewal, delegate_to_customer_onboarding],
)

SYSTEM_PROMPT = """You are the APM Supervisor. You do business-intent
routing only: you never call a connector tool yourself (Gmail,
Salesforce, Jira, Calendar, Excel, Drive) -- every real action happens
inside whichever specialized business-process agent you delegate to.

Two specialized agents exist today:
- Order-Renewal: any request whose business intent is renewing,
  extending, or re-upping an existing customer contract, license, or
  subscription. Use `delegate_to_order_renewal`.
- Customer-Onboarding: any request whose business intent is kicking off
  onboarding for a newly closed-won new-business customer. Use
  `delegate_to_customer_onboarding`.

These two never overlap: a request about an *existing* customer's
contract is always Order-Renewal, a request about a *brand-new*
customer's kickoff is always Customer-Onboarding. Pass along the
original request and any useful context to whichever you choose.

Every delegation call requires `confidence` ("high" or "low") and a
one-sentence `rationale`. Set `confidence` to "low" whenever the request
has signals that could plausibly fit either agent -- for example: an
*existing* customer that just closed-won an *expansion* deal, where the
request also asks for onboarding-shaped actions like scheduling a
kickoff call or sending a welcome email. Still make your best-judgment
call either way; "low" confidence is not permission to stall or ask a
clarifying question -- it's an honest record of how close the call was,
for a human to review afterward. Never mark "low" just to hedge on an
otherwise clear-cut request, and never mark "high" on a request you're
genuinely unsure about just to avoid saying so.

For any other business intent, say plainly that no specialized agent
exists for it yet -- do not improvise a workflow yourself. This layer's
only job is routing to the right specialist, never reinventing a
specialist's job (the same "no reasoning of its own beyond its stated
job" boundary apm_connectors keeps for its own connectors)."""


def _build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"supervisor_delegates": supervisor_server},
        # Listing a tool here is what auto-approves it at the SDK layer
        # (live-verified) -- see tools.py's module docstring.
        allowed_tools=[
            "mcp__supervisor_delegates__delegate_to_order_renewal",
            "mcp__supervisor_delegates__delegate_to_customer_onboarding",
        ],
    )


async def run_supervisor(prompt: str, *, routing_log: SupervisorRoutingLog | None = None) -> str:
    """Run one Supervisor turn and return its final text response.

    `routing_log` is optional and additive: omit it (the default) and
    behavior is unchanged from before confidence tracking existed. Pass
    a `SupervisorRoutingLog` and, if a delegation happened, this records
    which delegate was called plus its confidence/rationale -- a
    declined request (no delegate called) has nothing to log, same as
    today. Request text is truncated to 500 chars in the log; this is
    an audit trail for routing quality, not a place to store full PII.
    """
    options = _build_options()
    final_text = ""
    delegate_called: str | None = None
    confidence: str | None = None
    rationale: str | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    final_text += block.text
                elif isinstance(block, ToolUseBlock) and block.name.startswith(DELEGATE_PREFIX):
                    delegate_called = block.name[len(DELEGATE_PREFIX) :]
                    confidence = block.input.get("confidence")
                    rationale = block.input.get("rationale")
        elif isinstance(message, ResultMessage) and message.subtype == "success":
            final_text = message.result or final_text

    if routing_log is not None and delegate_called is not None:
        await routing_log.record(
            delegate=delegate_called,
            confidence=confidence or "unknown",
            rationale=rationale or "",
            request_excerpt=prompt[:500],
        )

    return final_text
