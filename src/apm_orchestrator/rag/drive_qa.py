"""Drive documents -> retriever -> grounded answer, in one place.

Shared by every caller (the case graph's contract-terms node, the
``contract_qa`` SDK tool, the Strands ``ask_contract`` tool and
``scripts/ask_docs.py``) so they all apply the same loaders, chunking,
gates and limits. Read-only: it only ever calls ``drive_list_files`` and
``drive_read_file``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apm_orchestrator.rag.answer import DEFAULT_MIN_SCORE, DEFAULT_TOP_K, AnswerModel, RagResult, answer_question
from apm_orchestrator.rag.chunking import DEFAULT_CHUNK_CHARS, DEFAULT_OVERLAP_CHARS, chunk_documents
from apm_orchestrator.rag.index import BM25Index
from apm_orchestrator.rag.loaders import Document, DriveReader, SkippedFile, load_drive_documents

DEFAULT_MAX_FILES = 5


@dataclass(frozen=True)
class DriveCorpus:
    index: BM25Index | None  # None when nothing readable was found
    documents: list[Document] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "documents": [{"file_id": d.doc_id, "name": d.name, "pages": len(d.pages)} for d in self.documents],
            "skipped": [{"file_id": s.doc_id, "name": s.name, "reason": s.reason} for s in self.skipped],
        }


async def build_drive_corpus(
    client: DriveReader,
    name_contains: str | None,
    max_files: int = DEFAULT_MAX_FILES,
    process_id: str | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> DriveCorpus:
    docs, skipped = await load_drive_documents(client, name_contains=name_contains, max_files=max_files, process_id=process_id)
    chunks = chunk_documents(docs, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
    return DriveCorpus(index=BM25Index(chunks) if chunks else None, documents=docs, skipped=skipped)


async def ask_drive(
    client: DriveReader,
    question: str,
    model: AnswerModel,
    name_contains: str | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    process_id: str | None = None,
) -> dict[str, Any]:
    """Answer one question over matching Drive files. Returns a JSON-safe
    dict: the ``RagResult`` fields plus which files were read or skipped.
    No readable file is ``insufficient_evidence`` without a model call."""
    corpus = await build_drive_corpus(client, name_contains, max_files=max_files, process_id=process_id)
    if corpus.index is None:
        result = RagResult(question, "insufficient_evidence", None, reason="no_readable_documents")
    else:
        result = await answer_question(question, corpus.index, model, top_k=top_k, min_score=min_score)
    return {**result.to_dict(), **corpus.describe()}
