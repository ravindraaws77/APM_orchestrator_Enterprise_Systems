"""Runs rag_cases.CASES through the real grounded-RAG pipeline and the
real Claude API (RAG_MODEL), and grades each case.

Verdicts:
- PASS: the correct outcome for the case's category (see rag_cases.py).
- MISS: an answerable question came back not grounded. Safe -- nothing
  wrong was shown to anyone -- but it is lost recall.
- SAFE: a non-ideal but safe outcome (``needs_human_review`` where
  ``insufficient_evidence`` was expected, or an injection case that
  refused to answer).
- FAIL: something wrong would have reached a human as a grounded answer
  -- a hallucinated answer to an unanswerable question, a wrong value,
  a citation to the wrong document, or an injected value.

The exit code is 1 only for FAILs. MISS/SAFE rates are reported (and in
the JSON output) but don't fail the run: they measure recall and gate
tuning, not safety.

Uses the shipped Order-Renewal policy's contract_terms settings (top_k,
min_score, chunking, and the term questions for ``term_key`` cases), so
it evaluates what production runs. ``--retrieval-only`` runs just the
deterministic retrieval + abstain-gate half (no API key, no cost); the
same check runs offline in tests/test_rag_eval.py on every push.

Usage:
    python -m apm_orchestrator.evals.run_rag_eval
    python -m apm_orchestrator.evals.run_rag_eval --output rag-eval.json
    python -m apm_orchestrator.evals.run_rag_eval --retrieval-only

Needs the rag extra and ANTHROPIC_API_KEY (except --retrieval-only).
Makes one model call per case that passes the abstain gate (~16).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apm_orchestrator.agents.order_renewal.policy import ContractTermsPolicy, load_policy
from apm_orchestrator.evals.rag_cases import CASES, CORPUS, RagCase
from apm_orchestrator.rag.answer import AnswerModel, answer_question
from apm_orchestrator.rag.chunking import chunk_documents
from apm_orchestrator.rag.grounding import normalise
from apm_orchestrator.rag.index import BM25Index
from apm_orchestrator.rag.loaders import load_bytes

ANSWERABLE_LIKE = frozenset({"answerable", "paraphrased", "negative", "cross_document"})


def build_index(docs: tuple[str, ...], cfg: ContractTermsPolicy) -> BM25Index:
    documents = [load_bytes(name, name, "text/plain", CORPUS[name].encode()) for name in docs]
    return BM25Index(chunk_documents(documents, chunk_chars=cfg.chunk_chars, overlap_chars=cfg.overlap_chars))


def resolve(case: RagCase, cfg: ContractTermsPolicy) -> tuple[str, str | None]:
    """(question for the model, retrieval query or None)."""
    if case.term_key:
        term = next(q for q in cfg.questions if q.key == case.term_key)
        return term.question, term.query
    if not case.question:
        raise ValueError(f"{case.label}: needs question or term_key")
    return case.question, None


@dataclass(frozen=True)
class RetrievalCheck:
    label: str
    gate_passed: bool
    best_relevance: float
    evidence_rank: int | None  # rank of the first retrieved chunk holding the evidence

    @property
    def evidence_retrieved(self) -> bool:
        return self.evidence_rank is not None


def retrieval_check(case: RagCase, cfg: ContractTermsPolicy, index: BM25Index | None = None) -> RetrievalCheck:
    """The deterministic half: what the retriever hands the model, and
    whether the abstain gate lets it through. No model involved."""
    index = index or build_index(case.docs, cfg)
    question, query = resolve(case, cfg)
    hits = index.search(query or question, cfg.top_k)
    best = max((h.relevance for h in hits), default=0.0)
    rank = None
    if case.evidence:
        ev = normalise(case.evidence)
        rank = next((i for i, h in enumerate(hits) if ev in normalise(h.chunk.text)), None)
    return RetrievalCheck(case.label, bool(hits) and best >= cfg.min_score, best, rank)


def grade(case: RagCase, result: dict[str, Any]) -> tuple[str, str]:
    """(verdict, reason) for one case's RagResult.to_dict()."""
    status = result["status"]
    answer = (result.get("answer") or "").casefold()
    cited_docs = {c.get("doc_name") for c in result.get("citations", []) if c.get("ok")}

    def wrong_value() -> str | None:
        for pattern in case.forbidden:
            if re.search(pattern, answer, re.IGNORECASE):
                return f"forbidden_value:{pattern}"
        if case.answer_any and not any(a.casefold() in answer for a in case.answer_any):
            return "wrong_answer"
        if case.expected_doc and case.expected_doc not in cited_docs:
            return "wrong_document"
        return None

    if case.category in ANSWERABLE_LIKE or case.category == "injection":
        if status != "grounded":
            if case.category == "injection":
                return "SAFE", f"not grounded ({status})"
            return "MISS", f"not grounded ({status}: {result.get('reason')})"
        problem = wrong_value()
        return ("FAIL", problem) if problem else ("PASS", "grounded, correct, cited")

    if case.category in ("unanswerable", "off_topic"):
        if status == "grounded":
            return "FAIL", f"hallucinated grounded answer: {result.get('answer')!r}"
        if status == "needs_human_review":
            return "SAFE", f"needs_human_review ({result.get('reason')})"
        if case.category == "off_topic" and case.gate_should_abstain and result.get("model_called"):
            return "SAFE", "insufficient_evidence, but the abstain gate let it reach the model"
        return "PASS", f"insufficient_evidence ({result.get('reason')})"

    raise ValueError(f"unknown category {case.category!r}")


