"""PO extraction golden-set eval.

The scoring logic and the PDF rendering are tested offline (a canned
extractor that returns each case's own labels must score 100%). The live
run against the real Claude API is skipped unless ANTHROPIC_API_KEY is
set, like test_supervisor_routing_eval.py -- see .github/workflows/eval.yml.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from apm_orchestrator.document_extraction.schema import PO_FIELDS, parse_extraction
from apm_orchestrator.evals.minimal_pdf import render_pdf
from apm_orchestrator.evals.po_extraction_cases import AMBIGUOUS, CASES
from apm_orchestrator.evals.run_po_extraction_eval import calibration, evaluate_all

THRESHOLD = 0.8


class _OracleExtractor:
    """Returns each case's labels -- ambiguous fields at low confidence."""

    def __init__(self):
        self._by_filename = {f"{c.id}.pdf": c for c in CASES}

    async def extract_purchase_order(self, document, media_type, filename=None):
        assert document.startswith(b"%PDF-1.4") and media_type == "application/pdf"
        case = self._by_filename[filename]
        fields = {}
        for name in PO_FIELDS:
            expected = case.expected[name]
            if expected is AMBIGUOUS:
                fields[name] = {"value": 1.0, "confidence": 0.3, "evidence": None}
            else:
                fields[name] = {"value": expected, "confidence": 0.95 if expected is not None else 0.0, "evidence": None}
        return parse_extraction(
            {
                "document_type": case.document_type,
                "fields": fields,
                "contains_instructions": case.contains_instructions,
                "notes": "",
            }
        )


def test_dataset_labels_are_valid():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    for case in CASES:
        assert set(case.expected) == set(PO_FIELDS), case.id
        render_pdf(case.lines)  # every line encodes


def test_oracle_scores_perfectly():
    scores, _ = asyncio.run(evaluate_all(CASES, _OracleExtractor(), THRESHOLD))
    assert all(s.passed for s in scores), [s.case_id for s in scores if not s.passed]


def test_confident_wrong_value_fails_and_shows_in_calibration():
    class Wrong(_OracleExtractor):
        async def extract_purchase_order(self, document, media_type, filename=None):
            extraction = await super().extract_purchase_order(document, media_type, filename)
            payload = extraction.to_dict()
            payload["fields"]["po_number"] = {"value": "WRONG-1", "confidence": 0.99, "evidence": None}
            return parse_extraction(payload)

    scores, _ = asyncio.run(evaluate_all(CASES[:1], Wrong(), THRESHOLD))
    assert not scores[0].passed
    assert calibration(scores, THRESHOLD)["high"]["correct"] < calibration(scores, THRESHOLD)["high"]["total"]


def test_rendered_pdf_is_readable():
    pypdf = pytest.importorskip("pypdf")
    import io

    text = pypdf.PdfReader(io.BytesIO(render_pdf(["Net amount £36,000.00", "Laufzeit: (2027)"]))).pages[0].extract_text()
    assert "£36,000.00" in text and "(2027)" in text


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set -- skipping live PO eval")
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_live_extraction(case):
    scores, _ = asyncio.run(evaluate_all([case], threshold=THRESHOLD))
    score = scores[0]
    assert score.error is None, score.error
    wrong = [(f.name, f.expected, f.actual, f.confidence) for f in score.fields if not f.correct]
    assert not wrong, wrong
    assert score.document_type_ok
    assert score.instructions_ok
