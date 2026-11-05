"""po_reconciliation.reconcile: an extracted PO vs the Opportunity."""

from __future__ import annotations

from apm_orchestrator.agents.order_renewal.po_reconciliation import reconcile
from apm_orchestrator.document_extraction.schema import parse_extraction
from tests.document_extraction_fakes import field, po_payload

RULES = {
    "amount_tolerance_abs": 1.0,
    "amount_tolerance_pct": 0.0,
    "term_start_max_days_from_close_date": 45,
    "expected_currency": None,
}
REQUIRED = ["account_name", "po_number", "amount", "term_start", "term_end"]


def _opportunity(**fields):
    base = {"Amount": 48500.0, "CloseDate": "2026-12-20", "Account": {"Name": "Acme Corp."}}
    base.update(fields)
    return {"record_id": "opp1", "fields": base}


def _run(payload=None, opportunity=None, rules=None):
    return reconcile(
        parse_extraction(payload or po_payload()),
        opportunity or _opportunity(),
        "Acme Corp",
        {**RULES, **(rules or {})},
        REQUIRED,
    )


def test_matching_po_passes_with_normalized_account_name():
    result = _run()
    assert result.matched, result.failures


def test_sibling_account_is_a_mismatch_not_a_fuzzy_match():
    result = _run(po_payload(account_name=field("Acme Corp UK Ltd")))
    assert not result.matched
    assert any(f.startswith("account_name") for f in result.failures)


def test_amount_outside_tolerance():
    assert not _run(po_payload(amount=field(48600.0))).matched
    assert _run(po_payload(amount=field(48500.75))).matched
    assert _run(po_payload(amount=field(48600.0)), rules={"amount_tolerance_pct": 0.5}).matched


def test_opportunity_without_amount_is_a_mismatch():
    result = _run(opportunity=_opportunity(Amount=None))
    assert any("no Amount" in f for f in result.failures)


def test_missing_required_field_is_a_mismatch():
    result = _run(po_payload(po_number=field(None, 0.0)))
    assert "po_number: not found on the document" in result.failures


def test_term_start_far_from_close_date():
    result = _run(po_payload(term_start=field("2027-06-01")))
    assert any(f.startswith("term_start") for f in result.failures)
    assert _run(po_payload(term_start=field("2027-06-01")), rules={"term_start_max_days_from_close_date": None}).matched


def test_term_end_before_start():
    result = _run(po_payload(term_end=field("2026-06-01")))
    assert any(f.startswith("term:") for f in result.failures)


def test_expected_currency_enforced_only_when_configured():
    eur = po_payload(currency=field("EUR"))
    assert _run(eur).matched
    assert not _run(eur, rules={"expected_currency": "USD"}).matched
