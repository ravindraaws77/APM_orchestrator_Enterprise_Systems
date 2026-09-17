"""Unit tests for the Order-Renewal case graph's routing/policy logic --
mocked ConnectorsClient (httpx.MockTransport), MemorySaver checkpointer,
no live credentials, no Postgres, no running apm_connectors server.
Complements test_case_graph_mechanics.py, which proves the real
interrupt/Postgres/poller mechanism against live infra.
"""

from __future__ import annotations

import json

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import apm_orchestrator.tools as tools_module
from apm_orchestrator.agents.order_renewal.case_graph import build_case_graph
from apm_orchestrator.config import Settings
from apm_orchestrator.connectors_client import ConnectorsClient


def _install_fake_client(monkeypatch, handler):
    settings = Settings(
        connectors_base_url="http://testserver",
        connectors_api_key="test-key",
        anthropic_api_key=None,
        order_renewal_policy_path=None,
        database_url=None,
    )
    client = ConnectorsClient(settings=settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(tools_module, "_client", client)
    return client


def _config(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_id}}


@pytest.mark.asyncio
async def test_detect_stops_when_no_gmail_signal(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tools/gmail/search":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected call: {request.url.path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())

    result = await graph.ainvoke(
        {"case_id": "c1", "account_name": "Acme", "steps_completed": []}, config=_config("c1")
    )

    assert result["done"] is True
    assert "No renewal signal found" in result["final_summary"]


@pytest.mark.asyncio
async def test_verify_account_stops_outside_renewal_window(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tools/gmail/search":
            return httpx.Response(200, json=[{"message_id": "m1"}])
        if request.url.path == "/tools/gmail/read":
            return httpx.Response(200, json={"message_id": "m1", "sender": "buyer@acme.com"})
        if request.url.path == "/tools/salesforce/query":
            return httpx.Response(
                200,
                json=[
                    {
                        "record_id": "opp1",
                        "object_type": "Opportunity",
                        "fields": {"CloseDate": "2099-01-01"},
                    }
                ],
            )
        raise AssertionError(f"unexpected call: {request.url.path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())

    result = await graph.ainvoke(
        {"case_id": "c2", "account_name": "Acme", "steps_completed": []}, config=_config("c2")
    )

    assert result["done"] is True
    assert "outside the" in result["final_summary"]


@pytest.mark.asyncio
async def test_check_blockers_stops_on_matching_issue_type(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tools/gmail/search":
            return httpx.Response(200, json=[{"message_id": "m1"}])
        if request.url.path == "/tools/gmail/read":
            return httpx.Response(200, json={"message_id": "m1", "sender": "buyer@acme.com"})
        if request.url.path == "/tools/salesforce/query":
            return httpx.Response(
                200, json=[{"record_id": "opp1", "object_type": "Opportunity", "fields": {}}]
            )
        if request.url.path == "/tools/jira/search":
            return httpx.Response(
                200,
                json=[
                    # "Task", not "Escalation" -- policy.yaml's
                    # blocking_issue_types only lists "Task", the one
                    # issue type live-checked to actually exist in a
                    # real Jira site's configured work types.
                    {
                        "issue_key": "SUP-1",
                        "issue_type": "Task",
                        "fields": {"labels": []},
                    }
                ],
            )
        raise AssertionError(f"unexpected call: {request.url.path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())

    result = await graph.ainvoke(
        {"case_id": "c3", "account_name": "Acme", "steps_completed": []}, config=_config("c3")
    )

    assert result["done"] is True
    assert "Blocked by open ticket SUP-1" in result["final_summary"]


@pytest.mark.asyncio
async def test_full_happy_path_pauses_at_propose_call_then_rejection_stops_chain(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/tools/gmail/search":
            return httpx.Response(200, json=[{"message_id": "m1"}])
        if path == "/tools/gmail/read":
            return httpx.Response(200, json={"message_id": "m1", "sender": "buyer@acme.com"})
        if path == "/tools/salesforce/query":
            return httpx.Response(
                200, json=[{"record_id": "opp1", "object_type": "Opportunity", "fields": {}}]
            )
        if path == "/tools/jira/search":
            return httpx.Response(200, json=[])
        if path == "/tools/drive/list":
            return httpx.Response(200, json=[])
        if path == "/tools/calendar/create-event":
            return httpx.Response(
                200,
                json={
                    "action_id": "real-action-1",
                    "summary": None,
                    "pending_action": {
                        "type": "approval_request",
                        "action_id": "internal-record-id-999",  # deliberately different
                        "tool": "google_calendar",
                        "method": "create_event",
                        "description": "Create event",
                        "payload": {},
                        "category": "manual",
                        "proposed_by": None,
                    },
                    "final_result": None,
                },
            )
        raise AssertionError(f"unexpected call: {path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())
    config = _config("c4")

    result = await graph.ainvoke(
        {"case_id": "c4", "account_name": "Acme", "steps_completed": []}, config=config
    )

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["step"] == "call"
    # The critical regression this guards: the id to resume with is the
    # top-level action_id, never pending_action's own (different) id.
    assert payload["action_id"] == "real-action-1"
    assert payload["pending_action"]["action_id"] == "internal-record-id-999"

    snapshot = await graph.aget_state(config)
    assert snapshot.interrupts
    assert snapshot.values["action_id"] == "real-action-1"

    # A human rejects the call -> the chain stops, no further proposals.
    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "final_result": {"executed": False, "reason": "rejected"}}),
        config=config,
    )
    assert resumed["done"] is True
    assert "call" in resumed["final_summary"]
    assert "rejected" in resumed["final_summary"]
