"""Runs routing_cases.CASES against the real Supervisor + real Claude
API and reports pass/fail per case plus an overall pass rate.

Scope: this evaluates the Supervisor's *routing decision* only, not
whatever the delegated agent does afterward. Left un-stubbed,
delegate_to_order_renewal/delegate_to_customer_onboarding would each run
that agent's own full Claude Agent SDK loop against real
apm_connectors tools -- a second, separate, much more expensive live
eval surface (and one that needs a running apm_connectors server this
eval doesn't require). So the delegate functions supervisor.py already
imported are patched to lightweight stubs that just record which one
got called and return immediately, the same "capture the tool call
callers, don't chase live infra" trace scripts/test_supervisor_routing.py's
original run_traced already relied on -- just applied deliberately
instead of by making the actual downstream call fail on its own after
the fact.

Also checks confidence calibration, not just the delegate choice --
supervisor.py's delegate tools require a confidence ("high"/"low") on
every call (see that module's docstring for why: a plausible-but-wrong
routing produces no error and looks fine, so the ambiguity has to be
caught at decision time). Expected confidence follows directly from
category: "low" for "ambiguous" cases, "high" for every hard-expected
one -- "adversarial" cases especially, since the whole point of that
category is that the correct routing is *not* actually ambiguous
despite the tempting wording, so "low" there is itself a miscalibration.
`expect_none` cases never call a delegate, so there's no confidence to
check. Confidence failures are reported and included in the JSON output
but deliberately don't affect this script's exit code (see main()) --
unlike delegate correctness, this is a new, not-yet-battle-tested signal
and a flaky nightly build over it isn't worth it yet.

Usage:
    python -m apm_orchestrator.evals.run_supervisor_routing_eval
    python -m apm_orchestrator.evals.run_supervisor_routing_eval --output results.json

Needs ANTHROPIC_API_KEY. Does not need DATABASE_URL or any connector
credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from apm_orchestrator import supervisor
from apm_orchestrator.evals.routing_cases import CASES, RoutingCase
from apm_orchestrator.tools import aclose_client

DELEGATE_PREFIX = "mcp__supervisor_delegates__delegate_to_"


@dataclass
class RoutingResult:
    delegates_called: list[str] = field(default_factory=list)
    confidence: str | None = None
    rationale: str | None = None
    final_text: str = ""


async def run_traced(prompt: str) -> RoutingResult:
    options = ClaudeAgentOptions(
        system_prompt=supervisor.SYSTEM_PROMPT,
        mcp_servers={"supervisor_delegates": supervisor.supervisor_server},
        allowed_tools=[
            "mcp__supervisor_delegates__delegate_to_order_renewal",
            "mcp__supervisor_delegates__delegate_to_customer_onboarding",
        ],
    )
    result = RoutingResult()
    # Stub the downstream agents, not the routing tools themselves: same
    # tool names/descriptions/schemas the real Supervisor decides against
    # (supervisor_server is untouched), only what happens *after* a
    # delegate is chosen is replaced -- see module docstring.
    with (
        patch.object(supervisor, "run_order_renewal", return_value="stubbed for eval"),
        patch.object(supervisor, "run_customer_onboarding", return_value="stubbed for eval"),
    ):
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


def verdict_for(case: RoutingCase, result: RoutingResult) -> str:
    if case.expect_none:
        return "PASS" if result.delegates_called == [] else "FAIL (expected no delegate)"
    if case.expected_delegate is None:
        return "OBSERVE"
    if result.delegates_called == [case.expected_delegate]:
        return "PASS"
    return f"FAIL (expected [{case.expected_delegate}])"


def expected_confidence(case: RoutingCase) -> str | None:
    """None for expect_none cases (no delegate call, nothing to check).
    'low' for the ambiguous category, 'high' for every hard-expected
    case -- including adversarial, since that category's whole point is
    that the correct routing is *not* actually ambiguous despite the
    tempting wording."""
    if case.expect_none:
        return None
    return "low" if case.category == "ambiguous" else "high"


def confidence_verdict_for(case: RoutingCase, result: RoutingResult) -> str | None:
    expected = expected_confidence(case)
    if expected is None:
        return None
    if result.confidence == expected:
        return "PASS"
    return f"FAIL (expected {expected!r}, got {result.confidence!r})"


async def evaluate_all(cases: list[RoutingCase]) -> list[tuple[RoutingCase, RoutingResult, str]]:
    outcomes = []
    for case in cases:
        result = await run_traced(case.prompt)
        outcomes.append((case, result, verdict_for(case, result)))
    return outcomes


def summarize(outcomes: list[tuple[RoutingCase, RoutingResult, str]]) -> dict:
    hard = [(c, r, v) for c, r, v in outcomes if v != "OBSERVE"]
    passed = [o for o in hard if o[2] == "PASS"]
    failed = [o for o in hard if o[2] != "PASS"]

    confidence_checks = [(c, r, confidence_verdict_for(c, r)) for c, r, _ in outcomes]
    confidence_checked = [(c, r, cv) for c, r, cv in confidence_checks if cv is not None]
    confidence_failed = [o for o in confidence_checked if o[2] != "PASS"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(outcomes),
        "hard_cases": len(hard),
        "passed": len(passed),
        "failed": len(failed),
        "pass_rate": (len(passed) / len(hard)) if hard else 1.0,
        "confidence_checked": len(confidence_checked),
        "confidence_failed": len(confidence_failed),
        "results": [
            {
                "label": case.label,
                "category": case.category,
                "expected_delegate": case.expected_delegate,
                "expect_none": case.expect_none,
                "delegates_called": result.delegates_called,
                "verdict": verdict,
                "confidence": result.confidence,
                "rationale": result.rationale,
                "confidence_verdict": confidence_verdict_for(case, result),
                "final_text": result.final_text[:500],
            }
            for case, result, verdict in outcomes
        ],
    }


async def main(output: Path | None) -> int:
    print(f"Running {len(CASES)} Supervisor routing cases against the real Claude API...\n")
    try:
        outcomes = await evaluate_all(CASES)
    finally:
        await aclose_client()

    for case, result, verdict in outcomes:
        print(f"[{case.category}] {case.label}")
        print(f"  delegate(s) called: {result.delegates_called or '(none)'} -> {verdict}")
        cv = confidence_verdict_for(case, result)
        if cv is not None:
            print(f"  confidence: {result.confidence!r} -> {cv}  (rationale: {result.rationale!r})")

    summary = summarize(outcomes)
    print("\n=== Summary ===")
    print(
        f"{summary['passed']}/{summary['hard_cases']} hard-expected cases passed "
        f"({summary['pass_rate']:.0%}); {summary['total_cases'] - summary['hard_cases']} ambiguous case(s) observed only"
    )
    print(
        f"confidence calibration: {summary['confidence_checked'] - summary['confidence_failed']}"
        f"/{summary['confidence_checked']} correct (informational -- does not affect exit code, see module docstring)"
    )
    for case, result, verdict in outcomes:
        if verdict not in ("PASS", "OBSERVE"):
            print(f"  FAILED: {case.label} -- called {result.delegates_called or '(none)'}, {verdict}")
    for case, result, _ in outcomes:
        cv = confidence_verdict_for(case, result)
        if cv is not None and cv != "PASS":
            print(f"  CONFIDENCE MISCALIBRATED: {case.label} -- {cv}")

    if output is not None:
        output.write_text(json.dumps(summary, indent=2))
        print(f"\nWrote results to {output}")

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=None, help="write JSON results to this path")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.output)))
