"""Print the Supervisor's low-confidence routing decisions for review --
the actual answer to "does it depend on someone noticing" (see
CLAUDE.md and supervisor.py's module docstring).

A routing only ends up here if the Supervisor itself flagged it as a
close call at decision time (`confidence="low"` on the delegation, see
supervisor.py's SYSTEM_PROMPT). This is a read-only retrospective view,
same spirit as show_case.py -- run it periodically (or wire it into a
scheduled job later) to review every request that plausibly fit more
than one specialized agent, whichever way it was actually routed.

Usage:
    python scripts/show_low_confidence_routings.py
    python scripts/show_low_confidence_routings.py --limit 10
"""

from __future__ import annotations

import argparse
import asyncio

from apm_orchestrator.config import load_settings
from apm_orchestrator.db import SupervisorRoutingLog


async def main(limit: int) -> None:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required to read the routing log")

    log = SupervisorRoutingLog(settings.database_url)
    rows = await log.list_low_confidence(limit=limit)

    if not rows:
        print("No low-confidence routings recorded.")
        return

    print(f"{len(rows)} low-confidence routing(s), newest first:\n")
    for row in rows:
        print(f"[{row['created_at']}] -> {row['delegate']}")
        print(f"  rationale: {row['rationale']}")
        print(f"  request:   {row['request_excerpt']}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=50, help="Max rows to show (default: 50)")
    args = parser.parse_args()
    asyncio.run(main(args.limit))
