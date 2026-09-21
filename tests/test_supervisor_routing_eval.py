"""pytest entry point for the Supervisor routing eval (evals/routing_cases.py
+ evals/run_supervisor_routing_eval.py). Runs the real dataset against the
real Claude API exactly once per session (module-scoped fixture), then
asserts each hard-expected case individually so a CI failure names the
specific routing case that regressed rather than "the eval failed."

Also asserts confidence calibration per case (see
run_supervisor_routing_eval.py's module docstring for why confidence
exists and what's expected per category) -- unlike that script's own
exit code, a pytest assertion here is binary, and this file was never
part of the fast/default pytest job anyway (see below), so there's no
"soft signal" tradeoff to make: a miscalibration is just a failing test,
same as a wrong delegate.

Skipped like this repo's other live-infra tests (test_case_graph_mechanics.py)
when the credential it needs isn't set -- here that's ANTHROPIC_API_KEY,
not APM_TEST_DATABASE_URL, since this needs the real model, nothing else.

Not part of the fast/default test run in spirit (it makes ~20 real API
calls), but nothing here restricts *when* it can run other than that
skip -- see .github/workflows/eval.yml for how CI schedules it
separately from the fast pytest job.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from apm_orchestrator.evals.routing_cases import CASES
from apm_orchestrator.evals.run_supervisor_routing_eval import confidence_verdict_for, evaluate_all

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set -- skipping live Supervisor routing eval",
)

HARD_CASES = [case for case in CASES if case.expected_delegate is not None or case.expect_none]
# Every case except expect_none ones has an expected confidence -- see
# expected_confidence() in run_supervisor_routing_eval.py.
CONFIDENCE_CASES = [case for case in CASES if not case.expect_none]


@pytest.fixture(scope="module")
def outcomes():
    return {case.label: (result, verdict) for case, result, verdict in asyncio.run(evaluate_all(CASES))}


@pytest.mark.parametrize("case", HARD_CASES, ids=[c.label for c in HARD_CASES])
def test_routing_case(case, outcomes):
    result, verdict = outcomes[case.label]
    assert verdict == "PASS", (
        f"{case.label}: expected "
        f"{'no delegate' if case.expect_none else case.expected_delegate!r}, "
        f"got {result.delegates_called!r}\nfinal text: {result.final_text[:300]!r}"
    )


@pytest.mark.parametrize("case", CONFIDENCE_CASES, ids=[c.label for c in CONFIDENCE_CASES])
def test_routing_confidence(case, outcomes):
    result, _ = outcomes[case.label]
    confidence_verdict = confidence_verdict_for(case, result)
    assert confidence_verdict == "PASS", (
        f"{case.label}: {confidence_verdict}\nrationale given: {result.rationale!r}"
    )
