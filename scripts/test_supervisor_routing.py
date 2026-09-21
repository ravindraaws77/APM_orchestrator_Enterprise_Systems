"""Live test: does supervisor.py's LLM-based routing pick the right
delegate agent, and does it correctly flag the cases where "right" isn't
knowable in advance?

Both delegate tools' descriptions and the Supervisor's own system
prompt (supervisor.py) explicitly warn against routing on a mentioned
connector/keyword instead of actual business intent. That claim has
never been exercised against a real request -- every live run so far
called each agent's case_graph.py directly via run_case.py, bypassing
supervisor.py entirely. This script closes that gap.

The ambiguous case used to be marked OBSERVE -- no assertion, just
printed output a human had to read, because there's no single correct
*delegate* to check against. Since the Supervisor now emits a
`confidence` alongside every delegation (see supervisor.py), there IS
something to assert on the ambiguous case even without knowing which
delegate is "right": that the Supervisor called itself out as unsure,
rather than looking exactly as confident as a clear-cut case. That's
the actual fix for "a request that plausibly fits both agents, goes to
the wrong one, and comes back with an answer that looks fine" -- see
CLAUDE.md.

Unlike run_supervisor() (which only returns final text), this taps the
same query() stream supervisor.py itself uses so it can report which
delegate tool, if any, the Supervisor actually invoked, plus its
confidence/rationale -- the facts that matter for a routing test, not
the prose the model produces about it.

Needs ANTHROPIC_API_KEY. Does NOT need DATABASE_URL or live connector
credentials to test routing itself: a delegate tool call is captured
the moment the Supervisor emits it, before the delegated agent's own
(separate) SDK loop runs -- so a downstream failure for lack of live
infra doesn't hide which way the routing went. Run standalone:

    python scripts/test_supervisor_routing.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from apm_orchestrator.supervisor import SYSTEM_PROMPT, supervisor_server
from apm_orchestrator.tools import aclose_client

DELEGATE_PREFIX = "mcp__supervisor_delegates__delegate_to_"


@dataclass
class RoutingCase:
    label: str
    prompt: str
    # None for the deliberately ambiguous case: there is no single
    # correct *delegate* to assert against. expect_low_confidence is
    # what we check instead, for every case.
    expected_delegate: str | None
    expect_low_confidence: bool


@dataclass
class RoutingResult:
    delegates_called: list[str] = field(default_factory=list)
    confidence: str | None = None
    rationale: str | None = None
    final_text: str = ""


CASES = [
    RoutingCase(
        label="clear Order-Renewal",
        prompt=(
            "Acme Corp's license is coming up for renewal next month and "
            "they emailed asking to extend their existing contract."
        ),
        expected_delegate="order_renewal",
        expect_low_confidence=False,
    ),
    RoutingCase(
        label="clear Customer-Onboarding",
        prompt=(
            "We just closed-won a brand-new deal with Initrode Corp -- "
            "kick off their onboarding."
        ),
        expected_delegate="customer_onboarding",
        expect_low_confidence=False,
    ),
    RoutingCase(
        label="ambiguous: expansion deal, existing customer, mentions onboarding-shaped actions",
        prompt=(
            "Acme Corp, an existing customer, just closed-won an expansion "
            "deal for a new product line. Schedule a kickoff call and send "
            "them a welcome email for the new product."
        ),
        expected_delegate=None,
        expect_low_confidence=True,
    ),
]


async def run_traced(prompt: str) -> RoutingResult:
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"supervisor_delegates": supervisor_server},
        allowed_tools=[
            "mcp__supervisor_delegates__delegate_to_order_renewal",
            "mcp__supervisor_delegates__delegate_to_customer_onboarding",
        ],
    )
    result = RoutingResult()
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result.final_text += block.text
                elif isinstance(block, ToolUseBlock) and block.name.startswith(DELEGATE_PREFIX):
                    result.delegates_called.append(block.name[len(DELEGATE_PREFIX) :])
                    result.confidence = block.input.get("confidence")
                    result.rationale = block.input.get("rationale")
        elif isinstance(message, ResultMessage) and message.subtype == "success":
            result.final_text = message.result or result.final_text
    return result


async def main() -> None:
    print(f"Running {len(CASES)} Supervisor routing cases against the real Claude API...\n")
    outcomes: list[tuple[RoutingCase, RoutingResult]] = []
    try:
        for case in CASES:
            print(f"--- {case.label} ---")
            print(f"prompt: {case.prompt!r}")
            result = await run_traced(case.prompt)
            outcomes.append((case, result))
            print(f"delegate(s) called: {result.delegates_called or '(none)'}")
            print(f"confidence: {result.confidence!r}  rationale: {result.rationale!r}")
            print(f"final text: {result.final_text[:300]!r}")
            print()
    finally:
        await aclose_client()

    print("=== Summary ===")
    all_passed = True
    for case, result in outcomes:
        checks: list[str] = []

        if case.expected_delegate is not None:
            if result.delegates_called == [case.expected_delegate]:
                checks.append("delegate: PASS")
            else:
                checks.append(f"delegate: FAIL (expected [{case.expected_delegate}])")
                all_passed = False
        else:
            checks.append(f"delegate: OBSERVE (called {result.delegates_called}, no single correct answer)")

        got_low = result.confidence == "low"
        if got_low == case.expect_low_confidence:
            checks.append(f"confidence: PASS ({result.confidence!r})")
        else:
            checks.append(
                f"confidence: FAIL (got {result.confidence!r}, "
                f"expected {'low' if case.expect_low_confidence else 'high'})"
            )
            all_passed = False

        print(f"{case.label}: {' | '.join(checks)}")

    if not all_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
