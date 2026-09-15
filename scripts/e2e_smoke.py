"""End-to-end smoke test against a real, running apm_connectors server --
no mocks, no fake transport. Proves the wiring this repo depends on:
this repo's `ConnectorsClient` really talks HTTP to a really running
apm_connectors process, a proposed write really pauses until a human
approves it, and the audit trail really attributes proposer vs. approver
as two independent identities.

Exercises the one connector that needs no external credentials at all
(local Excel) so this is runnable with zero live Gmail/Salesforce/Jira
accounts -- see apm_connectors' docs/running-locally.md. Also confirms
an unconfigured connector (Salesforce here) degrades to a clean error
rather than a crash, exactly as apm_connectors' docs/api-contract.md
documents (503 before anything is recorded).

Setup (one-time), from a shell that can reach the target server:
  1. In apm_connectors: set APM_EXCEL_WORKBOOK_PATH to a real .xlsx file
     and APM_API_KEYS to at least two callers, e.g.
       APM_API_KEYS="orchestrator:orch-key,human-approver:approver-key"
     then: uvicorn apm_connectors.api.app:app --port 8123
  2. pip install -e ".[dev]" in this repo.

Usage:
  python scripts/e2e_smoke.py \\
      --base-url http://127.0.0.1:8123 \\
      --orchestrator-key orch-key \\
      --approver-key approver-key
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from apm_orchestrator.config import Settings
from apm_orchestrator.connectors_client import ConnectorError, ConnectorsClient


async def main(base_url: str, orchestrator_key: str, approver_key: str) -> int:
    orchestrator = ConnectorsClient(
        Settings(
            connectors_base_url=base_url,
            connectors_api_key=orchestrator_key,
            anthropic_api_key=None,
            order_renewal_policy_path=None,
            database_url=None,
        )
    )
    approver = ConnectorsClient(
        Settings(
            connectors_base_url=base_url,
            connectors_api_key=approver_key,
            anthropic_api_key=None,
            order_renewal_policy_path=None,
            database_url=None,
        )
    )
    ok = True

    try:
        sheets = await orchestrator.excel_list_worksheets()
        print(f"[ OK ] excel_list_worksheets (read) -> {sheets}")

        before = await orchestrator.excel_read_range(sheet_name="Renewals", address="A2:B2")
        print(f"[ OK ] excel_read_range A2:B2 before proposing -> {before['values']}")

        # Toggle the value each run so a re-run against the same workbook
        # still proves something actually changed, rather than coincidentally
        # matching the previous run's already-written value.
        new_stage = "Renewed" if before["values"] == [["Acme Corp", "Closed Won"]] else "Closed Won"
        new_row = [["Acme Corp", new_stage]]

        write_result = await orchestrator.excel_write_range(
            sheet_name="Renewals", address="A2:B2", values=new_row
        )
        action_id = write_result["action_id"]
        assert write_result["final_result"] is None, "write executed without approval!"
        assert write_result["pending_action"] is not None
        print(f"[ OK ] excel_write_range proposed, NOT executed -> action_id={action_id}")

        still_before = await orchestrator.excel_read_range(
            sheet_name="Renewals", address="A2:B2"
        )
        assert still_before["values"] == before["values"], (
            f"row A2:B2 changed before approval: {before['values']} -> {still_before['values']}"
        )
        print("[ OK ] confirmed nothing written to the real workbook pre-approval")

        decision = await approver.decide_action(action_id, approved=True)
        assert decision["final_result"]["executed"] is True
        print(f"[ OK ] human-approver key approved action -> executed=True")

        after = await orchestrator.excel_read_range(sheet_name="Renewals", address="A2:B2")
        assert after["values"] == new_row
        print(f"[ OK ] excel_read_range after approval -> {after['values']} (really written)")

        async with httpx.AsyncClient(base_url=base_url) as raw:
            history = await raw.get(
                f"/processes/{action_id}/history",
                headers={"Authorization": f"Bearer {orchestrator_key}"},
            )
        events = history.json()
        proposed = next(e for e in events if e["event_type"] == "action_proposed")
        approved = next(e for e in events if e["event_type"] == "action_approved")
        assert proposed["caller"] == "orchestrator"
        assert approved["caller"] == "human-approver"
        print(
            "[ OK ] audit trail attributes proposer/approver as independent identities: "
            f"action_proposed.caller={proposed['caller']!r} "
            f"action_approved.caller={approved['caller']!r}"
        )

    except (ConnectorError, AssertionError, StopIteration) as exc:
        print(f"[FAIL] {exc}")
        ok = False

    try:
        await orchestrator.salesforce_query_records(soql="SELECT Id FROM Opportunity LIMIT 1")
        print("[FAIL] salesforce_query_records succeeded but no Salesforce is configured")
        ok = False
    except ConnectorError as exc:
        print(f"[ OK ] salesforce_query_records degrades cleanly (unconfigured): {exc}")

    await orchestrator.aclose()
    await approver.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8123")
    parser.add_argument("--orchestrator-key", required=True)
    parser.add_argument("--approver-key", required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.base_url, args.orchestrator_key, args.approver_key)))
