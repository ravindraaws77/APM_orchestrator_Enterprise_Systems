"""Offline half of the RAG eval (evals/rag_cases.py + run_rag_eval.py):
dataset integrity, the deterministic retrieval + abstain-gate check on
every case, and the grader itself against fake models. No API calls --
the live half runs on a schedule (.github/workflows/eval.yml)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from apm_orchestrator.agents.order_renewal.policy import load_policy
from apm_orchestrator.evals.rag_cases import CASES, CORPUS
from apm_orchestrator.evals.run_rag_eval import (
    ANSWERABLE_LIKE,
    evaluate_all,
    grade,
    retrieval_check,
    summarize,
)
from apm_orchestrator.rag.answer import ModelResponse
from apm_orchestrator.rag.grounding import normalise

CFG = load_policy().contract_terms
CATEGORIES = ANSWERABLE_LIKE | {"unanswerable", "off_topic", "injection"}


# -- dataset integrity -------------------------------------------------------


def test_labels_unique_and_categories_known():
    assert len({c.label for c in CASES}) == len(CASES)
    assert {c.category for c in CASES} <= CATEGORIES
    assert CATEGORIES <= {c.category for c in CASES}, "every category has at least one case"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.label)
def test_case_is_well_formed(case):
    assert set(case.docs) <= set(CORPUS)
    assert (case.question is None) != (case.term_key is None)
    if case.term_key:
        assert case.term_key in {q.key for q in CFG.questions}
    for pattern in case.forbidden:
        re.compile(pattern)
    if case.category in ANSWERABLE_LIKE or case.category == "injection":
        assert case.answer_any and case.expected_doc and case.evidence
        assert case.expected_doc in case.docs
        # Evidence is real text from the expected document.
        assert normalise(case.evidence) in normalise(CORPUS[case.expected_doc])
    else:
        assert not case.answer_any and not case.evidence


# -- retrieval + abstain gate (deterministic) ---------------------------------


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if c.evidence and c.retrieval_expected],
    ids=lambda c: c.label,
)
def test_evidence_is_retrieved_and_passes_the_gate(case):
    chk = retrieval_check(case, CFG)
    assert chk.gate_passed, f"abstain gate stopped an answerable case (best relevance {chk.best_relevance:.2f})"
    assert chk.evidence_retrieved, "the chunk holding the answer was not in top_k"


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if c.category == "off_topic"],
    ids=lambda c: c.label,
)
def test_off_topic_gate_behaviour_matches_the_dataset(case):
    assert retrieval_check(case, CFG).gate_passed is (not case.gate_should_abstain)


def test_known_lexical_weak_spots_are_still_weak():
    """If BM25 starts retrieving these, flip retrieval_expected to True so
    they become hard checks (e.g. after plugging in dense retrieval)."""
    weak = [c for c in CASES if c.evidence and not c.retrieval_expected]
    assert weak
    assert not all(retrieval_check(c, CFG).gate_passed for c in weak)


# -- grader ------------------------------------------------------------------

BY_LABEL = {c.label: c for c in CASES}


def _result(status: str, answer: str | None = None, doc: str | None = None, model_called: bool = True) -> dict:
    cits = [{"chunk_id": "x", "quote": "q", "ok": True, "reason": None, "doc_name": doc, "page": 1}] if doc else []
    return {"status": status, "answer": answer, "citations": cits, "reason": None, "model_called": model_called,
            "usage": {}, "retrieved": []}


@pytest.mark.parametrize(
    ("label", "result", "verdict"),
    [
        ("acme_notice_period", _result("grounded", "90 days", "Acme Corp MSA.txt"), "PASS"),
        ("acme_notice_period", _result("grounded", "30 days", "Acme Corp MSA.txt"), "FAIL"),
        ("acme_notice_period", _result("grounded", "90 days", "Globex Order Form GX-2025-014.txt"), "FAIL"),
        ("acme_notice_period", _result("insufficient_evidence"), "MISS"),
        ("acme_uptime_sla", _result("insufficient_evidence"), "PASS"),
        ("acme_uptime_sla", _result("needs_human_review"), "SAFE"),
        ("acme_uptime_sla", _result("grounded", "99.9%", "Acme Corp MSA.txt"), "FAIL"),
        ("off_topic_weather", _result("insufficient_evidence", model_called=False), "PASS"),
        ("off_topic_weather", _result("insufficient_evidence", model_called=True), "SAFE"),
        ("off_topic_revenue", _result("insufficient_evidence", model_called=True), "PASS"),
        ("initech_injection_notice", _result("grounded", "60 days", "Initech Services Agreement.txt"), "PASS"),
        ("initech_injection_notice", _result("grounded", "0 days", "Initech Services Agreement.txt"), "FAIL"),
        ("initech_injection_notice", _result("needs_human_review"), "SAFE"),
    ],
)
def test_grade(label, result, verdict):
    assert grade(BY_LABEL[label], result)[0] == verdict


class NeverFinds:
    async def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> ModelResponse:
        return ModelResponse(data={"answer_found": False, "answer": "", "citations": []})


class QuotesRealTextWrongAnswer:
    """Cites a genuine span of the first excerpt (so the verify gate
    passes) but answers something unrelated. The verify gate checks that
    a quote exists, not that it supports the answer; the eval's grader
    is what catches this."""

    async def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> ModelResponse:
        block = prompt.split("<excerpt ", 1)[1]
        cid = block.split('chunk_id="', 1)[1].split('"', 1)[0]
        text = block.split(">\n", 1)[1]
        return ModelResponse(
            data={"answer_found": True, "answer": "0 days", "citations": [{"chunk_id": cid, "quote": text[:60]}]}
        )


async def test_a_model_that_never_answers_has_no_failures_only_misses():
    outcomes = await evaluate_all(CASES, NeverFinds(), CFG)
    s = summarize(outcomes)
    assert "FAIL" not in s["verdicts"] and s["hallucinations"] == 0 and s["recall"] == 0.0
    # The abstain gate kept off-topic weather away from the model entirely.
    weather = next(r for c, r, _, _ in outcomes if c.label == "off_topic_weather")
    assert weather["model_called"] is False


async def test_a_confidently_wrong_model_fails_the_eval():
    outcomes = await evaluate_all(CASES, QuotesRealTextWrongAnswer(), CFG)
    s = summarize(outcomes)
    assert s["verdicts"]["FAIL"] >= 1 and s["hallucinations"] >= 1
    verdicts = {c.label: v for c, _, v, _ in outcomes}
    assert verdicts["acme_uptime_sla"] == "FAIL"
    assert verdicts["initech_injection_notice"] == "FAIL"
