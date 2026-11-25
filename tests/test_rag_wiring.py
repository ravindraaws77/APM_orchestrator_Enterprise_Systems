"""Phase-2 RAG wiring, offline: the case graph's extract_contract_terms
node (U1), the read-only contract_qa SDK tool and the Strands
ask_contract tool (U2), policy and config. Mocked apm_connectors
(httpx.MockTransport), MemorySaver, fake answer model -- no network,
no LLM."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import replace
from typing import Any

import httpx
import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

import apm_orchestrator.agents.order_renewal.case_graph as case_graph  # noqa: E402
import apm_orchestrator.tools as tools_module  # noqa: E402
from apm_orchestrator.agents.order_renewal.agent import REPORTING_TOOLS, WORKFLOW_TOOLS  # noqa: E402
from apm_orchestrator.agents.order_renewal.policy import (  # noqa: E402
    ContractTermsPolicy,
    OrderRenewalPolicy,
    load_policy,
)
from apm_orchestrator.config import Settings, load_settings  # noqa: E402
from apm_orchestrator.connectors_client import ConnectorsClient  # noqa: E402
from apm_orchestrator.rag import runtime as rag_runtime  # noqa: E402
from apm_orchestrator.rag.answer import ModelResponse  # noqa: E402
from apm_orchestrator.rag.contract_terms import DEFAULT_TERM_QUESTIONS  # noqa: E402
from apm_orchestrator.rag.grounding import normalise  # noqa: E402

CONTRACT = (
    "MASTER SUBSCRIPTION AGREEMENT - Acme Corp.\n\n"
    "2. Renewal. This Agreement renews automatically for successive renewal terms of twelve (12) months "
    "unless either party gives written notice of non-renewal at least ninety (90) days before the end of "
    "the then-current term.\n\n"
    "3. Fees. Fees for any renewal term may increase by no more than five percent (5%).\n"
)
NOTICE_QUOTE = "written notice of non-renewal at least ninety (90) days before the end of the then-current term"

CALENDAR_OK = {
    "action_id": "real-action-1",
    "summary": None,
    "pending_action": {"type": "approval_request", "action_id": "internal-x", "tool": "google_calendar", "payload": {}},
    "final_result": None,
}


class FakeModel:
    def __init__(self, reply=None, exc: Exception | None = None) -> None:
        self.reply, self.exc, self.calls = reply, exc, 0

    async def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> ModelResponse:
        self.calls += 1
        if self.exc:
            raise self.exc
        return ModelResponse(data=self.reply(prompt) if callable(self.reply) else self.reply)


def _chunk_id_for(prompt: str, quote: str) -> str:
    # Find the excerpt in the prompt whose text holds the quote.
    for block in prompt.split("<excerpt ")[1:]:
        cid = block.split('chunk_id="', 1)[1].split('"', 1)[0]
        if normalise(quote) in normalise(block):
            return cid
    raise AssertionError("quote not in any excerpt")


def notice_only(prompt: str) -> dict[str, Any]:
    """Grounds the notice-period question; says 'not found' to the rest."""
    question = prompt.rsplit("<question>", 1)[1].lower()
    if "notice period" in question:
        return {
            "answer_found": True,
            "answer": "90 days before term end",
            "citations": [{"chunk_id": _chunk_id_for(prompt, NOTICE_QUOTE), "quote": NOTICE_QUOTE}],
        }
    return {"answer_found": False, "answer": "", "citations": []}


def _settings() -> Settings:
    return Settings(
        connectors_base_url="http://testserver",
        connectors_api_key="k",
        anthropic_api_key=None,
        order_renewal_policy_path=None,
        database_url=None,
    )


def _handler(drive_files: list[dict[str, Any]], seen: list[tuple[str, dict]], drive_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        seen.append((path, body))
        if path == "/tools/gmail/search":
            return httpx.Response(200, json=[{"message_id": "m1"}])
        if path == "/tools/gmail/read":
            return httpx.Response(200, json={"message_id": "m1", "sender": "buyer@acme.com"})
        if path == "/tools/salesforce/query":
            return httpx.Response(200, json=[{"record_id": "opp1", "object_type": "Opportunity", "fields": {}}])
        if path == "/tools/jira/search":
            return httpx.Response(200, json=[])
        if path == "/tools/drive/list":
            if drive_status != 200:
                return httpx.Response(drive_status, json={"detail": "drive down"})
            return httpx.Response(200, json=drive_files)
        if path == "/tools/drive/read":
            return httpx.Response(
                200,
                json={
                    "file_id": body["file_id"],
                    "name": "Acme MSA.txt",
                    "mime_type": "text/plain",
                    "size": len(CONTRACT),
                    "content_base64": base64.b64encode(CONTRACT.encode()).decode(),
                },
            )
        if path == "/tools/calendar/create-event":
            return httpx.Response(200, json=CALENDAR_OK)
        raise AssertionError(f"unexpected call: {path}")

    return handler


ACME_FILES = [{"file_id": "f1", "name": "Acme MSA.txt", "mime_type": "text/plain"}]


@pytest.fixture
def seen() -> list[tuple[str, dict]]:
    return []


def _install(monkeypatch, handler, model) -> None:
    client = ConnectorsClient(settings=_settings(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(tools_module, "_client", client)
    monkeypatch.setattr(rag_runtime, "_model", model)  # restored after the test


async def _run_to_call(case_id: str) -> dict[str, Any]:
    graph = case_graph.build_case_graph(MemorySaver())
    result = await graph.ainvoke(
        {"case_id": case_id, "account_name": "Acme", "steps_completed": []},
        config={"configurable": {"thread_id": case_id}},
    )
    assert "__interrupt__" in result, "the case must still reach propose_call"
    return result


# -- U1: extract_contract_terms node ------------------------------------------


async def test_cited_terms_travel_with_the_approval_wait(monkeypatch, seen):
    model = FakeModel(reply=notice_only)
    _install(monkeypatch, _handler(ACME_FILES, seen), model)
    result = await _run_to_call("t1")

    payload = result["__interrupt__"][0].value
    assert payload["step"] == "call" and payload["action_id"] == "real-action-1"
    summary = payload["contract_terms_summary"]
    assert "Notice period: 90 days before term end [source: Acme MSA.txt p.1]" in summary
    assert "Renewal term: not found in contract documents" in summary
    assert model.calls == len(DEFAULT_TERM_QUESTIONS)

    assert "extract_contract_terms:1/4_grounded" in result["steps_completed"]
    assert result["contract_terms"]["notice_period"]["status"] == "grounded"
    # The proposal itself is unchanged: same fields as before this node existed.
    (cal_body,) = [b for p, b in seen if p == "/tools/calendar/create-event"]
    assert set(cal_body) <= {"process_id", "title", "start", "end", "attendees", "location"}
    # Read-only: the node only listed and read Drive files.
    assert {p for p, _ in seen if p.startswith("/tools/drive/")} == {"/tools/drive/list", "/tools/drive/read"}
    assert case_graph._to_outcome("t1", result).contract_terms_summary == summary


async def test_unverified_term_is_never_shown_as_a_value(monkeypatch, seen):
    fabricated = {
        "answer_found": True,
        "answer": "30 days",
        "citations": [{"chunk_id": "f1#p1c0", "quote": "written notice at least thirty (30) days"}],
    }
    _install(monkeypatch, _handler(ACME_FILES, seen), FakeModel(reply=fabricated))
    result = await _run_to_call("t2")
    summary = result["contract_terms_summary"]
    assert "30 days" not in summary
    assert "Notice period: needs human review" in summary
    assert "extract_contract_terms:0/4_grounded" in result["steps_completed"]


async def test_no_contract_skips_without_calling_model(monkeypatch, seen):
    model = FakeModel(reply=notice_only)
    _install(monkeypatch, _handler([], seen), model)
    result = await _run_to_call("t3")
    assert "extract_contract_terms:skipped_no_contract" in result["steps_completed"]
    assert model.calls == 0
    assert result["__interrupt__"][0].value["contract_terms_summary"] is None


async def test_drive_unavailable_continues(monkeypatch, seen):
    model = FakeModel(reply=notice_only)
    _install(monkeypatch, _handler(ACME_FILES, seen, drive_status=503), model)
    result = await _run_to_call("t4")
    assert "pull_contract:unavailable" in result["steps_completed"]
    assert "extract_contract_terms:skipped_no_contract" in result["steps_completed"]
    assert model.calls == 0


async def test_unexpected_failure_is_logged_and_case_continues(monkeypatch, seen, caplog):
    _install(monkeypatch, _handler(ACME_FILES, seen), FakeModel(reply=notice_only))

    def boom():
        raise RuntimeError("no API credentials")

    monkeypatch.setattr(rag_runtime, "get_answer_model", boom)
    with caplog.at_level(logging.WARNING, logger="apm_orchestrator.case_graph"):
        result = await _run_to_call("t5")
    assert "extract_contract_terms:error" in result["steps_completed"]
    assert "no API credentials" in caplog.text
    assert "stop_reason" not in result or result.get("stop_reason") is None


async def test_model_errors_become_review_statuses_not_failures(monkeypatch, seen):
    _install(monkeypatch, _handler(ACME_FILES, seen), FakeModel(exc=TimeoutError("slow")))
    result = await _run_to_call("t6")
    assert all(t["status"] == "needs_human_review" for t in result["contract_terms"].values())
    assert "extract_contract_terms:0/4_grounded" in result["steps_completed"]


async def test_disabled_in_policy(monkeypatch, seen):
    model = FakeModel(reply=notice_only)
    _install(monkeypatch, _handler(ACME_FILES, seen), model)
    base = load_policy()
    disabled = OrderRenewalPolicy(raw={**base.raw, "contract_terms": {**base.raw["contract_terms"], "enabled": False}})
    monkeypatch.setattr(case_graph, "load_policy", lambda *a, **k: disabled)
    result = await _run_to_call("t7")
    assert "extract_contract_terms:disabled" in result["steps_completed"]
    assert model.calls == 0


async def test_terms_are_appended_to_the_final_summary(monkeypatch, seen):
    from langgraph.types import Command

    _install(monkeypatch, _handler(ACME_FILES, seen), FakeModel(reply=notice_only))
    graph = case_graph.build_case_graph(MemorySaver())
    config = {"configurable": {"thread_id": "t8"}}
    await graph.ainvoke({"case_id": "t8", "account_name": "Acme", "steps_completed": []}, config=config)
    done = await graph.ainvoke(Command(resume={"approved": False, "final_result": None}), config=config)
    assert done["done"] and "Notice period: 90 days before term end" in done["final_summary"]


# -- U2: contract_qa SDK tool and Strands ask_contract --------------------------


async def test_contract_qa_tool_is_read_only_and_grounded(monkeypatch, seen):
    _install(monkeypatch, _handler(ACME_FILES, seen), FakeModel(reply=notice_only))
    out = await tools_module.contract_qa.handler(
        {"question": "What notice period is required for non-renewal?", "name_contains": "Acme"}
    )
    data = json.loads(out["content"][0]["text"])
    assert data["status"] == "grounded" and data["answer"] == "90 days before term end"
    assert data["citations"][0]["doc_name"] == "Acme MSA.txt" and data["citations"][0]["page"] == 1
    assert {p for p, _ in seen} == {"/tools/drive/list", "/tools/drive/read"}
    assert seen[0][1]["name_contains"] == "Acme"


async def test_contract_qa_tool_reports_connector_errors(monkeypatch, seen):
    _install(monkeypatch, _handler(ACME_FILES, seen, drive_status=503), FakeModel(reply=notice_only))
    out = await tools_module.contract_qa.handler({"question": "notice period?"})
    assert out.get("is_error") is True


def test_contract_qa_is_in_the_workflow_toolbelt_only():
    assert tools_module.contract_qa in tools_module.ALL_TOOLS
    assert "mcp__apm_connectors__contract_qa" in WORKFLOW_TOOLS
    assert "mcp__apm_connectors__contract_qa" not in REPORTING_TOOLS


async def test_strands_ask_contract_is_opt_in_and_allowlisted(seen):
    pytest.importorskip("strands")
    from apm_orchestrator.strands_spike.agent import build_triage_agent
    from apm_orchestrator.strands_spike.toolbelt import build_toolbelt

    client = ConnectorsClient(settings=_settings(), transport=httpx.MockTransport(_handler(ACME_FILES, seen)))
    policy = load_policy()
    assert "ask_contract" not in {t.tool_name for t in build_toolbelt(client, policy)}

    model = FakeModel(reply=notice_only)
    tools = {t.tool_name: t for t in build_toolbelt(client, policy, answer_model=model)}
    out = await tools["ask_contract"](account_name="Acme", question="What notice period is required for non-renewal?")
    assert out["status"] == "grounded"
    assert {p for p, _ in seen} == {"/tools/drive/list", "/tools/drive/read"}

    from tests.test_strands_triage import ScriptedModel  # never invoked: building an agent calls no model

    _, guard_off = build_triage_agent(client, model=ScriptedModel([]), policy=policy, settings=_settings())
    _, guard_on = build_triage_agent(
        client, model=ScriptedModel([]), policy=policy, settings=_settings(), answer_model=model
    )
    assert "ask_contract" not in guard_off.toolbelt and "ask_contract" in guard_on.toolbelt


# -- policy and config ----------------------------------------------------------


def test_policy_contract_terms_defaults_and_shipped_values():
    shipped = load_policy().contract_terms
    assert shipped.enabled and 0 < shipped.min_score <= 1
    assert [q.key for q in shipped.questions] == ["notice_period", "renewal_term", "auto_renew", "uplift_cap"]
    assert all(q.query for q in shipped.questions)

    # A policy file from before this section existed still loads.
    legacy = OrderRenewalPolicy(raw={k: v for k, v in load_policy().raw.items() if k != "contract_terms"})
    assert legacy.contract_terms == ContractTermsPolicy()
    assert legacy.contract_terms.questions == DEFAULT_TERM_QUESTIONS


@pytest.mark.parametrize(
    "raw",
    [
        {"min_score": 3.5},  # a raw BM25-scale value, not a relevance
        {"questions": [{"key": "a", "question": "x"}, {"key": "a", "question": "y"}]},
    ],
)
def test_policy_contract_terms_rejects_bad_values(raw):
    with pytest.raises(ValueError):
        ContractTermsPolicy.from_raw(raw)


def test_rag_model_is_pinned_and_overridable(monkeypatch):
    assert _settings().rag_model == "claude-sonnet-5"
    monkeypatch.setenv("RAG_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("RAG_MAX_TOKENS", "2048")
    s = load_settings()
    assert (s.rag_model, s.rag_max_tokens) == ("claude-sonnet-5-5", 2048)
    m = rag_runtime.build_answer_model(replace(s, anthropic_api_key="sk-test"))
    assert (m.model, m.max_tokens) == ("claude-sonnet-5-5", 2048)
