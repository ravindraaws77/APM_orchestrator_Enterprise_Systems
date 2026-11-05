"""Order-Renewal case graph, PO-intake branch (roadmap 6.1 + 6.3).

apm_connectors is mocked (httpx.MockTransport) as in
test_case_graph_nodes.py; the document-extraction agent is the *real* A2A
server app and client, in-process, with a canned extractor in place of
Claude -- so these tests exercise the actual A2A task lifecycle
(completed / input-required -> review -> completed / rejected) from
inside the case graph. MemorySaver, no Postgres.
"""

from __future__ import annotations

import base64
from datetime import date, timedelta

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import apm_orchestrator.agents.order_renewal.case_graph as case_graph
import apm_orchestrator.document_extraction.client as extraction_client_module
import apm_orchestrator.tools as tools_module
from apm_orchestrator.agents.order_renewal.policy import OrderRenewalPolicy, load_policy
from apm_orchestrator.connectors_client import ConnectorsClient
from apm_orchestrator.document_extraction.client import DocumentExtractionClient
from apm_orchestrator.document_extraction.server import build_app
from apm_orchestrator.poller import _sweep_agent
from tests.document_extraction_fakes import FakeExtractor, field, po_payload, settings

CLOSE_DATE = (date.today() + timedelta(days=20)).isoformat()
PDF = b"%PDF-1.4 purchase order"


def _payload(**overrides):
    defaults = {"term_start": field(CLOSE_DATE), "term_end": field((date.today() + timedelta(days=385)).isoformat())}
    return po_payload(**{**defaults, **overrides})


def _write_response(action_id: str, tool: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "action_id": action_id,
            "summary": None,
            "pending_action": {"type": "approval_request", "action_id": f"internal-{action_id}", "tool": tool},
            "final_result": None,
        },
    )


class FakeConnectors:
    """apm_connectors /tools/* responses for one renewal case, recording calls."""

    def __init__(self, amount: float = 48500.0):
        self.calls: list[tuple[str, dict]] = []
        self.amount = amount

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        if path == "/tools/gmail/search":
            return httpx.Response(200, json=[{"message_id": "m1"}])
        if path == "/tools/gmail/read":
            return httpx.Response(200, json={"message_id": "m1", "sender": "buyer@acme.com"})
        if path == "/tools/salesforce/query":
            return httpx.Response(
                200,
                json=[
                    {
                        "record_id": "opp1",
                        "object_type": "Opportunity",
                        "fields": {
                            "CloseDate": CLOSE_DATE,
                            "Amount": self.amount,
                            "AccountId": "acc1",
                            "Account": {"Name": "Acme Corp."},
                        },
                    }
                ],
            )
        if path in ("/tools/jira/search", "/tools/drive/list"):
            return httpx.Response(200, json=[])
        if path == "/tools/drive/read":
            return httpx.Response(
                200,
                json={
                    "file_id": body["file_id"],
                    "name": "Acme PO 4512.pdf",
                    "mime_type": "application/pdf",
                    "size": len(PDF),
                    "content_base64": base64.b64encode(PDF).decode(),
                },
            )
        if path == "/tools/salesforce/update":
            return _write_response("sf-update-1", "salesforce")
        if path == "/tools/jira/create":
            return _write_response("jira-create-1", "jira")
        if path == "/tools/calendar/create-event":
            return _write_response("cal-1", "google_calendar")
        raise AssertionError(f"unexpected call: {path}")

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def body(self, path: str) -> dict:
        return next(body for p, body in self.calls if p == path)


