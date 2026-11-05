"""Strands triage spike: the full Strands agent loop, driven by a
scripted fake model and a stub connectors client. No LLM, no network."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest

pytest.importorskip("strands")
pytest.importorskip("langgraph")

from strands.models import Model  # noqa: E402

from apm_orchestrator.agents.order_renewal.policy import load_policy  # noqa: E402
from apm_orchestrator.config import Settings  # noqa: E402
from apm_orchestrator.strands_spike.agent import (  # noqa: E402
    RenewalTriageReport,
    build_model,
    build_triage_agent,
    run_triage,
)
from apm_orchestrator.strands_spike.guardrails import GuardrailLimits  # noqa: E402
from apm_orchestrator.strands_spike.toolbelt import TOOLBELT, build_toolbelt  # noqa: E402

POLICY = load_policy()
SETTINGS = Settings(
    connectors_base_url="http://unused",
    connectors_api_key=None,
    anthropic_api_key=None,
    order_renewal_policy_path=None,
    database_url=None,
)


class ScriptedModel(Model):
    """Replays a fixed list of turns. A turn is either plain text or a
    (tool_name, input) tool call. Records the tool names offered each call."""

    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.offered_tools: list[list[str]] = []

    def update_config(self, **_: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {}

    async def structured_output(self, *_: Any, **__: Any):  # pragma: no cover - not used by the agent loop
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **_: Any):
        self.offered_tools.append([spec["name"] for spec in (tool_specs or [])])
        turn = self.turns.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(turn, str):
            yield {"contentBlockDelta": {"delta": {"text": turn}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            name, tool_input = turn
            tool_use_id = f"t{len(self.offered_tools)}"
            yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": tool_use_id}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}, "metrics": {"latencyMs": 1}}}


class StubClient:
    def __init__(self, *, exact=None, fallback=None, issues=None) -> None:
        self.exact = exact or []
        self.fallback = fallback or []
        self.issues = issues or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def salesforce_query_records(self, soql: str, process_id=None):
        self.calls.append(("salesforce_query_records", {"soql": soql, "process_id": process_id}))
        return self.exact if "LIKE" not in soql else self.fallback

    async def jira_search_issues(self, jql: str, max_results=None, process_id=None):
        self.calls.append(("jira_search_issues", {"jql": jql}))
        return self.issues

    async def jira_create_issue(self, fields: dict[str, Any], process_id=None):
        self.calls.append(("jira_create_issue", {"fields": fields}))
        return {
            "action_id": "real-top-level-id",
            "status": "pending_approval",
            "pending_action": {"action_id": "internal-store-id", "tool": "jira"},
        }

    async def get_action_status(self, action_id: str):
        self.calls.append(("get_action_status", {"action_id": action_id}))
        return None

    async def decide_action(self, action_id: str, approved: bool):  # pragma: no cover - must never be reached
        raise AssertionError("an agent must never decide its own action")


def _opp(record_id: str, days_from_today: int, account="Acme Corp.", account_id="001A") -> dict[str, Any]:
    return {
        "record_id": record_id,
        "fields": {
            "Name": f"Renewal {record_id}",
            "StageName": "Negotiation",
            "CloseDate": (date.today() + timedelta(days=days_from_today)).isoformat(),
            "Amount": 1000,
            "AccountId": account_id,
            "IsClosed": False,
            "Account": {"Name": account},
        },
    }


def _report(**overrides: Any) -> tuple[str, dict[str, Any]]:
    body = {
        "account_name": "Acme Corp",
        "recommendation": "hold_for_blockers",
        "opportunity_id": "006NEAR",
        "blocking_ticket_keys": ["KAN-7"],
        "proposed_follow_up_action_id": "real-top-level-id",
        "rationale": "KAN-7 is an open Task about this account.",
    }
    body.update(overrides)
    return (RenewalTriageReport.__name__, body)


async def test_happy_path_blocked_renewal_proposes_one_ticket():
    client = StubClient(
        exact=[_opp("006OLD", -400), _opp("006NEAR", 20)],
        issues=[{"issue_key": "KAN-7", "issue_type": "Task", "fields": {"summary": "Acme Corp outage", "labels": []}}],
    )
    model = ScriptedModel(
        [
            ("lookup_renewal_opportunity", {"account_name": "Acme Corp"}),
            ("find_blocking_tickets", {"account_name": "Acme Corp"}),
            ("propose_follow_up_ticket", {"account_name": "Acme Corp", "summary": "Resolve KAN-7", "description": "x"}),
            _report(),
        ]
    )

    run = await run_triage("Acme Corp", client, model=model, settings=SETTINGS, policy=POLICY)

    assert run.error is None
    assert run.report.recommendation == "hold_for_blockers"
    assert run.report.proposed_follow_up_action_id == "real-top-level-id"
    create = next(c for name, c in client.calls if name == "jira_create_issue")
    assert create["fields"]["project"] == {"key": POLICY.route_for(None)}
    assert create["fields"]["issuetype"] == {"name": POLICY.follow_up_issue_type}
    assert [a["name"] for a in run.audit if a["kind"] == "tool"] == [
        "lookup_renewal_opportunity",
        "find_blocking_tickets",
        "propose_follow_up_ticket",
        RenewalTriageReport.__name__,
    ]
    assert all(a["allowed"] for a in run.audit)
    assert run.usage["totalTokens"] == 4 * 120


async def test_only_the_toolbelt_is_offered_and_no_decision_tool_exists():
    model = ScriptedModel([_report()])
    await run_triage("Acme Corp", StubClient(), model=model, settings=SETTINGS, policy=POLICY)
    offered = set(model.offered_tools[0])
    assert offered == TOOLBELT | {RenewalTriageReport.__name__}
    assert not any("decide" in name or "decision" in name for name in offered)


async def test_guard_refuses_a_tool_outside_the_toolbelt():
    client = StubClient()
    model = ScriptedModel(
        [("decide_action", {"action_id": "x", "approved": True}), _report(recommendation="needs_human_review")]
    )
    run = await run_triage("Acme Corp", client, model=model, settings=SETTINGS, policy=POLICY)

    refused = [a for a in run.audit if not a["allowed"]]
    assert refused and refused[0]["name"] == "decide_action"
    assert client.calls == []


async def test_second_proposal_in_one_run_is_refused():
    client = StubClient()
    propose = ("propose_follow_up_ticket", {"account_name": "Acme Corp", "summary": "s", "description": "d"})
    model = ScriptedModel([propose, propose, _report()])
    run = await run_triage("Acme Corp", client, model=model, settings=SETTINGS, policy=POLICY)

    assert sum(1 for name, _ in client.calls if name == "jira_create_issue") == 1
    assert [a["allowed"] for a in run.audit if a["name"] == "propose_follow_up_ticket"] == [True, False]


async def test_model_call_limit_stops_the_run_without_a_report():
    lookup = ("lookup_renewal_opportunity", {"account_name": "Acme Corp"})
    model = ScriptedModel([lookup, lookup, lookup, _report()])
    run = await run_triage(
        "Acme Corp",
        StubClient(exact=[_opp("006NEAR", 20)]),
        model=model,
        settings=SETTINGS,
        policy=POLICY,
        limits=GuardrailLimits(max_model_calls=2),
    )

    assert run.report is None
    assert any(a["kind"] == "model" and not a["allowed"] for a in run.audit)


def _tool(client: StubClient, name: str):
    return next(t for t in build_toolbelt(client, POLICY) if t.tool_name == name)


async def test_lookup_picks_closest_opportunity_and_flags_window():
    client = StubClient(exact=[_opp("006OLD", -400), _opp("006NEAR", -5), _opp("006FAR", 300)])
    result = await _tool(client, "lookup_renewal_opportunity")(account_name="Acme Corp")
    assert result["opportunity_id"] == "006NEAR"
    assert result["days_to_close"] == -5
    assert result["in_renewal_window"] is True
    assert result["stale_open_opportunities"]  # 006OLD and 006NEAR are past due and still open


async def test_lookup_fallback_reports_ambiguous_accounts_instead_of_guessing():
    client = StubClient(
        exact=[],
        fallback=[_opp("006A", 10, "Acme Corp", "001A"), _opp("006B", 10, "acme corp.", "001B")],
    )
    result = await _tool(client, "lookup_renewal_opportunity")(account_name="Acme Corp")
    assert result["status"] == "ambiguous_account"


async def test_lookup_escapes_soql_and_blockers_escape_jql():
    client = StubClient(exact=[_opp("006NEAR", 10)])
    await _tool(client, "lookup_renewal_opportunity")(account_name="O'Brien \"Ltd\"")
    await _tool(client, "find_blocking_tickets")(account_name='Say "hi"')
    assert "O\\'Brien" in client.calls[0][1]["soql"]
    assert 'Say \\"hi\\"' in client.calls[1][1]["jql"]


async def test_proposal_result_hides_the_internal_action_id():
    result = await _tool(StubClient(), "propose_follow_up_ticket")(account_name="A", summary="s", description="d")
    assert result["action_id"] == "real-top-level-id"
    assert "action_id" not in result["pending_action"]


def test_model_is_pinned_from_settings():
    pytest.importorskip("anthropic")
    model = build_model(SETTINGS)
    assert model.get_config()["model_id"] == SETTINGS.business_agent_model
    assert model.get_config()["max_tokens"] > 0


def test_agent_registers_the_guard():
    agent, guard = build_triage_agent(StubClient(), model=ScriptedModel([]), settings=SETTINGS, policy=POLICY)
    assert set(agent.tool_names) == TOOLBELT
    assert guard.limits.max_proposals == 1