async def evaluate_all(
    cases: list[RagCase], model: AnswerModel, cfg: ContractTermsPolicy
) -> list[tuple[RagCase, dict[str, Any], str, str]]:
    indexes: dict[tuple[str, ...], BM25Index] = {}
    out = []
    for case in cases:
        index = indexes.setdefault(case.docs, build_index(case.docs, cfg))
        question, query = resolve(case, cfg)
        result = (
            await answer_question(question, index, model, top_k=cfg.top_k, min_score=cfg.min_score, query=query)
        ).to_dict()
        verdict, reason = grade(case, result)
        out.append((case, result, verdict, reason))
    return out


def summarize(outcomes: list[tuple[RagCase, dict[str, Any], str, str]]) -> dict[str, Any]:
    verdicts = Counter(v for _, _, v, _ in outcomes)
    by_category: dict[str, Counter] = {}
    for case, _, v, _ in outcomes:
        by_category.setdefault(case.category, Counter())[v] += 1
    answerable = [(c, v) for c, _, v, _ in outcomes if c.category in ANSWERABLE_LIKE]
    negatives = [(c, r) for c, r, _, _ in outcomes if c.category in ("unanswerable", "off_topic")]
    return {
        "verdicts": dict(verdicts),
        "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
        "recall": round(sum(v == "PASS" for _, v in answerable) / len(answerable), 3) if answerable else None,
        "hallucinations": sum(r["status"] == "grounded" for _, r in negatives),
        "model_calls": sum(bool(r["model_called"]) for _, r, _, _ in outcomes),
        "verify_gate_rejections": sum(
            (r.get("reason") or "").startswith(("citation_verification_failed", "answer_without_citations"))
            for _, r, _, _ in outcomes
        ),
        "tokens": {
            "input": sum(r["usage"].get("input_tokens", 0) for _, r, _, _ in outcomes),
            "output": sum(r["usage"].get("output_tokens", 0) for _, r, _, _ in outcomes),
        },
    }


def _retrieval_report(cfg: ContractTermsPolicy) -> int:
    print(f"{'case':28} {'category':14} {'gate':8} {'best':>5}  evidence")
    for case in CASES:
        chk = retrieval_check(case, cfg)
        ev = "-" if not case.evidence else (f"rank {chk.evidence_rank}" if chk.evidence_retrieved else "NOT RETRIEVED")
        gate = "pass" if chk.gate_passed else "abstain"
        print(f"{case.label:28} {case.category:14} {gate:8} {chk.best_relevance:5.2f}  {ev}")
    return 0


async def main(output: Path | None, retrieval_only: bool) -> int:
    cfg = load_policy().contract_terms
    if retrieval_only:
        return _retrieval_report(cfg)

    from apm_orchestrator.config import load_settings
    from apm_orchestrator.rag.runtime import build_answer_model

    settings = load_settings()
    outcomes = await evaluate_all(CASES, build_answer_model(settings), cfg)
    for case, result, verdict, reason in outcomes:
        print(f"[{verdict:4}] {case.label:28} {case.category:14} {result['status']:22} {reason}")
    summary = summarize(outcomes)
    print(json.dumps(summary, indent=2))

    if output:
        output.write_text(
            json.dumps(
                {
                    "ran_at": datetime.now(timezone.utc).isoformat(),
                    "rag_model": settings.rag_model,
                    "policy": {"top_k": cfg.top_k, "min_score": cfg.min_score, "chunk_chars": cfg.chunk_chars},
                    "summary": summary,
                    "cases": [
                        {"label": c.label, "category": c.category, "verdict": v, "reason": why, "result": r}
                        for c, r, v, why in outcomes
                    ],
                },
                indent=2,
                default=str,
            )
        )
    return 1 if summary["verdicts"].get("FAIL") else 0


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help="write full per-case results as JSON")
    parser.add_argument("--retrieval-only", action="store_true", help="deterministic retrieval check only; no API calls")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.output, args.retrieval_only)))


if __name__ == "__main__":
    cli()
