"""The document-extraction agent over the real A2A protocol: the real
server app (Agent Card + JSON-RPC) in-process via httpx's ASGI
transport, the real client, and a canned extractor in place of Claude."""

from __future__ import annotations

import httpx
import pytest

from apm_orchestrator.document_extraction.client import DocumentExtractionClient
from apm_orchestrator.document_extraction.extractor import ExtractionError
from apm_orchestrator.document_extraction.schema import SKILL_ID
from apm_orchestrator.document_extraction.server import build_app
from tests.document_extraction_fakes import FakeExtractor, field, po_payload, settings

PDF = b"%PDF-1.4 fake"
REQUIRED = ["account_name", "po_number", "amount"]


def _client(extractor, **setting_overrides) -> DocumentExtractionClient:
    s = settings(**setting_overrides)
    app = build_app(extractor, s)
    return DocumentExtractionClient(s, transport=httpx.ASGITransport(app=app))


@pytest.mark.asyncio
async def test_agent_card_advertises_skill():
    s = settings()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(FakeExtractor(), s)), base_url="http://extraction-agent"
    ) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
    assert [skill["id"] for skill in card["skills"]] == [SKILL_ID]
    assert card["supportedInterfaces"][0]["url"] == "http://extraction-agent/a2a"


@pytest.mark.asyncio
async def test_confident_extraction_completes():
    extractor = FakeExtractor()
    result = await _client(extractor).extract_purchase_order(PDF, "application/pdf", "po.pdf", 0.8, REQUIRED)
    assert result.state == "completed"
    assert result.extraction.value("po_number") == "PO-004512"
    assert result.review_reasons == {}
    assert extractor.calls == [(PDF, "application/pdf", "po.pdf")]


@pytest.mark.asyncio
async def test_low_confidence_goes_input_required_then_review_completes():
    client = _client(FakeExtractor(po_payload(po_number=field("PO-45l2", confidence=0.3))))
    first = await client.extract_purchase_order(PDF, "application/pdf", "po.pdf", 0.8, REQUIRED)
    assert first.state == "input_required"
    assert set(first.review_reasons) == {"po_number"}

    reviewed = await client.submit_review(
        first.task_id, first.context_id, reviewer="jane", decision="accept", reviewed_fields={"po_number": "PO-4512"}
    )
    assert reviewed.state == "completed"
    assert reviewed.task_id == first.task_id
    assert reviewed.extraction.value("po_number") == "PO-4512"
    assert reviewed.extraction.reviewed_by == "jane"


@pytest.mark.asyncio
async def test_invalid_review_keeps_task_waiting():
    client = _client(FakeExtractor(po_payload(po_number=field("PO-1", confidence=0.3))))
    first = await client.extract_purchase_order(PDF, "application/pdf", None, 0.8, REQUIRED)
    bad = await client.submit_review(
        first.task_id, first.context_id, reviewer="jane", decision="accept", reviewed_fields={"amount": "lots"}
    )
    assert bad.state == "input_required"
    assert "review not applied" in bad.message


@pytest.mark.asyncio
async def test_reviewer_can_reject():
    client = _client(FakeExtractor(po_payload(po_number=field("PO-1", confidence=0.3))))
    first = await client.extract_purchase_order(PDF, "application/pdf", None, 0.8, REQUIRED)
    rejected = await client.submit_review(first.task_id, first.context_id, "jane", "reject", reason="draft quote")
    assert rejected.state == "rejected"
    assert "draft quote" in rejected.message


@pytest.mark.asyncio
async def test_threshold_and_required_fields_come_from_the_caller():
    payload = po_payload(signatory_name=field(None, confidence=0.0), amount=field(48500.0, confidence=0.85))
    lenient = await _client(FakeExtractor(payload)).extract_purchase_order(PDF, "application/pdf", None, 0.8, REQUIRED)
    strict = await _client(FakeExtractor(payload)).extract_purchase_order(
        PDF, "application/pdf", None, 0.9, REQUIRED + ["signatory_name"]
    )
    assert lenient.state == "completed"
    assert set(strict.review_reasons) == {"amount", "signatory_name"}


@pytest.mark.asyncio
async def test_unsupported_media_type_rejected_without_calling_the_model():
    extractor = FakeExtractor()
    result = await _client(extractor).extract_purchase_order(
        b"x", "application/vnd.google-apps.document", None, 0.8, REQUIRED
    )
    assert result.state == "rejected"
    assert "unsupported media type" in result.message
    assert extractor.calls == []


@pytest.mark.asyncio
async def test_oversized_document_rejected():
    result = await _client(FakeExtractor(), document_extraction_max_bytes=10).extract_purchase_order(
        PDF, "application/pdf", None, 0.8, REQUIRED
    )
    assert result.state == "rejected"


@pytest.mark.asyncio
async def test_extractor_error_fails_task():
    result = await _client(FakeExtractor(error=ExtractionError("model declined"))).extract_purchase_order(
        PDF, "application/pdf", None, 0.8, REQUIRED
    )
    assert result.state == "failed"
    assert "model declined" in result.message


@pytest.mark.asyncio
async def test_bearer_auth_enforced_when_configured():
    extractor = FakeExtractor()
    s_server = settings(document_extraction_server_api_keys=("secret",))
    app = build_app(extractor, s_server)

    anonymous = DocumentExtractionClient(settings(), transport=httpx.ASGITransport(app=app))
    denied = await anonymous.extract_purchase_order(PDF, "application/pdf", None, 0.8, REQUIRED)
    assert denied.state == "failed"
    assert extractor.calls == []

    keyed = DocumentExtractionClient(
        settings(document_extraction_api_key="secret"), transport=httpx.ASGITransport(app=app)
    )
    allowed = await keyed.extract_purchase_order(PDF, "application/pdf", None, 0.8, REQUIRED)
    assert allowed.state == "completed"


@pytest.mark.asyncio
async def test_unreachable_agent_is_a_clean_failure():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = DocumentExtractionClient(settings(), transport=httpx.MockTransport(refuse))
    result = await client.extract_purchase_order(PDF, "application/pdf", None, 0.8, REQUIRED)
    assert result.state == "failed"
    assert "document-extraction agent call failed" in result.message
