"""Manually start one Order-Renewal case against a real apm_connectors
server and a real Postgres checkpointer. This is the "run as needed"
counterpart to the poller: starting a case is a one-off action (someone
or something detected a signal worth checking), so it's a script, not a
daemon.

Usage:
    python scripts/run_case.py acme-2026-09-15 "Acme Corp"
"""

from __future__ import annotations

import argparse
import asyncio
import json

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from apm_orchestrator.agents.order_renewal.case_graph import build_case_graph, start_case
from apm_orchestrator.config import load_settings
from apm_orchestrator.db import CaseRegistry
from apm_orchestrator.tools import aclose_client


async def main(case_id: str, account_name: str) -> None:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required to run a case")

    registry = CaseRegistry(settings.database_url)
    await registry.setup()

    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        await checkpointer.setup()
        graph = build_case_graph(checkpointer)
        outcome = await start_case(graph, registry, case_id, account_name)

    if outcome.done:
        print(f"Case {case_id} finished: {outcome.final_summary}")
    else:
        print(f"Case {case_id} is now awaiting approval on step {outcome.step!r} (action_id={outcome.action_id}):")
        print(json.dumps(outcome.pending_action, indent=2, default=str))
        print("\nRun the poller (apm-orchestrator-poller) once that's decided.")

    await aclose_client()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("account_name")
    args = parser.parse_args()
    asyncio.run(main(args.case_id, args.account_name))
