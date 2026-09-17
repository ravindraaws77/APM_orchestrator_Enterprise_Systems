"""Print one Order-Renewal case's current (or final, if it's finished)
CaseState -- the same LangGraph checkpoint scripts/run_case.py and
poller.py read/resume, surfaced read-only for manual inspection.

Useful once a case has finished (run_case.py's own completion message
doesn't repeat on demand) or any time you want to see exactly what's
paused, on which step, and why -- steps_completed, awaiting_step,
action_id, stop_reason, final_summary -- without going through
apm_connectors' own API.

Usage:
    python scripts/show_case.py acme-2026-09-15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

# psycopg's async mode needs a selector-based event loop; Windows'
# asyncio default (ProactorEventLoop) isn't compatible with it and fails
# at connection time with "Psycopg cannot use the 'ProactorEventLoop'"
# -- live-verified, same fix as run_case.py/poller.py. No effect on
# other platforms.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from apm_orchestrator.agents.order_renewal.case_graph import build_case_graph
from apm_orchestrator.config import load_settings


async def main(case_id: str) -> None:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required to inspect a case")

    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        graph = build_case_graph(checkpointer)
        snapshot = await graph.aget_state({"configurable": {"thread_id": case_id}})

    if not snapshot.values:
        raise SystemExit(f"No checkpoint found for case_id {case_id!r} -- has it been started with run_case.py?")

    print(json.dumps(snapshot.values, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_id")
    args = parser.parse_args()
    asyncio.run(main(args.case_id))
