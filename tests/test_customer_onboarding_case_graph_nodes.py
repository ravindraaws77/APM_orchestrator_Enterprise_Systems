"""Unit tests for the Customer-Onboarding case graph's routing/policy
logic -- mocked ConnectorsClient (httpx.MockTransport), MemorySaver
checkpointer, no live credentials, no Postgres, no running
apm_connectors server. Same style as
tests/test_case_graph_nodes.py (order_renewal's equivalent).
"""

from __future__ import annotations

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import apm_orchestrator.tools as tools_module
from apm_orchestrator.agents.customer_onboarding.case_graph import build_case_graph
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


def _opportunity(record_id="opp1", account_id="acc1", close_date="2026-09-10"):
    return {
        "record_id": record_id,
        "object_type": "Opportunity",
        "fields": {"AccountId": account_id, "CloseDate": close_date, "Type": "New Business"},
    }


def _contact(email="buyer@acme.com"):
    return {"record_id": "con1", "object_type": "Contact", "fields": {"Email": email}}


@pytest.mark.asyncio
async def test_detect_stops_when_no_new_business_opportunity(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tools/salesforce/query":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected call: {request.url.path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())

    result = await graph.ainvoke(
        {"case_id": "c1", "account_name": "Acme", "steps_completed": []}, config=_config("c1")
    )

    assert result["done"] is True
    assert "No new-business" in result["final_summary"]


@pytest.mark.asyncio
async def test_detect_stops_outside_onboarding_window(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tools/salesforce/query":
            return httpx.Response(200, json=[_opportunity(close_date="2020-01-01")])
        raise AssertionError(f"unexpected call: {request.url.path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())

    result = await graph.ainvoke(
        {"case_id": "c2", "account_name": "Acme", "steps_completed": []}, config=_config("c2")
    )

    assert result["done"] is True
    assert "outside the" in result["final_summary"]


@pytest.mark.asyncio
async def test_verify_contact_stops_when_no_email(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path == "/tools/salesforce/query":
            if calls["n"] == 1:
                return httpx.Response(200, json=[_opportunity()])
            return httpx.Response(200, json=[{"record_id": "con1", "fields": {}}])
        raise AssertionError(f"unexpected call: {request.url.path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())

    result = await graph.ainvoke(
        {"case_id": "c3", "account_name": "Acme", "steps_completed": []}, config=_config("c3")
    )

    assert result["done"] is True
    assert "No Salesforce Contact with an email" in result["final_summary"]


@pytest.mark.asyncio
async def test_check_blockers_stops_on_existing_onboarding_ticket(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path == "/tools/salesforce/query":
            if calls["n"] == 1:
                return httpx.Response(200, json=[_opportunity()])
            return httpx.Response(200, json=[_contact()])
        if request.url.path == "/tools/jira/search":
            return httpx.Response(
                200,
                json=[{"issue_key": "OPS-1", "issue_type": "Onboarding", "fields": {"labels": []}}],
            )
        raise AssertionError(f"unexpected call: {request.url.path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())

    result = await graph.ainvoke(
        {"case_id": "c4", "account_name": "Acme", "steps_completed": []}, config=_config("c4")
    )

    assert result["done"] is True
    assert "already tracked by OPS-1" in result["final_summary"]


@pytest.mark.asyncio
async def test_full_happy_path_pauses_through_all_four_approvals_then_completes(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        path = request.url.path
        if path == "/tools/salesforce/query":
            if calls["n"] == 1:
                return httpx.Response(200, json=[_opportunity()])
            return httpx.Response(200, json=[_contact()])
        if path == "/tools/jira/search":
            return httpx.Response(200, json=[])
        if path == "/tools/drive/list":
            return httpx.Response(200, json=[])
        if path == "/tools/calendar/create-event":
            return httpx.Response(
                200,
                json={
                    "action_id": "action-call",
                    "pending_action": {"action_id": "internal-call", "tool": "google_calendar"},
                    "final_result": None,
                },
            )
        if path == "/tools/gmail/send":
            return httpx.Response(
                200,
                json={
                    "action_id": "action-notice",
                    "pending_action": {"action_id": "internal-notice", "tool": "gmail"},
                    "final_result": None,
                },
            )
        if path == "/tools/jira/create":
            return httpx.Response(
                200,
                json={
                    "action_id": "action-ticket",
                    "pending_action": {"action_id": "internal-ticket", "tool": "jira"},
                    "final_result": None,
                },
            )
        if path == "/tools/salesforce/update":
            return httpx.Response(
                200,
                json={
                    "action_id": "action-record",
                    "pending_action": {"action_id": "internal-record", "tool": "salesforce"},
                    "final_result": None,
                },
            )
        raise AssertionError(f"unexpected call: {path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())
    config = _config("c5")

    result = await graph.ainvoke(
        {"case_id": "c5", "account_name": "Acme", "steps_completed": []}, config=config
    )
    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["step"] == "kickoff_call"
    # The critical regression this guards, same as order_renewal's own
    # test: the id to resume with is the top-level action_id, never
    # pending_action's own (different) id.
    assert payload["action_id"] == "action-call"

    for expected_step, expected_action_id in [
        ("welcome_notice", "action-notice"),
        ("onboarding_ticket", "action-ticket"),
        ("record_update", "action-record"),
    ]:
        result = await graph.ainvoke(
            Command(resume={"approved": True, "final_result": {"executed": True}}), config=config
        )
        assert "__interrupt__" in result
        payload = result["__interrupt__"][0].value
        assert payload["step"] == expected_step
        assert payload["action_id"] == expected_action_id

    result = await graph.ainvoke(
        Command(resume={"approved": True, "final_result": {"executed": True}}), config=config
    )
    assert result["done"] is True
    assert "Onboarding workflow completed for Acme" in result["final_summary"]


@pytest.mark.asyncio
async def test_rejection_at_kickoff_call_stops_the_chain(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        path = request.url.path
        if path == "/tools/salesforce/query":
            if calls["n"] == 1:
                return httpx.Response(200, json=[_opportunity()])
            return httpx.Response(200, json=[_contact()])
        if path == "/tools/jira/search":
            return httpx.Response(200, json=[])
        if path == "/tools/drive/list":
            return httpx.Response(200, json=[])
        if path == "/tools/calendar/create-event":
            return httpx.Response(
                200,
                json={
                    "action_id": "action-call",
                    "pending_action": {"action_id": "internal-call", "tool": "google_calendar"},
                    "final_result": None,
                },
            )
        raise AssertionError(f"unexpected call: {path}")

    _install_fake_client(monkeypatch, handler)
    graph = build_case_graph(MemorySaver())
    config = _config("c6")

    await graph.ainvoke(
        {"case_id": "c6", "account_name": "Acme", "steps_completed": []}, config=config
    )
    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "final_result": {"executed": False, "reason": "rejected"}}),
        config=config,
    )
    assert resumed["done"] is True
    assert "kickoff_call" in resumed["final_summary"]
    assert "rejected" in resumed["final_summary"]
