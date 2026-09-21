"""Standalone poller: resumes in-flight business-process cases once their
apm_connectors pending action resolves. Deliberately decoupled from the
Supervisor and each case graph's own starting path -- it only knows how
to (1) find in-flight cases per agent and (2) check + resume them, so it
can be deployed and scheduled independently of everything else in this
repo.

Two ways to run it, same sweep either way:

    apm-orchestrator-poller --once             # one sweep, then exit --
                                                # point cron/systemd-timer/
                                                # a CI schedule/EventBridge at this
    apm-orchestrator-poller --loop --interval 60   # a long-lived process
"""

from __future__ import annotations

import argparse
import asyncio
import logging
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
from apm_orchestrator.logging_config import configure_logging
from apm_orchestrator.tools import aclose_client, get_client

logger = logging.getLogger("apm_orchestrator.poller")

# One sweep checks every registered agent -- adding a new business agent
# (per docs/roadmap.md's "decomposed by business process") means adding
# its case_graph module here, never new poller logic.
AGENT_MODULES = {
    "order_renewal": order_renewal_case_graph,
    "customer_onboarding": customer_onboarding_case_graph,
}


async def _sweep_agent(agent: str, module, registry: CaseRegistry, checkpointer, client) -> int:
    graph = module.build_case_graph(checkpointer)
    resumed = 0

    for case_id in await registry.list_case_ids(agent=agent):
        config = {"configurable": {"thread_id": case_id}}
        snapshot = await graph.aget_state(config)

        if not snapshot.interrupts:
            continue  # finished already, or never actually paused

        pending_action = snapshot.values.get("pending_action")
        action_id = snapshot.values.get("action_id")
        if not pending_action or not action_id:
            continue

        status = await client.get_action_status(action_id)
        if status is None:
            logger.debug(
                "case still awaiting a human decision", extra={"case_id": case_id, "agent": agent}
            )
            continue

        outcome = await module.resume_case(
            graph,
            case_id,
            approved=bool(status["result"].get("executed")),
            final_result=status["result"],
        )
        logger.info(
            "resumed case",
            extra={
                "case_id": case_id,
                "agent": agent,
                "tool": pending_action.get("tool"),
                "done": outcome.done,
                "awaiting_step": outcome.step,
            },
        )
        resumed += 1

    return resumed


async def sweep_once(database_url: str) -> int:
    """Check every registered case (across every business agent) once;
    resume any whose apm_connectors pending action has resolved
    (approved or rejected). Returns how many cases it resumed."""
    registry = CaseRegistry(database_url)
    await registry.setup()
    client = get_client()
    resumed = 0

    async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
        await checkpointer.setup()
        for agent, module in AGENT_MODULES.items():
            resumed += await _sweep_agent(agent, module, registry, checkpointer, client)

    return resumed


async def _run_once(database_url: str) -> int:
    try:
        return await sweep_once(database_url)
    finally:
        await aclose_client()


async def _run_loop(database_url: str, interval: float) -> None:
    try:
        while True:
            try:
                count = await sweep_once(database_url)
                logger.info("sweep complete", extra={"resumed": count})
            except Exception:
                logger.exception("sweep failed, will retry next interval")
            await asyncio.sleep(interval)
    finally:
        await aclose_client()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--loop", action="store_true", help="Run continuously instead of sweeping once and exiting"
    )
    parser.add_argument("--interval", type=float, default=60.0, help="Seconds between sweeps in --loop mode")
    args = parser.parse_args()

    configure_logging()
    settings = load_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required to run the poller")

    if args.loop:
        asyncio.run(_run_loop(settings.database_url, args.interval))
    else:
        count = asyncio.run(_run_once(settings.database_url))
        logger.info("sweep complete", extra={"resumed": count})


if __name__ == "__main__":
    main()
