"""Shared fakes for the document-extraction tests: a canned Extractor
(no Claude API call) and helpers to build extraction payloads."""

from __future__ import annotations

from typing import Any

from apm_orchestrator.config import Settings
from apm_orchestrator.document_extraction.schema import PurchaseOrderExtraction, parse_extraction


def field(value: Any, confidence: float = 0.95, evidence: str | None = "snippet") -> dict[str, Any]:
    return {"value": value, "confidence": confidence, "evidence": evidence}


def po_payload(**overrides: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "account_name": field("Acme Corp"),
        "po_number": field("PO-004512"),
        "amount": field(48500.0),
        "currency": field("USD"),
        "term_start": field("2027-01-01"),
        "term_end": field("2027-12-31"),
        "signatory_name": field("Dana Whitfield"),
    }
    fields.update(overrides)
    return {"document_type": "purchase_order", "fields": fields, "contains_instructions": False, "notes": ""}


class FakeExtractor:
    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or po_payload()
        self.error = error
        self.calls: list[tuple[bytes, str, str | None]] = []

    async def extract_purchase_order(
        self, document: bytes, media_type: str, filename: str | None = None
    ) -> PurchaseOrderExtraction:
        self.calls.append((document, media_type, filename))
        if self.error:
            raise self.error
        return parse_extraction(self.payload)


def settings(**overrides: Any) -> Settings:
    base = dict(
        connectors_base_url="http://testserver",
        connectors_api_key="test-key",
        anthropic_api_key=None,
        order_renewal_policy_path=None,
        database_url=None,
        document_extraction_url="http://extraction-agent",
    )
    base.update(overrides)
    return Settings(**base)