@pytest.fixture
def connectors(monkeypatch):
    fake = FakeConnectors()
    client = ConnectorsClient(settings=settings(), transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(tools_module, "_client", client)
    return fake


@pytest.fixture
def configured_policy(monkeypatch):
    """The shipped policy, with the PO-number placeholder replaced by a real-looking field."""
    raw = load_policy().raw
    raw["po_intake"]["record_fields"] = {"po_number": "PO_Number__c"}
    monkeypatch.setattr(case_graph, "load_policy", lambda: OrderRenewalPolicy(raw=raw))


def _install_extraction_agent(monkeypatch, extractor: FakeExtractor) -> None:
    s = settings()
    client = DocumentExtractionClient(s, transport=httpx.ASGITransport(app=build_app(extractor, s)))
    monkeypatch.setattr(extraction_client_module, "_client", client)


def _config(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_id}}


async def _start(graph, case_id: str, po_file_id: str | None = "drive-po-1"):
    return await graph.ainvoke(
        {"case_id": case_id, "account_name": "Acme Corp", "po_file_id": po_file_id, "steps_completed": []},
        config=_config(case_id),
    )


def _approve(graph, case_id):
    return graph.ainvoke(
        Command(resume={"approved": True, "final_result": {"executed": True}}), config=_config(case_id)
    )


@pytest.mark.asyncio
async def test_matching_po_proposes_opportunity_update_before_the_call(monkeypatch, connectors, configured_policy):
    extractor = FakeExtractor(_payload())
    _install_extraction_agent(monkeypatch, extractor)
    graph = case_graph.build_case_graph(MemorySaver())

    result = await _start(graph, "po-match")
    payload = result["__interrupt__"][0].value
    assert payload["step"] == "po_update"
    assert payload["action_id"] == "sf-update-1"
    assert connectors.body("/tools/salesforce/update")["fields"] == {"PO_Number__c": "PO-004512"}
    # The document reached the extraction agent as bytes from Drive, with its real media type.
    assert extractor.calls == [(PDF, "application/pdf", "Acme PO 4512.pdf")]

    after = await _approve(graph, "po-match")
    assert after["__interrupt__"][0].value["step"] == "call"


@pytest.mark.asyncio
async def test_placeholder_record_field_stops_instead_of_proposing_a_write(monkeypatch, connectors):
    _install_extraction_agent(monkeypatch, FakeExtractor(_payload()))
    graph = case_graph.build_case_graph(MemorySaver())

    result = await _start(graph, "po-placeholder")
    assert result["done"] is True
    assert "REPLACE_WITH_OPPORTUNITY_PO_NUMBER_FIELD" in result["final_summary"]
    assert "/tools/salesforce/update" not in connectors.paths()


@pytest.mark.asyncio
async def test_mismatched_po_proposes_jira_ticket_and_ends_case(monkeypatch, connectors, configured_policy):
    _install_extraction_agent(monkeypatch, FakeExtractor(_payload(amount=field(52000.0))))
    graph = case_graph.build_case_graph(MemorySaver())

    result = await _start(graph, "po-mismatch")
    assert result["__interrupt__"][0].value["step"] == "po_mismatch"
    fields = connectors.body("/tools/jira/create")["fields"]
    assert fields["project"] == {"key": "KAN"}
    assert fields["issuetype"] == {"name": "Task"}
    assert "52000.00" in fields["description"]
    assert "/tools/salesforce/update" not in connectors.paths()

    done = await _approve(graph, "po-mismatch")
    assert done["done"] is True
    assert "did not reconcile" in done["final_summary"]
    assert "/tools/calendar/create-event" not in connectors.paths()


@pytest.mark.asyncio
async def test_low_confidence_pauses_for_review_then_corrected_value_is_used(
    monkeypatch, connectors, configured_policy
):
    _install_extraction_agent(monkeypatch, FakeExtractor(_payload(po_number=field("PO-0O4512", confidence=0.35))))
    graph = case_graph.build_case_graph(MemorySaver())

    result = await _start(graph, "po-review")
    payload = result["__interrupt__"][0].value
    assert payload["type"] == "awaiting_po_field_review"
    assert payload["action_id"] is None and payload["pending_action"] is None
    assert set(payload["review"]["review_reasons"]) == {"po_number"}

    outcome = await case_graph.resume_po_review(
        graph, "po-review", reviewer="jane", decision="accept", reviewed_fields={"po_number": "PO-004512"}
    )
    assert outcome.step == "po_update"
    assert connectors.body("/tools/salesforce/update")["fields"] == {"PO_Number__c": "PO-004512"}


