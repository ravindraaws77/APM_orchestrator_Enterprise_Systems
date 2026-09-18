"""The Supervisor: business-intent routing only, never connector routing.

Per apm_connectors' docs/roadmap.md, the Supervisor decides *which*
business process a request belongs to (e.g. "this is a renewal") and
hands off to the one specialized agent that owns that process end to
end -- it never calls a connector tool itself. Two specialized agents
exist today (Order-Renewal, Customer-Onboarding); more (Churn
Prevention, ...) arrive as new delegate tools here, each a business
capability, not a per-connector agent.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from apm_orchestrator.agents.customer_onboarding.agent import run_customer_onboarding
from apm_orchestrator.agents.order_renewal.agent import run_order_renewal


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
            }
        },
        "required": ["renewal_context"],
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
            }
        },
        "required": ["onboarding_context"],
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


async def run_supervisor(prompt: str) -> str:
    """Run one Supervisor turn and return its final text response."""
    options = _build_options()
    final_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    final_text += block.text
        elif isinstance(message, ResultMessage) and message.subtype == "success":
            final_text = message.result or final_text
    return final_text
