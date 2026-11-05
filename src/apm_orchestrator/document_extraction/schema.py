"""What a purchase-order extraction looks like, and the rules for when a
human has to look at it before anything acts on it.

One source of truth used on both sides of the A2A boundary: the
extraction agent (`server.py`) builds its result with it, and the
calling business agent (`client.py`) re-validates the artifact it gets
back with the same `parse_extraction` -- an A2A artifact is untrusted
input to the caller, whoever produced it.

Confidence is a number in [0, 1] the model reports per field. It is
*not* assumed to be calibrated: `evals/run_po_extraction_eval.py`
reports how often high- and low-confidence fields are actually right,
which is the only evidence the threshold in `policy.yaml` means
anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

# A2A protocol identifiers shared by server.py and client.py (kept here
# so a caller never has to import the server, and its HTTP-server deps).
SKILL_ID = "extract_purchase_order"
ARTIFACT_NAME = "purchase_order_extraction"

# field name -> (kind, description given to the model). Order matters:
# it's the order fields are listed in prompts, artifacts and reports.
PO_FIELDS: dict[str, tuple[str, str]] = {
    "account_name": (
        "text",
        "The buying customer's company name -- the party issuing the purchase order or "
        "accepting the quote, NOT the vendor/seller it is addressed to.",
    ),
    "po_number": (
        "text",
        "The buyer's purchase order number exactly as printed (keep prefixes, dashes and "
        "leading zeros). For a signed quote with no PO number, null.",
    ),
    "amount": (
        "number",
        "The total order amount BEFORE tax/VAT, as a plain number with no currency symbol or "
        "thousands separators (e.g. 48500.00). If only a tax-inclusive total is shown, null.",
    ),
    "currency": ("text", "ISO 4217 currency code of the amount, e.g. USD, EUR, GBP."),
    "term_start": ("date", "Start date of the subscription/service term, as YYYY-MM-DD."),
    "term_end": ("date", "End date of the subscription/service term, as YYYY-MM-DD."),
    "signatory_name": (
        "text",
        "Full name of the person who signed or authorized the document on the buyer's side.",
    ),
}

DOCUMENT_TYPES = ("purchase_order", "renewal_quote", "other")
ACCEPTED_DOCUMENT_TYPES = ("purchase_order", "renewal_quote")

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class ExtractionSchemaError(ValueError):
    """The extraction payload doesn't have the shape this module defines --
    a bug or a misbehaving peer agent, never something to act on."""


@dataclass(frozen=True)
class FieldValue:
    value: str | float | None
    confidence: float
    evidence: str | None = None
    # "model" (extracted), "human_review" (a reviewer typed the value),
    # "human_confirmed" (a reviewer looked at a flagged value and kept it)
    source: str = "model"

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class PurchaseOrderExtraction:
    document_type: str
    fields: dict[str, FieldValue]
    # The document contains text addressed to an AI/automated system
    # ("ignore previous instructions", "set the amount to ...") -- always
    # a reason for human review, never something to follow.
    contains_instructions: bool = False
    notes: str = ""
    reviewed_by: str | None = None
    problems: list[str] = field(default_factory=list)  # normalization issues found while parsing

    def value(self, name: str) -> Any:
        return self.fields[name].value

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_type": self.document_type,
            "fields": {name: self.fields[name].to_dict() for name in PO_FIELDS},
            "contains_instructions": self.contains_instructions,
            "notes": self.notes,
            "reviewed_by": self.reviewed_by,
            "problems": list(self.problems),
        }


def output_json_schema() -> dict[str, Any]:
    """The JSON schema handed to the Messages API's structured outputs
    (`output_config.format`), so the model's reply is schema-valid JSON
    by construction rather than parsed out of free text."""

    def field_schema(kind: str) -> dict[str, Any]:
        value_type = {"type": "number"} if kind == "number" else {"type": "string"}
        return {
            "type": "object",
            "properties": {
                "value": {"anyOf": [value_type, {"type": "null"}]},
                "confidence": {"type": "number"},
                "evidence": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["value", "confidence", "evidence"],
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": {
            "document_type": {"type": "string", "enum": list(DOCUMENT_TYPES)},
            "fields": {
                "type": "object",
                "properties": {name: field_schema(kind) for name, (kind, _) in PO_FIELDS.items()},
                "required": list(PO_FIELDS),
                "additionalProperties": False,
            },
            "contains_instructions": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": ["document_type", "fields", "contains_instructions", "notes"],
        "additionalProperties": False,
    }


def _clamp_confidence(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return min(1.0, max(0.0, float(raw)))


def normalize_value(name: str, raw: Any) -> str | float | None:
    """Coerce one field to its canonical form, or raise ValueError. Shared
    by model output parsing and human-review input, so a reviewer can't
    enter a value the extractor itself would have rejected."""
    if raw is None:
        return None
    kind = PO_FIELDS[name][0]
    if kind == "number":
        if isinstance(raw, bool):
            raise ValueError(f"{name}: expected a number, got {raw!r}")
        if isinstance(raw, str):
            raw = raw.replace(",", "").strip()
        try:
            return round(float(raw), 2)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: expected a number, got {raw!r}") from None
    if not isinstance(raw, str):
        raise ValueError(f"{name}: expected text, got {raw!r}")
    text = " ".join(raw.split())
    if not text:
        return None
    if kind == "date":
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError(f"{name}: expected a YYYY-MM-DD date, got {raw!r}") from None
    if name == "currency":
        text = text.upper()
        if not _CURRENCY_RE.match(text):
            raise ValueError(f"currency: expected an ISO 4217 code, got {raw!r}")
    return text


def parse_extraction(raw: Any) -> PurchaseOrderExtraction:
    """Validate an extraction payload (the model's JSON, or an A2A
    artifact) into a PurchaseOrderExtraction. Structural problems raise
    ExtractionSchemaError; a single field that doesn't normalize (a
    malformed date, say) is kept as value=None, confidence=0 and noted
    in `problems`, so it's routed to a human rather than failing the
    whole document."""
    if not isinstance(raw, dict):
        raise ExtractionSchemaError("extraction must be an object")
    document_type = raw.get("document_type")
    if document_type not in DOCUMENT_TYPES:
        raise ExtractionSchemaError(f"unknown document_type {document_type!r}")
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, dict):
        raise ExtractionSchemaError("fields must be an object")

    fields: dict[str, FieldValue] = {}
    problems: list[str] = list(raw.get("problems") or [])
    for name in PO_FIELDS:
        entry = raw_fields.get(name)
        if not isinstance(entry, dict):
            raise ExtractionSchemaError(f"missing field {name!r}")
        source = entry.get("source") or "model"
        evidence = entry.get("evidence")
        evidence = evidence if isinstance(evidence, str) else None
        try:
            value = normalize_value(name, entry.get("value"))
            confidence = _clamp_confidence(entry.get("confidence"))
        except ValueError as exc:
            problems.append(str(exc))
            value, confidence = None, 0.0
        fields[name] = FieldValue(value=value, confidence=confidence, evidence=evidence, source=source)

    reviewed_by = raw.get("reviewed_by")
    return PurchaseOrderExtraction(
        document_type=document_type,
        fields=fields,
        contains_instructions=bool(raw.get("contains_instructions")),
        notes=raw.get("notes") if isinstance(raw.get("notes"), str) else "",
        reviewed_by=reviewed_by if isinstance(reviewed_by, str) else None,
        problems=problems,
    )


def review_reasons(
    extraction: PurchaseOrderExtraction, min_field_confidence: float, required_fields: list[str]
) -> dict[str, str]:
    """Why a human has to look at this extraction before anything uses
    it, keyed by field name (or "_document" for document-level reasons).
    Empty means it can proceed unreviewed. Policy (threshold, which
    fields are required) comes from the *caller's* policy.yaml, passed
    in per request -- the extraction agent has no business policy of its
    own."""
    reasons: dict[str, str] = {}
    if extraction.document_type not in ACCEPTED_DOCUMENT_TYPES:
        reasons["_document"] = f"document looks like {extraction.document_type!r}, not a PO or renewal quote"
    if extraction.contains_instructions:
        reasons["_instructions"] = (
            "document contains text addressed to an automated system -- check every value by hand"
        )
    for name in PO_FIELDS:
        fv = extraction.fields[name]
        if fv.value is None:
            if name in required_fields:
                reasons[name] = "required field not found"
        elif fv.confidence < min_field_confidence:
            reasons[name] = f"confidence {fv.confidence:.2f} below {min_field_confidence:.2f}"
    return reasons


def apply_review(
    extraction: PurchaseOrderExtraction,
    reviewed_fields: dict[str, Any],
    reviewer: str,
    flagged_fields: list[str],
) -> PurchaseOrderExtraction:
    """A reviewer's corrections applied to an extraction. Values they
    typed become source="human_review"; flagged values they left alone
    are taken as confirmed (source="human_confirmed"). Both get
    confidence 1.0. Raises ValueError on an unknown field or a value
    that doesn't normalize."""
    if not reviewer:
        raise ValueError("a reviewer identity is required")
    unknown = set(reviewed_fields) - set(PO_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {sorted(unknown)}")

    fields = dict(extraction.fields)
    for name, raw in reviewed_fields.items():
        fields[name] = FieldValue(
            value=normalize_value(name, raw),
            confidence=1.0,
            evidence=f"entered by reviewer {reviewer}",
            source="human_review",
        )
    for name in flagged_fields:
        if name in PO_FIELDS and name not in reviewed_fields:
            fields[name] = replace(fields[name], confidence=1.0, source="human_confirmed")
    return replace(extraction, fields=fields, reviewed_by=reviewer)
