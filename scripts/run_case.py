"""Manually start one business-process case against a real apm_connectors
server and a real Postgres checkpointer. This is the "run as needed"
counterpart to the poller: starting a case is a one-off action (someone
or something detected a signal worth checking), so it's a script, not a
daemon.

Usage:
    python scripts/run_case.py acme-2026-09-15 "Acme Corp"
    python scripts/run_case.py --agent customer_onboarding onboard-acme-2026-09-17 "Acme Corp"
    python scripts/run_case.py --po-file-id <drive-file-id> acme-po-2026-11-05 "Acme Corp"

--po-file-id (order_renewal only) starts the case with a purchase order
to extract (via the document-extraction A2A agent, which must be
running -- DOCUMENT_EXTRACTION_URL) and reconcile against the Opportunity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# psycopg's async mode needs a selector-based event loop; Windows'
# asyncio default (ProactorEventLoop) isn't compatible with it and fails
# at connection time with "Psycopg cannot use the 'ProactorEventLoop'"
# -- live-verified. No effect on other platforms.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import apm_orchestrator.agents.customer_onboarding.case_graph as customer_onboarding_case_graph
import apm_orchestrator.agents.order_renewal.case_graph as order_renewal_case_graph
from apm_orchestrator.config import load_settings
from apm_orchestrator.db import CaseRegistry
from apm_orchestrator.document_extraction.client import aclose_extraction_client
from apm_orchestrator.tools import aclose_client

# Each business agent owns its own case_graph module (build_case_graph +
# start_case) per docs/roadmap.md's "decomposed by business process, not
# by connector" -- this is the one place that maps an --agent flag to
# the right module, so this script stays a thin dispatcher, never a home
# for agent-specific logic itself.
AGENT_MODULES = {
    "order_renewal": order_renewal_case_graph,
    "customer_onboarding": customer_onboarding_case_graph,
}


async def main(agent: str, case_id: str, account_name: str, po_file_id: str | None = None) -> None:
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required to run a case")

    module = AGENT_MODULES[agent]
    registry = CaseRegistry(settings.database_url)
    await registry.setup()

    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        await checkpointer.setup()
        graph = module.build_case_graph(checkpointer)
        kwargs = {"po_file_id": po_file_id} if po_file_id else {}
        outcome = await module.start_case(graph, registry, case_id, account_name, **kwargs)

    if outcome.done:
        print(f"Case {case_id} finished: {outcome.final_summary}")
    elif outcome.step == "po_review":
        print(f"Case {case_id} is waiting for a human to review the extracted PO values:")
        print(json.dumps(outcome.review, indent=2, default=str))
        print(f"\nReview them with: python scripts/review_po_extraction.py {case_id} --reviewer <you> ...")
    else:
        print(f"Case {case_id} is now awaiting approval on step {outcome.step!r} (action_id={outcome.action_id}):")
        print(json.dumps(outcome.pending_action, indent=2, default=str))
        print("\nRun the poller (apm-orchestrator-poller) once that's decided.")

    await aclose_client()
    await aclose_extraction_client()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_MODULES),
        default="order_renewal",
        help="Which business agent starts this case (default: order_renewal)",
    )
    parser.add_argument("case_id")
    parser.add_argument("account_name")
    parser.add_argument(
        "--po-file-id",
        default=None,
        help="order_renewal only: Drive file id of a purchase order to extract and reconcile",
    )
    args = parser.parse_args()
    if args.po_file_id and args.agent != "order_renewal":
        parser.error("--po-file-id is only supported for --agent order_renewal")
    asyncio.run(main(args.agent, args.case_id, args.account_name, args.po_file_id))
