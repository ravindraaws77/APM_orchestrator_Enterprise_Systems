"""Split documents into overlapping, page-scoped chunks with stable ids.

A chunk never spans two pages, so every citation maps to exactly one
page. Ids are ``{doc_id}#p{page}c{n}`` -- deterministic for the same
document text and settings, so an eval or a stored citation can refer
to a chunk across runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apm_orchestrator.rag.loaders import Document

DEFAULT_CHUNK_CHARS = 800
DEFAULT_OVERLAP_CHARS = 150
_BREAK_RE = re.compile(r"(?<=[.!?;:])\s+|\n\s*\n|\n")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    doc_name: str
    page: int
    text: str
    start: int  # char offset within the page text
    end: int


def _cut_point(text: str, start: int, target: int) -> int:
    """Best boundary in [start + target/2, start + target]: a paragraph
    or sentence break, else whitespace, else the hard limit."""
    hard = min(len(text), start + target)
    if hard >= len(text):
        return len(text)
    window_start = start + target // 2
    window = text[window_start:hard]
    breaks = [m.end() for m in _BREAK_RE.finditer(window)]
    if breaks:
        return window_start + breaks[-1]
    space = window.rfind(" ")
    return window_start + space + 1 if space >= 0 else hard


def chunk_document(
    doc: Document, chunk_chars: int = DEFAULT_CHUNK_CHARS, overlap_chars: int = DEFAULT_OVERLAP_CHARS
) -> list[Chunk]:
    if chunk_chars <= 0 or not 0 <= overlap_chars < chunk_chars:
        raise ValueError("need chunk_chars > 0 and 0 <= overlap_chars < chunk_chars")
    chunks: list[Chunk] = []
    for page in doc.pages:
        text = page.text
        start, n = 0, 0
        while start < len(text):
            end = _cut_point(text, start, chunk_chars)
            piece = text[start:end].strip()
            if piece:
                chunks.append(
                    Chunk(
                        chunk_id=f"{doc.doc_id}#p{page.number}c{n}",
                        doc_id=doc.doc_id,
                        doc_name=doc.name,
                        page=page.number,
                        text=piece,
                        start=start,
                        end=end,
                    )
                )
                n += 1
            if end >= len(text):
                break
            next_start = max(end - overlap_chars, start + 1)
            # Start the overlap on a word boundary, not mid-word.
            space = text.find(" ", next_start, end)
            start = space + 1 if space != -1 else next_start
    return chunks


def chunk_documents(
    docs: list[Document], chunk_chars: int = DEFAULT_CHUNK_CHARS, overlap_chars: int = DEFAULT_OVERLAP_CHARS
) -> list[Chunk]:
    return [c for d in docs for c in chunk_document(d, chunk_chars, overlap_chars)]
