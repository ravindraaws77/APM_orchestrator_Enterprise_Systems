"""Unit tests for verify_account_node's name-matching fallback and the
past-due-but-open Opportunity flag -- both added after a real run
surfaced an exact-match SOQL query silently failing on a trailing period
in a real account name. Mocked ConnectorsClient, no live infra.
"""

from __future__ import annotations

import httpx
import pytest

from apm_orchestrator.agents.order_renewal.case_graph import record_outcome_node, verify_account_node
from apm_orchestrator.config import Settings
from apm_orchestrator.connectors_client import ConnectorsClient


def _client_with_handler(handler) -> ConnectorsClient:
    settings = Settings(
        connectors_base_url="http://testserver",
        connectors_api_key="test-key",
        anthropic_api_key=None,
        order_renewal_policy_path=None,
        database_url=None,
    )
    return ConnectorsClient(settings=settings, transport=httpx.MockTransport(handler))


def _opportunity(record_id, account_id, account_name, close_date, is_closed=False, name="Renewal"):
    return {
        "record_id": record_id,
        "object_type": "Opportunity",
        "fields": {
            "Name": name,
            "StageName": "Negotiation/Review",
            "CloseDate": close_date,
            "AccountId": account_id,
            "IsClosed": is_closed,
            "Account": {"Name": account_name},
        },
    }


@pytest.mark.asyncio
async def test_falls_back_and_tie_breaks_on_normalized_name(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # Exact match: nothing, because the real name has a trailing period.
            return httpx.Response(200, json=[])
        # Fallback LIKE query: the real account (trailing period) plus an
        # unrelated sibling whose name merely contains the same substring.
        return httpx.Response(
            200,
            json=[
                _opportunity("opp-real", "acc-real", "United Oil & Gas Corp.", "2026-10-01"),
                _opportunity("opp-uk", "acc-uk", "United Oil & Gas Corp (UK)", "2026-10-01"),
            ],
        )

    import apm_orchestrator.tools as tools_module

    monkeypatch.setattr(tools_module, "_client", _client_with_handler(handler))

    result = await verify_account_node(
        {"account_name": "United Oil & Gas Corp", "steps_completed": []}
    )

    assert result["opportunity"]["record_id"] == "opp-real"
    assert "verify_account" in result["steps_completed"]


@pytest.mark.asyncio
async def test_stops_on_ambiguous_match_across_different_accounts(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if b"= '" in request.content:
            return httpx.Response(200, json=[])
        # Two genuinely different accounts that both normalize to the
        # same target -- can't safely pick one.
        return httpx.Response(
            200,
            json=[
                _opportunity("opp-1", "acc-1", "Acme Corp.", "2026-10-01"),
                _opportunity("opp-2", "acc-2", "acme corp", "2026-10-01"),
            ],
        )

    import apm_orchestrator.tools as tools_module

    monkeypatch.setattr(tools_module, "_client", _client_with_handler(handler))

    result = await verify_account_node({"account_name": "Acme Corp", "steps_completed": []})

    assert "opportunity" not in result
    assert "different Salesforce accounts" in result["stop_reason"]
    assert result["steps_completed"][-1] == "verify_account:ambiguous_account"


@pytest.mark.asyncio
async def test_surfaces_stale_open_opportunities_without_stopping(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _opportunity("opp-current", "acc-1", "Acme Corp", "2026-10-01"),
                _opportunity(
                    "opp-stale", "acc-1", "Acme Corp", "2020-01-01", is_closed=False, name="Old deal"
                ),
                _opportunity(
                    "opp-closed-past", "acc-1", "Acme Corp", "2020-01-01", is_closed=True, name="Done"
                ),
            ],
        )

    import apm_orchestrator.tools as tools_module

    monkeypatch.setattr(tools_module, "_client", _client_with_handler(handler))

    result = await verify_account_node({"account_name": "Acme Corp", "steps_completed": []})

    assert result["opportunity"]["record_id"] == "opp-current"
    assert len(result["stale_opportunities"]) == 1
    assert "opp-stale" in result["stale_opportunities"][0]
    assert "opp-closed-past" not in result["stale_opportunities"][0]

    outcome = record_outcome_node(
        {
            "account_name": "Acme Corp",
            "stale_opportunities": result["stale_opportunities"],
        }
    )
    assert "outside this renewal's scope" in outcome["final_summary"]
    assert "opp-stale" in outcome["final_summary"]
