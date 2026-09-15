"""Proves the interrupt -> Postgres checkpoint -> external resume
mechanism that agents/order_renewal/case_graph.py's `wait_for_approval`
depends on -- against a *real* Postgres checkpointer and a *real*
running apm_connectors server, not mocks.

Deliberately doesn't drive the full Order-Renewal graph end to end: that
needs live Gmail/Calendar/Salesforce/Jira credentials this environment
doesn't have. Instead it builds a minimal two-node graph (propose a real
Excel write -> the *same* `wait_for_approval_node` function the real
Order-Renewal graph uses) so the actual mechanism under test -- an
`interrupt()` surviving a real Postgres round-trip and resuming only
once a real apm_connectors action resolves -- is exercised for real.
The Order-Renewal-specific node logic (detect/verify/blockers/propose_*)
is covered separately by test_case_graph_nodes.py's mocked-client unit
tests, and by a real graceful-stop run confirmed manually against a live
server (see README).

Skipped automatically unless both APM_TEST_DATABASE_URL and a reachable
apm_connectors server (APM_CONNECTORS_BASE_URL) are configured -- same
convention apm_connectors' own test_postgres_state_store.py uses.
"""

from __future__ import annotations

import os
import uuid

from typing import Any, TypedDict

import httpx
import pytest
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from apm_orchestrator.agents.order_renewal.case_graph import wait_for_approval_node
from apm_orchestrator.config import Settings
from apm_orchestrator.connectors_client import ConnectorsClient

TEST_DATABASE_URL = os.environ.get("APM_TEST_DATABASE_URL")
BASE_URL = os.environ.get("APM_CONNECTORS_BASE_URL", "http://127.0.0.1:8123")
ORCH_KEY = os.environ.get("APM_CONNECTORS_API_KEY", "orch-key-123")
APPROVER_KEY = os.environ.get("APM_APPROVER_API_KEY", "approver-key-456")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="APM_TEST_DATABASE_URL not set -- skipping real-Postgres graph tests"
)


def _reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


class _TestState(TypedDict, total=False):
    case_id: str
    marker: str
    action_id: str | None
    pending_action: dict[str, Any] | None
    awaiting_step: str | None
    last_action_result: dict[str, Any] | None
    done: bool


async def _propose_node(state):
    client = ConnectorsClient(
        Settings(
            connectors_base_url=BASE_URL,
            connectors_api_key=ORCH_KEY,
            anthropic_api_key=None,
            order_renewal_policy_path=None,
            database_url=None,
        )
    )
    try:
        result = await client.excel_write_range(
            sheet_name="Renewals", address="D1:D1", values=[[state["marker"]]]
        )
    finally:
        await client.aclose()
    return {
        # RunOutcomeResponse's top-level action_id, NOT
        # result["pending_action"]["action_id"] -- see case_graph.py's
        # propose node comments, this is the same live-confirmed gotcha.
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "test_write",
    }


def _record_node(state):
    return {"done": True}


def _build_test_graph(checkpointer):
    graph = StateGraph(_TestState)
    graph.add_node("propose", _propose_node)
    graph.add_node("wait_for_approval", wait_for_approval_node)
    graph.add_node("record", _record_node)
    graph.set_entry_point("propose")
    graph.add_edge("propose", "wait_for_approval")
    graph.add_edge("wait_for_approval", "record")
    graph.add_edge("record", END)
    return graph.compile(checkpointer=checkpointer)


@pytest.mark.skipif(
    not _reachable(BASE_URL), reason=f"no apm_connectors server reachable at {BASE_URL}"
)
@pytest.mark.asyncio
async def test_interrupt_survives_real_postgres_and_resumes_on_real_approval():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    thread_id = f"mechanics-test-{uuid.uuid4()}"
    marker = f"marker-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    approver = ConnectorsClient(
        Settings(
            connectors_base_url=BASE_URL,
            connectors_api_key=APPROVER_KEY,
            anthropic_api_key=None,
            order_renewal_policy_path=None,
            database_url=None,
        )
    )
    reader = ConnectorsClient(
        Settings(
            connectors_base_url=BASE_URL,
            connectors_api_key=ORCH_KEY,
            anthropic_api_key=None,
            order_renewal_policy_path=None,
            database_url=None,
        )
    )

    async with AsyncPostgresSaver.from_conn_string(TEST_DATABASE_URL) as checkpointer:
        await checkpointer.setup()
        graph = _build_test_graph(checkpointer)

        # 1. Start: the graph really pauses, checkpointed in real Postgres.
        result = await graph.ainvoke(
            {"case_id": thread_id, "marker": marker}, config=config
        )
        assert "__interrupt__" in result
        action_id = result["__interrupt__"][0].value["action_id"]

        # 2. Confirm it's really paused, from a *fresh* read of Postgres
        # state (not the in-memory result above) -- proves the interrupt
        # actually persisted, not just survived in this process's memory.
        snapshot = await graph.aget_state(config)
        assert snapshot.interrupts
        assert snapshot.values["action_id"] == action_id

        # 3. Confirm apm_connectors itself has no status yet -- genuinely
        # still awaiting a human decision.
        assert await reader.get_action_status(action_id) is None

        # 4. A human (different API key) approves for real.
        decision = await approver.decide_action(action_id, approved=True)
        assert decision["final_result"]["executed"] is True

        # 5. Now apm_connectors reports it resolved.
        status = await reader.get_action_status(action_id)
        assert status is not None
        assert status["result"]["executed"] is True

        # 6. Resume the graph with that real outcome -- it actually finishes.
        resumed = await graph.ainvoke(
            Command(resume={"approved": True, "final_result": status["result"]}),
            config=config,
        )
        assert resumed.get("done") is True
        assert "__interrupt__" not in resumed

    # 7. And the real workbook cell really has the marker we wrote.
    after = await reader.excel_read_range(sheet_name="Renewals", address="D1:D1")
    assert after["values"] == [[marker]]

    await approver.aclose()
    await reader.aclose()
