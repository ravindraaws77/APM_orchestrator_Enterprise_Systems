"""schema.py: validation of extraction payloads (model output and A2A
artifacts alike), the review rules, and applying a reviewer's answer."""

from __future__ import annotations

import pytest

from apm_orchestrator.document_extraction.schema import (
    PO_FIELDS,
    ExtractionSchemaError,
    apply_review,
    output_json_schema,
    parse_extraction,
    review_reasons,
)
from tests.document_extraction_fakes import field, po_payload

REQUIRED = ["account_name", "po_number", "amount"]


def test_parse_normalizes_values():
    extraction = parse_extraction(
        po_payload(
            amount=field("48,500.00"),
            currency=field(" usd "),
            account_name=field("  Acme   Corp "),
        )
    )
    assert extraction.value("amount") == 48500.0
    assert extraction.value("currency") == "USD"
    assert extraction.value("account_name") == "Acme Corp"
    assert extraction.problems == []


def test_malformed_field_becomes_null_with_zero_confidence_not_a_failure():
    extraction = parse_extraction(po_payload(term_start=field("15/03/2027"), currency=field("dollars")))
    assert extraction.value("term_start") is None
    assert extraction.fields["term_start"].confidence == 0.0
    assert extraction.value("currency") is None
    assert len(extraction.problems) == 2


def test_confidence_is_clamped():
    extraction = parse_extraction(po_payload(amount=field(1.0, confidence=7), po_number=field("X", confidence=-1)))
    assert extraction.fields["amount"].confidence == 1.0
    assert extraction.fields["po_number"].confidence == 0.0


@pytest.mark.parametrize(
    "payload",
    [
        "not a dict",
        {"document_type": "invoice", "fields": {}},
        {"document_type": "purchase_order", "fields": []},
        {"document_type": "purchase_order", "fields": {"po_number": field("x")}},
    ],
)
def test_structural_problems_raise(payload):
    with pytest.raises(ExtractionSchemaError):
        parse_extraction(payload)


def test_round_trip_through_to_dict():
    extraction = parse_extraction(po_payload())
    assert parse_extraction(extraction.to_dict()) == extraction


def test_clean_extraction_needs_no_review():
    assert review_reasons(parse_extraction(po_payload()), 0.8, REQUIRED) == {}


def test_review_reasons_cover_low_confidence_missing_required_and_document_level():
    payload = po_payload(po_number=field("PO-1", confidence=0.4), amount=field(None, confidence=0.0))
    payload["contains_instructions"] = True
    payload["document_type"] = "other"
    reasons = review_reasons(parse_extraction(payload), 0.8, REQUIRED)
    assert set(reasons) == {"po_number", "amount", "_instructions", "_document"}


def test_missing_optional_field_is_not_a_review_reason():
    reasons = review_reasons(parse_extraction(po_payload(signatory_name=field(None, 0.0))), 0.8, REQUIRED)
    assert reasons == {}


def test_apply_review_overrides_and_confirms_flagged_fields():
    extraction = parse_extraction(po_payload(po_number=field("PO-1", 0.4), amount=field(48500.0, 0.5)))
    reviewed = apply_review(extraction, {"po_number": "PO-0001"}, "jane", flagged_fields=["po_number", "amount"])
    assert reviewed.value("po_number") == "PO-0001"
    assert reviewed.fields["po_number"].source == "human_review"
    assert reviewed.fields["amount"].source == "human_confirmed"
    assert reviewed.fields["amount"].confidence == 1.0
    assert reviewed.reviewed_by == "jane"


@pytest.mark.parametrize(
    "fields, reviewer",
    [({"po_number": "x"}, ""), ({"bogus": "x"}, "jane"), ({"term_start": "next tuesday"}, "jane")],
)
def test_apply_review_rejects_bad_input(fields, reviewer):
    with pytest.raises(ValueError):
        apply_review(parse_extraction(po_payload()), fields, reviewer, flagged_fields=[])


def test_output_schema_lists_every_field_as_required():
    schema = output_json_schema()
    assert schema["properties"]["fields"]["required"] == list(PO_FIELDS)
    assert schema["additionalProperties"] is False