@pytest.mark.asyncio
async def test_invalid_review_keeps_case_waiting(monkeypatch, connectors, configured_policy):
    _install_extraction_agent(monkeypatch, FakeExtractor(_payload(po_number=field("PO-1", confidence=0.35))))
    graph = case_graph.build_case_graph(MemorySaver())
    await _start(graph, "po-bad-review")

    outcome = await case_graph.resume_po_review(
        graph, "po-bad-review", reviewer="jane", decision="accept", reviewed_fields={"term_end": "soon"}
    )
    assert outcome.step == "po_review"
    assert "review not applied" in outcome.review["message"]


@pytest.mark.asyncio
async def test_reviewer_rejection_files_a_ticket(monkeypatch, connectors, configured_policy):
    _install_extraction_agent(monkeypatch, FakeExtractor(_payload(po_number=field("PO-1", confidence=0.35))))
    graph = case_graph.build_case_graph(MemorySaver())
    await _start(graph, "po-reject")

    outcome = await case_graph.resume_po_review(
        graph, "po-reject", reviewer="jane", decision="reject", reason="unsigned draft"
    )
    assert outcome.step == "po_mismatch"
    assert "unsigned draft" in connectors.body("/tools/jira/create")["fields"]["description"]


@pytest.mark.asyncio
async def test_resume_po_review_refuses_a_case_waiting_on_an_approval(monkeypatch, connectors, configured_policy):
    _install_extraction_agent(monkeypatch, FakeExtractor(_payload()))
    graph = case_graph.build_case_graph(MemorySaver())
    await _start(graph, "po-approval")  # paused on the po_update approval

    with pytest.raises(ValueError, match="not waiting on a PO review"):
        await case_graph.resume_po_review(graph, "po-approval", reviewer="jane", decision="accept")


@pytest.mark.asyncio
async def test_extraction_agent_unreachable_stops_case(monkeypatch, connectors):
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        extraction_client_module, "_client", DocumentExtractionClient(settings(), transport=httpx.MockTransport(refuse))
    )
    graph = case_graph.build_case_graph(MemorySaver())

    result = await _start(graph, "po-down")
    assert result["done"] is True
    assert "PO extraction failed" in result["final_summary"]


@pytest.mark.asyncio
async def test_case_without_po_never_touches_drive_read_or_the_agent(monkeypatch, connectors):
    extractor = FakeExtractor()
    _install_extraction_agent(monkeypatch, extractor)
    graph = case_graph.build_case_graph(MemorySaver())

    result = await _start(graph, "no-po", po_file_id=None)
    assert result["__interrupt__"][0].value["step"] == "call"
    assert "/tools/drive/read" not in connectors.paths()
    assert extractor.calls == []


class _Registry:
    def __init__(self, case_ids):
        self._case_ids = case_ids

    async def list_case_ids(self, agent):
        return self._case_ids


class _NoStatusClient:
    async def get_action_status(self, action_id):
        raise AssertionError("the poller must not poll a case paused on a PO review")


@pytest.mark.asyncio
async def test_poller_leaves_po_review_interrupts_alone(monkeypatch, connectors, configured_policy):
    _install_extraction_agent(monkeypatch, FakeExtractor(_payload(po_number=field("PO-1", confidence=0.35))))
    checkpointer = MemorySaver()
    await _start(case_graph.build_case_graph(checkpointer), "po-poll")

    resumed = await _sweep_agent("order_renewal", case_graph, _Registry(["po-poll"]), checkpointer, _NoStatusClient())
    assert resumed == 0
