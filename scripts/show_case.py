"""Print one case's current (or final, if it's finished) CaseState --
the same LangGraph checkpoint scripts/run_case.py and poller.py
read/resume, surfaced read-only for manual inspection.

Useful once a case has finished (run_case.py's own completion message
doesn't repeat on demand) or any time you want to see exactly what's
paused, on which step, and why -- steps_completed, awaiting_step,
action_id, stop_reason, final_summary -- without going through
apm_connectors' own API.

Usage:
    python scripts/show_case.py acme-2026-09-15
    python scripts/show_case.py --agent customer_onboarding onboard-acme-2026-09-17
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

import apm_orchestrator.agents.customer_onboarding.case_graph as customer_onboarding_case_graph
import apm_orchestrator.agents.order_renewal.case_graph as order_renewal_case_graph
from apm_orchestrator.config import load_settings

# Same dispatch table as run_case.py -- each agent's checkpoint is read
# through its own build_case_graph, since CaseState's shape differs
# per agent (see run_case.py's own comment on this).
AGENT_MODULES = {
    "order_renewal": order_renewal_case_graph,
    "customer_onboarding": customer_onboarding_case_graph,
}


async def main(agent: str, case_id: str) -> None:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required to inspect a case")

    module = AGENT_MODULES[agent]
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        graph = module.build_case_graph(checkpointer)
        snapshot = await graph.aget_state({"configurable": {"thread_id": case_id}})

    if not snapshot.values:
        raise SystemExit(f"No checkpoint found for case_id {case_id!r} -- has it been started with run_case.py?")

    print(json.dumps(snapshot.values, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_MODULES),
        default="order_renewal",
        help="Which business agent started this case (default: order_renewal)",
    )
    parser.add_argument("case_id")
    args = parser.parse_args()
    asyncio.run(main(args.agent, args.case_id))
