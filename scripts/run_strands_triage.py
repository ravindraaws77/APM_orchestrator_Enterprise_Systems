"""Run the Strands renewal-readiness triage agent once, against a real
apm_connectors server and the real Claude API.

Read-only apart from at most one *proposed* Jira ticket, which stays a
paused action until a human approves it against apm_connectors.

Usage:
    pip install -e ".[dev,postgres,strands]"
    python scripts/run_strands_triage.py "Acme Corp"

Needs ANTHROPIC_API_KEY and APM_CONNECTORS_BASE_URL (and
APM_CONNECTORS_API_KEY if the server requires one). No Postgres needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from apm_orchestrator.connectors_client import ConnectorsClient
from apm_orchestrator.strands_spike.agent import run_triage


async def _main(account_name: str) -> int:
    process_id = f"strands-triage-{uuid.uuid4().hex[:8]}"
    async with ConnectorsClient() as client:
        run = await run_triage(account_name, client, process_id=process_id)

    print(json.dumps(
        {
            "process_id": process_id,
            "stop_reason": run.stop_reason,
            "error": run.error,
            "report": run.report.model_dump() if run.report else None,
            "usage": run.usage,
            "audit": run.audit,
        },
        indent=2,
        default=str,
    ))
    return 0 if run.report else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("account_name")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.account_name)))


if __name__ == "__main__":
    main()
