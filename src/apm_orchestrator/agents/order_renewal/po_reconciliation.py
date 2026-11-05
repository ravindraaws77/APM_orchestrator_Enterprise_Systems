"""Reconciles an extracted purchase order against the Salesforce
Opportunity a renewal case already selected.

Pure, deterministic policy logic -- no I/O, no model. The extraction
agent decides what the document *says*; this decides whether that agrees
with the record of truth closely enough to propose an update, using
tolerances from `policy.yaml`'s `po_intake.reconciliation`. Anything
that doesn't clearly agree is a mismatch for a human, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from apm_orchestrator.document_extraction.schema import PurchaseOrderExtraction


def normalize_account_name(name: str) -> str:
    """Collapses the punctuation/whitespace/case differences a real
    account name and an inbound email's version of it commonly disagree
    on (a trailing period, extra whitespace, case) -- deliberately not
    fuzzy beyond that: two *different* accounts (a UK/Singapore
    subsidiary) should still normalize to different strings."""
    return " ".join(name.split()).rstrip(".").casefold()


@dataclass(frozen=True)
class PoReconciliation:
    matched: bool
    failures: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    def to_state(self) -> dict[str, Any]:
        return {"matched": self.matched, "failures": list(self.failures), "passed": list(self.passed)}


def reconcile(
    extraction: PurchaseOrderExtraction,
    opportunity: dict[str, Any],
    account_name: str,
    rules: dict[str, Any],
    required_fields: list[str],
) -> PoReconciliation:
    failures: list[str] = []
    passed: list[str] = []
    opp_fields = opportunity.get("fields", {})

    for name in required_fields:
        if extraction.value(name) is None:
            failures.append(f"{name}: not found on the document")

    po_account = extraction.value("account_name")
    sf_account = (opp_fields.get("Account") or {}).get("Name") or account_name
    if po_account is not None:
        if normalize_account_name(po_account) == normalize_account_name(sf_account):
            passed.append(f"account_name: {po_account!r} matches {sf_account!r}")
        else:
            failures.append(f"account_name: PO says {po_account!r}, Opportunity account is {sf_account!r}")

    po_amount = extraction.value("amount")
    sf_amount = opp_fields.get("Amount")
    if po_amount is not None:
        if sf_amount is None:
            failures.append("amount: Opportunity has no Amount to reconcile against")
        else:
            diff = abs(float(po_amount) - float(sf_amount))
            allowed = max(
                float(rules.get("amount_tolerance_abs") or 0.0),
                float(rules.get("amount_tolerance_pct") or 0.0) / 100.0 * abs(float(sf_amount)),
            )
            if diff <= allowed:
                passed.append(f"amount: {po_amount:.2f} vs Opportunity {float(sf_amount):.2f}")
            else:
                failures.append(
                    f"amount: PO {po_amount:.2f} vs Opportunity {float(sf_amount):.2f} "
                    f"(off by {diff:.2f}, tolerance {allowed:.2f})"
                )

    expected_currency = rules.get("expected_currency")
    po_currency = extraction.value("currency")
    if expected_currency:
        if po_currency == expected_currency:
            passed.append(f"currency: {po_currency}")
        else:
            failures.append(f"currency: PO is {po_currency or 'unstated'}, expected {expected_currency}")

    term_start = extraction.value("term_start")
    term_end = extraction.value("term_end")
    if term_start and term_end and date.fromisoformat(term_end) <= date.fromisoformat(term_start):
        failures.append(f"term: end {term_end} is not after start {term_start}")

    max_days = rules.get("term_start_max_days_from_close_date")
    close_date = opp_fields.get("CloseDate")
    if term_start and max_days is not None:
        if not close_date:
            failures.append("term_start: Opportunity has no CloseDate to reconcile against")
        else:
            days = abs((date.fromisoformat(term_start) - date.fromisoformat(close_date)).days)
            if days <= int(max_days):
                passed.append(f"term_start: {term_start} is {days}d from CloseDate {close_date}")
            else:
                failures.append(
                    f"term_start: {term_start} is {days}d from CloseDate {close_date} (limit {max_days}d)"
                )

    return PoReconciliation(matched=not failures, failures=failures, passed=passed)
