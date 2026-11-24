"""Grounded retrieval-augmented answering over Drive documents.

Pipeline: load (text / markdown / PDF per page) -> chunk (stable ids,
page refs) -> retrieve (BM25 today; dense retrieval is pluggable) ->
ABSTAIN GATE (weak evidence never reaches a model) -> one Claude call
with structured output (answer + [{chunk_id, quote}]) -> VERIFY GATE
(every quote must appear verbatim, after normalisation, in a chunk that
was actually retrieved) -> status ``grounded`` | ``insufficient_evidence``
| ``needs_human_review``.

Hard rule: an answer whose citations failed verification is never
returned -- ``RagResult.answer`` is only populated when status is
``grounded``.

This package has no agent-framework imports (no Claude Agent SDK, no
LangGraph, no Strands): the case graph, the SDK tools and the Strands
spike all call into it, never the other way round. The Anthropic SDK is
imported lazily, only by ``AnthropicAnswerModel``.
"""

from apm_orchestrator.rag.answer import (
    AnswerModel,
    AnthropicAnswerModel,
    RagResult,
    answer_question,
)
from apm_orchestrator.rag.chunking import Chunk, chunk_document, chunk_documents
from apm_orchestrator.rag.grounding import Citation, VerifiedCitation, normalise, verify_citations
from apm_orchestrator.rag.index import BM25Index, DenseIndex, Embedder, Retriever, ScoredChunk
from apm_orchestrator.rag.loaders import Document, Page, SkippedFile, load_bytes, load_drive_documents

__all__ = [
    "AnswerModel",
    "AnthropicAnswerModel",
    "BM25Index",
    "Chunk",
    "Citation",
    "DenseIndex",
    "Document",
    "Embedder",
    "Page",
    "RagResult",
    "Retriever",
    "ScoredChunk",
    "SkippedFile",
    "VerifiedCitation",
    "answer_question",
    "chunk_document",
    "chunk_documents",
    "load_bytes",
    "load_drive_documents",
    "normalise",
    "verify_citations",
]
