"""Manual smoke test: run the Supervisor on one prompt from the command
line. Not a service -- Phase 1's shared task/conversation state
(Postgres-backed multi-step tracking) isn't built yet, so each
invocation is a single, independent turn.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# psycopg's async mode needs a selector-based event loop; Windows'
# asyncio default (ProactorEventLoop) isn't compatible with it and fails
# at connection time with "Psycopg cannot use the 'ProactorEventLoop'"
# -- live-verified (this entry point didn't touch Postgres before
# SupervisorRoutingLog's routing_log wiring landed, so it never hit
# this until then), same fix as run_case.py/show_case.py/poller.py. No
# effect on other platforms.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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
