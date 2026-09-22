"""Run a batch of varied prompts through the Supervisor in one process, to
seed SupervisorRoutingLog with rows for scripts/review_routing_log.py and
scripts/calibration_report.py.

Every prompt below is deliberately NOT from evals/routing_cases.py -- the
golden dataset only proves the model agrees with that dataset's own
labels; calibration needs rows from prompts nothing has graded in
advance, so review is judging real routing decisions, not re-confirming
eval cases with a different script.

Only a prompt the Supervisor actually delegates gets logged: a declined
(out-of-scope) request has nothing to record, same as the CLI (see
run_supervisor's docstring in supervisor.py) -- so nothing here is
out-of-scope on purpose. A delegated prompt runs the FULL downstream
agent (mode="workflow"), not just the routing decision: expect real
Salesforce/Jira/Gmail reads and real Claude API calls per prompt, same
as running `apm-orchestrator` by hand once per line below. No writes
happen without a human approving them (same approval gate as always),
but reads and model calls are real and cost real API usage.

One run of this script is a start, not the whole calibration dataset --
calibration_report.py needs >=10 REVIEWED rows in each confidence
bucket before it gives a verdict, and review is a separate, manual step
(review_routing_log.py) that happens after routing decisions exist to
review. Re-run this (editing PROMPTS to add fresh variety each time,
so review keeps seeing new decisions, not repeats) as part of ongoing
usage, not as a one-shot batch.

Usage:
    python scripts/run_calibration_batch.py
    python scripts/run_calibration_batch.py --limit 3   # just try the first few
"""

from __future__ import annotations

import argparse
import asyncio

from apm_orchestrator.config import load_settings
from apm_orchestrator.db import SupervisorRoutingLog
from apm_orchestrator.supervisor import run_supervisor
from apm_orchestrator.tools import aclose_client

# Fresh company names, distinct from evals/routing_cases.py, covering the
# same shapes that dataset uses (see its module docstring) -- skewed
# toward adversarial/ambiguous on purpose, since only a delegated call is
# logged at all, and "low" confidence rows are the harder bucket to fill:
# a clear case almost always comes back "high."
PROMPTS: list[str] = [
    # -- clear: Order-Renewal --
    "Wayne Enterprises' security services contract is up for renewal in "
    "three weeks -- they emailed asking us to proceed.",
    "Sterling Cooper's annual license expires next Friday, please start "
    "the renewal.",
    # -- clear: Customer-Onboarding --
    "We just closed a brand-new deal with Oscorp -- first contract ever, "
    "get their onboarding started.",
    "Prestige Worldwide just signed as a brand-new customer -- kick off "
    "onboarding and send the welcome packet.",
    # -- adversarial: trap wording, unambiguous once you check account status --
    "Tyrell Corp wants to renew their contract but with a new payment "
    "schedule -- same existing account as always.",
    "Vandelay Industries wants to extend their trial into a first paid "
    "contract -- they've never been a real customer before.",
    # -- ambiguous: genuinely torn between both agents' definitions --
    "Dunder Mifflin, a longtime customer, just closed an expansion deal "
    "for a second office -- get a kickoff call scheduled and send a "
    "renewal confirmation too.",
    "This account's contract is expanding into a new region -- can you "
    "get things moving?",
]


async def main(limit: int | None) -> None:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required")

    routing_log = SupervisorRoutingLog(settings.database_url)
    await routing_log.setup()

    prompts = PROMPTS[:limit] if limit else PROMPTS
    logged = 0
    try:
        for i, prompt in enumerate(prompts, 1):
            print(f"\n=== [{i}/{len(prompts)}] {prompt[:70]!r}... ===")
            # list_for_review (no confidence filter) is oldest-first over
            # every unreviewed row -- since nothing in a fresh batch has
            # been reviewed yet, its last entry is this call's own new row,
            # if one landed. A before/after count diff (rather than reading
            # the row straight off run_supervisor's return value, which
            # only gives back final_text) is what confirms whether this
            # specific prompt actually logged one at all.
            before_ids = {row["id"] for row in await routing_log.list_for_review(limit=10_000)}
            result = await run_supervisor(prompt, routing_log=routing_log)
            after_rows = await routing_log.list_for_review(limit=10_000)
            print(result[:300])
            new_rows = [row for row in after_rows if row["id"] not in before_ids]
            if new_rows:
                logged += 1
                row = new_rows[-1]
                print(f"-> logged: delegate={row['delegate']} confidence={row['confidence']}")
            else:
                print("-> declined (no delegate called, nothing logged)")
    finally:
        await aclose_client()

    print(
        f"\nDone -- {logged}/{len(prompts)} prompt(s) delegated and logged.\n"
        "Review with:\n"
        "  python scripts/review_routing_log.py list --confidence low\n"
        "  python scripts/review_routing_log.py list --confidence high\n"
        "Then mark verdicts and check scripts/calibration_report.py once "
        ">=10 rows are reviewed per bucket."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="only run the first N prompts")
    args = parser.parse_args()
    asyncio.run(main(args.limit))
