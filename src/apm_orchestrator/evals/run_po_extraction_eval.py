"""Runs po_extraction_cases.CASES through the real ClaudeExtractor and
scores every field against its hand label.

Two questions, reported separately:

1. Accuracy -- is each field's value right? Per field, per case, overall.
   An AMBIGUOUS field passes only if it would reach a human (null, or
   confidence below the threshold).
2. Calibration -- does confidence mean anything? Of the labelled fields,
   how often is a value at/above the threshold right vs. one below it.
   If high-confidence values aren't clearly more accurate than
   low-confidence ones, the review threshold in policy.yaml isn't doing
   its job, whatever the accuracy number says.

It calls the extractor directly, not through the A2A server: this
measures extraction quality, and the A2A plumbing around it is covered
deterministically by tests/test_document_extraction_a2a.py.

Usage:
    python -m apm_orchestrator.evals.run_po_extraction_eval
    python -m apm_orchestrator.evals.run_po_extraction_eval --case injected_instructions --output po-eval.json

Needs ANTHROPIC_API_KEY (one Messages API call per case). No
apm_connectors server, Postgres or connector credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apm_orchestrator.agents.order_renewal.po_reconciliation import normalize_account_name
from apm_orchestrator.agents.order_renewal.policy import load_policy
from apm_orchestrator.config import load_settings
from apm_orchestrator.document_extraction.extractor import ClaudeExtractor, Extractor
from apm_orchestrator.document_extraction.schema import PO_FIELDS, PurchaseOrderExtraction
from apm_orchestrator.evals.minimal_pdf import render_pdf
from apm_orchestrator.evals.po_extraction_cases import AMBIGUOUS, CASES, DATASET_VERSION, POCase


@dataclass
class FieldScore:
    name: str
    expected: Any
    actual: Any
    confidence: float
    correct: bool


@dataclass
class CaseScore:
    case_id: str
    category: str
    fields: list[FieldScore] = field(default_factory=list)
    document_type_ok: bool = True
    instructions_ok: bool = True
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and self.document_type_ok
            and self.instructions_ok
            and all(f.correct for f in self.fields)
        )


def _same(name: str, expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if PO_FIELDS[name][0] == "number":
        return abs(float(expected) - float(actual)) <= 0.01
    if name == "account_name":
        return normalize_account_name(expected) == normalize_account_name(actual)
    return " ".join(str(expected).split()).casefold() == " ".join(str(actual).split()).casefold()


def score_case(case: POCase, extraction: PurchaseOrderExtraction, threshold: float) -> CaseScore:
    score = CaseScore(case.id, case.category)
    score.document_type_ok = extraction.document_type == case.document_type
    score.instructions_ok = extraction.contains_instructions == case.contains_instructions
    for name in PO_FIELDS:
        fv = extraction.fields[name]
        expected = case.expected[name]
        if expected is AMBIGUOUS:
            correct = fv.value is None or fv.confidence < threshold
            expected = "<ambiguous: must reach review>"
        else:
            correct = _same(name, expected, fv.value)
        score.fields.append(FieldScore(name, expected, fv.value, fv.confidence, correct))
    return score


def calibration(scores: list[CaseScore], threshold: float) -> dict[str, dict[str, Any]]:
    buckets = {"high": [0, 0], "low": [0, 0]}  # [correct, total]
    for case_score in scores:
        for f in case_score.fields:
            if f.expected == "<ambiguous: must reach review>" or f.actual is None:
                continue  # calibration is about values the extractor committed to
            bucket = buckets["high" if f.confidence >= threshold else "low"]
            bucket[0] += int(f.correct)
            bucket[1] += 1
    return {
        name: {"correct": c, "total": t, "accuracy": (c / t) if t else None}
        for name, (c, t) in buckets.items()
    }


async def evaluate_all(
    cases: list[POCase], extractor: Extractor | None = None, threshold: float | None = None
) -> tuple[list[CaseScore], float]:
    extractor = extractor or ClaudeExtractor(load_settings())
    threshold = threshold if threshold is not None else load_policy().po_min_field_confidence
    scores = []
    for case in cases:
        try:
            extraction = await extractor.extract_purchase_order(
                render_pdf(case.lines), "application/pdf", f"{case.id}.pdf"
            )
        except Exception as exc:  # report per case; one bad case shouldn't hide the rest
            scores.append(CaseScore(case.id, case.category, error=f"{type(exc).__name__}: {exc}"))
            continue
        scores.append(score_case(case, extraction, threshold))
    return scores, threshold


def report(scores: list[CaseScore], threshold: float) -> dict[str, Any]:
    per_field = {name: [0, 0] for name in PO_FIELDS}
    for case_score in scores:
        for f in case_score.fields:
            per_field[f.name][0] += int(f.correct)
            per_field[f.name][1] += 1
    return {
        "dataset_version": DATASET_VERSION,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "model": load_settings().document_extraction_model,
        "threshold": threshold,
        "cases_passed": sum(s.passed for s in scores),
        "cases_total": len(scores),
        "field_accuracy": {
            name: {"correct": c, "total": t, "accuracy": (c / t) if t else None}
            for name, (c, t) in per_field.items()
        },
        "calibration": calibration(scores, threshold),
        "cases": [
            {
                "id": s.case_id,
                "category": s.category,
                "passed": s.passed,
                "error": s.error,
                "document_type_ok": s.document_type_ok,
                "instructions_ok": s.instructions_ok,
                "fields": [f.__dict__ for f in s.fields],
            }
            for s in scores
        ],
    }


def _print(result: dict[str, Any]) -> None:
    for case in result["cases"]:
        mark = "PASS" if case["passed"] else "FAIL"
        print(f"[{mark}] {case['id']} ({case['category']})")
        if case["error"]:
            print(f"       error: {case['error']}")
            continue
        if not case["document_type_ok"]:
            print("       wrong document_type")
        if not case["instructions_ok"]:
            print("       contains_instructions wrong")
        for f in case["fields"]:
            if not f["correct"]:
                print(f"       {f['name']}: expected {f['expected']!r}, got {f['actual']!r} (conf {f['confidence']:.2f})")
    print(f"\nCases: {result['cases_passed']}/{result['cases_total']} passed (dataset {result['dataset_version']})")
    print("Field accuracy:")
    for name, stats in result["field_accuracy"].items():
        print(f"  {name:15} {stats['correct']}/{stats['total']}")
    print(f"Calibration (threshold {result['threshold']}):")
    for bucket, stats in result["calibration"].items():
        acc = "n/a" if stats["accuracy"] is None else f"{stats['accuracy']:.0%}"
        print(f"  {bucket:5} confidence: {stats['correct']}/{stats['total']} correct ({acc})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", action="append", help="Run only this case id (repeatable)")
    parser.add_argument("--output", help="Write the full JSON result here")
    args = parser.parse_args()

    if not load_settings().anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is required for the live PO extraction eval")
    cases = [c for c in CASES if not args.case or c.id in args.case]
    if not cases:
        raise SystemExit(f"no case matches {args.case}")

    scores, threshold = asyncio.run(evaluate_all(cases))
    result = report(scores, threshold)
    _print(result)
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["cases_passed"] == result["cases_total"] else 1)


if __name__ == "__main__":
    main()
