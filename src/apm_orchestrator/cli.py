"""Manual smoke test: run the Supervisor on one prompt from the command
line. Not a service -- Phase 1's shared task/conversation state
(Postgres-backed multi-step tracking) isn't built yet, so each
invocation is a single, independent turn.
"""

from __future__ import annotations

import argparse
import asyncio

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
        try:
            print(await run_supervisor(args.prompt))
        finally:
            await aclose_client()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
