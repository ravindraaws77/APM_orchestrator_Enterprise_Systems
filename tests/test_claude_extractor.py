"""ClaudeExtractor's request shape and response handling, against a mocked
Messages API (no network, no API key). The live quality check is
evals/run_po_extraction_eval.py."""

from __future__ import annotations

import base64
import json

import anthropic
import httpx2
import pytest

from apm_orchestrator.document_extraction.extractor import ClaudeExtractor, ExtractionError
from tests.document_extraction_fakes import po_payload, settings


def _extractor(handler) -> tuple[ClaudeExtractor, list[dict]]:
    seen: list[dict] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return handler(request)

    client = anthropic.AsyncAnthropic(
        api_key="test", max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(record))
    )
    return ClaudeExtractor(settings(), client=client), seen


def _message(content: list[dict], stop_reason: str = "end_turn", **extra) -> httpx2.Response:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
        **extra,
    }
    return httpx2.Response(200, json=body)


@pytest.mark.asyncio
async def test_pdf_request_shape_and_parsed_result():
    extractor, seen = _extractor(lambda r: _message([{"type": "text", "text": json.dumps(po_payload())}]))
    result = await extractor.extract_purchase_order(b"%PDF-1.4", "application/pdf", "po.pdf")

    assert result.value("po_number") == "PO-004512"
    body = seen[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "high"
    assert "tool_choice" not in body
    document, instruction = body["messages"][0]["content"]
    assert document["type"] == "document"
    assert base64.b64decode(document["source"]["data"]) == b"%PDF-1.4"
    assert '"po.pdf"' in instruction["text"]
    assert "untrusted" in body["system"]


@pytest.mark.asyncio
async def test_text_and_image_documents_use_matching_blocks():
    extractor, seen = _extractor(lambda r: _message([{"type": "text", "text": json.dumps(po_payload())}]))
    await extractor.extract_purchase_order(b"PO text", "text/plain")
    await extractor.extract_purchase_order(b"\x89PNG", "image/png")
    assert seen[0]["messages"][0]["content"][0]["source"] == {
        "type": "text",
        "media_type": "text/plain",
        "data": "PO text",
    }
    assert seen[1]["messages"][0]["content"][0]["type"] == "image"


@pytest.mark.asyncio
async def test_refusal_raises_instead_of_reading_content():
    extractor, _ = _extractor(
        lambda r: _message(
            [], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None}
        )
    )
    with pytest.raises(ExtractionError, match="declined"):
        await extractor.extract_purchase_order(b"%PDF", "application/pdf")


@pytest.mark.asyncio
async def test_truncated_output_raises():
    extractor, _ = _extractor(lambda r: _message([{"type": "text", "text": "{"}], stop_reason="max_tokens"))
    with pytest.raises(ExtractionError, match="truncated"):
        await extractor.extract_purchase_order(b"%PDF", "application/pdf")


@pytest.mark.asyncio
async def test_unsupported_media_type_never_calls_the_api():
    extractor, seen = _extractor(lambda r: _message([]))
    with pytest.raises(ValueError):
        await extractor.extract_purchase_order(b"x", "application/zip")
    assert seen == []
