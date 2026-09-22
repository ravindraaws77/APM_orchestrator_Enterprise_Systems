"""Manual smoke test: run the Supervisor on one prompt from the command
line. Not a service -- Phase 1's shared task/conversation state
(Postgres-backed multi-step tracking) isn't built yet, so each
invocation is a single, independent turn.
"""

from __future__ import annotations

import argparse
import asyncio

# Deliberately NOT the WindowsSelectorEventLoopPolicy guard that
# run_case.py/show_case.py/poller.py use: this entry point also runs
# run_supervisor(), which spawns a subprocess via the Claude Agent SDK
# (query() in supervisor.py) -- and on Windows, only the default
# ProactorEventLoop can create subprocesses; SelectorEventLoop can't.
# Live-verified: setting that policy here fixed SupervisorRoutingLog's
# psycopg crash but then broke the SDK subprocess with a *different*
# crash (NotImplementedError from asyncio's subprocess_exec). The two
# requirements can't both be satisfied by one process-wide loop policy,
# so SupervisorRoutingLog's own psycopg calls (db.py's `_run_pg`) run on
# a private worker-thread selector loop instead, and this entry point's
# main loop stays on Windows' default so the SDK subprocess still works.

from apm_orchestrator.config import load_settings
from apm_orchestrator.db import SupervisorRoutingLog
from apm_orchestrator.supervisor import run_supervisor
from apm_orchestrator.tools import aclose_client


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="apm-orchestrator",
        description="Run the APM Supervisor on a single prompt.",
    )
    parser.add_argument(
        "prompt",
        help="The request to route, e.g. 'Acme Corp emailed asking to renew their license.'",
    )
    args = parser.parse_args()

    async def _run() -> None:
        # Routing-confidence logging is opt-in: only when DATABASE_URL is
        # configured, same "unconfigured -> skip cleanly" pattern the rest
        # of this repo follows (e.g. get_client()'s connectors config).
        settings = load_settings()
        routing_log: SupervisorRoutingLog | None = None
        if settings.database_url:
            routing_log = SupervisorRoutingLog(settings.database_url)
            await routing_log.setup()

        try:
            print(await run_supervisor(args.prompt, routing_log=routing_log))
        finally:
            await aclose_client()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
