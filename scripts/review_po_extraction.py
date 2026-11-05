"""Review the extracted purchase-order values for an Order-Renewal case
paused at `wait_for_po_review`, and resume it.

This is a human checking *what the document says* -- not the write
approval. Whatever the case proposes afterwards (an Opportunity update
or a Jira ticket) still waits in apm_connectors for its own,
separately authenticated approval.

Usage:
    # show the extraction and why it was flagged
    python scripts/review_po_extraction.py acme-po-2026-11-05

    # accept, correcting one value (other flagged values are confirmed as-is)
    python scripts/review_po_extraction.py acme-po-2026-11-05 --reviewer jane@example.com \\
        --accept --set po_number=PO-004512

    # reject the document (the case files a Jira ticket for follow-up)
    python scripts/review_po_extraction.py acme-po-2026-11-05 --reviewer jane@example.com \\
        --reject --reason "This is a draft quote, not a signed PO"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

# Same Windows event-loop guard as run_case.py/show_case.py: this touches
# Postgres and spawns no subprocess.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from apm_orchestrator.agents.order_renewal import case_graph
from apm_orchestrator.config import load_settings
from apm_orchestrator.document_extraction.client import aclose_extraction_client
from apm_orchestrator.tools import aclose_client


def _print_review(review: dict) -> None:
    extraction = review.get("extraction") or {}
    reasons = review.get("review_reasons") or {}
    print(f"A2A task: {review.get('task_id')}  document type: {extraction.get('document_type')}")
    if extraction.get("notes"):
        print(f"Notes: {extraction['notes']}")
    print()
    for name, fv in (extraction.get("fields") or {}).items():
        flag = f"  <-- {reasons[name]}" if name in reasons else ""
        print(f"  {name:15} {fv.get('value')!s:30} conf={fv.get('confidence', 0):.2f}{flag}")
        if fv.get("evidence"):
            print(f"  {'':15} evidence: {fv['evidence']!r}")
    for key, reason in reasons.items():
        if key.startswith("_"):
            print(f"\n  ! {reason}")


async def main(args: argparse.Namespace) -> None:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required to review a case")

    reviewed_fields = {}
    for item in args.set or []:
        name, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--set expects field=value, got {item!r}")
        reviewed_fields[name] = value

    try:
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
            graph = case_graph.build_case_graph(checkpointer)
            snapshot = await graph.aget_state({"configurable": {"thread_id": args.case_id}})
            payloads = [i.value for i in snapshot.interrupts]
            if not payloads or payloads[0].get("type") != "awaiting_po_field_review":
                raise SystemExit(f"Case {args.case_id!r} is not waiting on a PO review.")

            if not (args.accept or args.reject):
                _print_review(payloads[0]["review"])
                print("\nRe-run with --reviewer and --accept [--set field=value ...] or --reject --reason ...")
                return

            outcome = await case_graph.resume_po_review(
                graph,
                args.case_id,
                reviewer=args.reviewer,
                decision="accept" if args.accept else "reject",
                reviewed_fields=reviewed_fields,
                reason=args.reason,
            )
    finally:
        await aclose_client()
        await aclose_extraction_client()

    if outcome.done:
        print(f"Case {args.case_id} finished: {outcome.final_summary}")
    elif outcome.step == "po_review":
        print("The review wasn't applied -- the case is still waiting on it:")
        _print_review(outcome.review or {})
    else:
        print(f"Case {args.case_id} is now awaiting approval on step {outcome.step!r} (action_id={outcome.action_id}):")
        print(json.dumps(outcome.pending_action, indent=2, default=str))
        print("\nRun the poller (apm-orchestrator-poller) once that's decided.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_id")
    parser.add_argument("--reviewer", help="Your identity, recorded on the extraction and in the A2A task")
    decision = parser.add_mutually_exclusive_group()
    decision.add_argument("--accept", action="store_true")
    decision.add_argument("--reject", action="store_true")
    parser.add_argument("--set", action="append", metavar="FIELD=VALUE", help="Corrected value (repeatable)")
    parser.add_argument("--reason", help="Why the document is rejected")
    args = parser.parse_args()
    if (args.accept or args.reject) and not args.reviewer:
        parser.error("--reviewer is required to accept or reject")
    if args.set and not args.accept:
        parser.error("--set only applies with --accept")
    asyncio.run(main(args))
