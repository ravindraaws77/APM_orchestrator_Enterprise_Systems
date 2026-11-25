"""Cited contract-term extraction for a renewal (use case U1).

One grounded question per term, each through the full gated pipeline,
so every term is independently ``grounded`` / ``insufficient_evidence``
/ ``needs_human_review``. Only grounded terms are ever rendered into a
proposal description, each with its document and page.

Best-effort by design: this never raises into the caller and never
decides anything -- the case graph continues whatever comes back.
"""

from __future__ import annotations

from dataclasses import dataclass

from apm_orchestrator.rag.answer import DEFAULT_MIN_SCORE, DEFAULT_TOP_K, AnswerModel, RagResult, answer_question
from apm_orchestrator.rag.index import Retriever


@dataclass(frozen=True)
class TermQuestion:
    key: str
    label: str
    question: str  # what the model is asked
    query: str | None = None  # retrieval wording, in contract vocabulary; defaults to question


DEFAULT_TERM_QUESTIONS: tuple[TermQuestion, ...] = (
    TermQuestion(
        "notice_period", "Notice period",
        "What notice period is required to terminate or not renew the agreement?",
        "written notice of non-renewal termination days prior to end of term",
    ),
    TermQuestion(
        "renewal_term", "Renewal term",
        "What is the renewal term length when the agreement renews?",
        "renewal term renews successive months years",
    ),
    TermQuestion(
        "auto_renew", "Auto-renewal",
        "Does the agreement renew automatically (auto-renewal / evergreen clause)?",
        "renews automatically auto-renewal evergreen successive renewal term",
    ),
    TermQuestion(
        "uplift_cap", "Price uplift cap",
        "What is the maximum price increase or uplift cap allowed at renewal?",
        "fees increase uplift price cap percent renewal term no more than",
    ),
)


async def extract_contract_terms(
    retriever: Retriever,
    model: AnswerModel,
    questions: tuple[TermQuestion, ...] | list[TermQuestion] = DEFAULT_TERM_QUESTIONS,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict[str, RagResult]:
    results: dict[str, RagResult] = {}
    for q in questions:
        results[q.key] = await answer_question(
            q.question, retriever, model, top_k=top_k, min_score=min_score, query=q.query
        )
    return results


def format_terms_for_description(
    results: dict[str, RagResult],
    questions: tuple[TermQuestion, ...] | list[TermQuestion] = DEFAULT_TERM_QUESTIONS,
) -> str:
    """Plain-text block for a proposal description: grounded terms with
    their source, and an honest marker for the rest. Empty string if no
    term was asked."""
    lines = []
    for q in questions:
        r = results.get(q.key)
        if r is None:
            continue
        if r.status == "grounded" and r.answer:
            sources = sorted({f"{c.doc_name} p.{c.page}" for c in r.citations if c.ok})
            lines.append(f"- {q.label}: {r.answer} [source: {'; '.join(sources)}]")
        elif r.status == "needs_human_review":
            lines.append(f"- {q.label}: needs human review (citations could not be verified)")
        else:
            lines.append(f"- {q.label}: not found in contract documents")
    return ("Contract terms (cited, machine-extracted -- verify before relying on them):\n" + "\n".join(lines)) if lines else ""
