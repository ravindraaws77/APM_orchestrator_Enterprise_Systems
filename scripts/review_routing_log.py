"""Sample Supervisor routing decisions for human review, and record the
verdict -- the ground truth calibration_report.py needs.

Deliberately samples *both* confidence buckets, not just low-confidence
ones: calibration only means something if you know the wrong-rate on
both sides, not just how often a flagged case was actually wrong.
Reviewing only what's already flagged can't tell you whether unflagged
("high") routings are wrong just as often.

Usage:
    # See what's waiting for review, one bucket at a time
    python scripts/review_routing_log.py list --confidence low
    python scripts/review_routing_log.py list --confidence high

    # Record a verdict once you've read the request/rationale and
    # decided whether the delegate it actually called was the right one
    python scripts/review_routing_log.py mark 42 --correct
    python scripts/review_routing_log.py mark 43 --incorrect
"""

from __future__ import annotations

import argparse
import asyncio

from apm_orchestrator.config import load_settings
from apm_orchestrator.db import SupervisorRoutingLog


async def do_list(log: SupervisorRoutingLog, confidence: str | None, limit: int) -> None:
    rows = await log.list_for_review(confidence=confidence, limit=limit)
    if not rows:
        bucket = f" ({confidence})" if confidence else ""
        print(f"Nothing unreviewed{bucket}.")
        return

    print(f"{len(rows)} unreviewed routing(s):\n")
    for row in rows:
        print(f"#{row['id']}  [{row['created_at']}]  confidence={row['confidence']!r} -> {row['delegate']}")
        print(f"  rationale: {row['rationale']}")
        print(f"  request:   {row['request_excerpt']}")
        print(f"  -> mark with: python scripts/review_routing_log.py mark {row['id']} --correct|--incorrect")
        print()


async def do_mark(log: SupervisorRoutingLog, row_id: int, correct: bool) -> None:
    await log.mark_reviewed(row_id, correct=correct)
    print(f"#{row_id} marked {'correct' if correct else 'incorrect'}.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="show unreviewed routing decisions")
    list_p.add_argument("--confidence", choices=["high", "low"], default=None, help="filter to one bucket")
    list_p.add_argument("--limit", type=int, default=20)

    mark_p = sub.add_parser("mark", help="record whether a routing decision was actually correct")
    mark_p.add_argument("id", type=int)
    verdict = mark_p.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--correct", action="store_true")
    verdict.add_argument("--incorrect", action="store_true")

    args = parser.parse_args()

    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required")
    log = SupervisorRoutingLog(settings.database_url)
    await log.setup()

    if args.command == "list":
        await do_list(log, args.confidence, args.limit)
    else:
        await do_mark(log, args.id, correct=args.correct)


if __name__ == "__main__":
    asyncio.run(main())
