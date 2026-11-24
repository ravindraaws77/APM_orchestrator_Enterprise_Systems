"""The verify gate: a citation counts only if its quote appears in a
chunk that was actually retrieved for this question.

Comparison is on normalised text (Unicode NFKC, curly quotes and dashes
folded to ASCII, whitespace collapsed, casefolded) so harmless
formatting differences -- a PDF line break, a smart quote -- don't fail
an honest citation, while any change of wording still does.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from collections.abc import Iterable

from apm_orchestrator.rag.chunking import Chunk

MIN_QUOTE_CHARS = 8  # after normalisation; shorter quotes prove nothing

_TRANSLATE = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
        "­": "",  # soft hyphen
        " ": " ",
    }
)
_WS_RE = re.compile(r"\s+")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE)
    return _WS_RE.sub(" ", text).strip().casefold()


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    quote: str


@dataclass(frozen=True)
class VerifiedCitation:
    chunk_id: str
    quote: str
    ok: bool
    reason: str | None = None  # why it failed: unretrieved_chunk | quote_too_short | quote_not_found
    doc_id: str | None = None
    doc_name: str | None = None
    page: int | None = None


def verify_citations(citations: Iterable[Citation], retrieved: Iterable[Chunk]) -> list[VerifiedCitation]:
    by_id = {c.chunk_id: c for c in retrieved}
    out: list[VerifiedCitation] = []
    for cit in citations:
        chunk = by_id.get(cit.chunk_id)
        if chunk is None:
            # Covers both a made-up id and a real chunk that wasn't in
            # this question's retrieved set: either way the model didn't
            # see it here, so it can't be evidence for this answer.
            out.append(VerifiedCitation(cit.chunk_id, cit.quote, False, "unretrieved_chunk"))
            continue
        meta = {"doc_id": chunk.doc_id, "doc_name": chunk.doc_name, "page": chunk.page}
        q = normalise(cit.quote)
        if len(q) < MIN_QUOTE_CHARS:
            out.append(VerifiedCitation(cit.chunk_id, cit.quote, False, "quote_too_short", **meta))
        elif q not in normalise(chunk.text):
            out.append(VerifiedCitation(cit.chunk_id, cit.quote, False, "quote_not_found", **meta))
        else:
            out.append(VerifiedCitation(cit.chunk_id, cit.quote, True, None, **meta))
    return out
