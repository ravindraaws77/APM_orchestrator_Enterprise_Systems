"""Retrieval: a ``Retriever`` protocol, a dependency-free BM25 index, and
a pluggable dense index for when an embedding model is wired in later.

Every hit carries two numbers:

- ``score``: the retriever's own ranking score (BM25 or cosine). Used
  only to order results; its scale depends on the retriever and, for
  BM25, on corpus size -- a single short contract gives every term a
  tiny IDF, so a fixed raw-score threshold would abstain on good
  questions exactly in the small per-account corpora U1 runs on.
- ``relevance``: a normalised [0, 1] signal the abstain gate compares
  with ``min_score``. For BM25 it is the IDF-weighted share of the
  question's terms that occur in the chunk (a term absent from the
  corpus weighs as much as its rarest term, so off-topic questions score
  ~0, and on a one-chunk corpus this is simply the share of question
  terms found); for dense retrieval
  it is the cosine similarity clipped at 0.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Protocol

from apm_orchestrator.rag.chunking import Chunk

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Deliberately small: only words with no retrieval value in contract text.
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were will with "
    "what which who whom does do did how".split()
)


_SUFFIXES = ("ing", "ed", "es", "s")


def _stem(token: str) -> str:
    """Crude suffix strip so governs/governed/governing, law/laws and
    require/required/requires meet. Deliberately conservative."""
    for suf in _SUFFIXES:
        if len(token) > len(suf) + 3 and token.endswith(suf):
            token = token[: -len(suf)]
            break
    return token[:-1] if len(token) > 4 and token.endswith("e") else token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.casefold()) if t not in _STOPWORDS and len(t) > 1]


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float  # retriever-specific ranking score
    relevance: float = 1.0  # normalised [0, 1]; what the abstain gate checks


class Retriever(Protocol):
    def search(self, query: str, top_k: int) -> list[ScoredChunk]: ...


class BM25Index:
    """Okapi BM25 over chunk text. Chunks with no query-term overlap are
    never returned, so an off-topic question retrieves nothing."""

    def __init__(self, chunks: Sequence[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = list(chunks)
        self.k1, self.b = k1, b
        self._tfs = [Counter(tokenize(c.text)) for c in self.chunks]
        self._lens = [sum(tf.values()) for tf in self._tfs]
        self._avgdl = (sum(self._lens) / len(self._lens)) if self._lens else 0.0
        df: Counter[str] = Counter()
        for tf in self._tfs:
            df.update(tf.keys())
        n = len(self.chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self._max_idf = max(self._idf.values(), default=1.0)

    def _term_idf(self, term: str) -> float:
        # An unseen term weighs as much as the rarest seen term -- not the
        # theoretical df=0 IDF, which on a one- or two-chunk corpus would
        # dwarf every real match and make relevance meaningless.
        return self._idf.get(term, self._max_idf)

    def search(self, query: str, top_k: int) -> list[ScoredChunk]:
        query_terms = list(dict.fromkeys(tokenize(query)))
        terms = [t for t in query_terms if t in self._idf]
        if not terms or top_k <= 0:
            return []
        total_weight = sum(self._term_idf(t) for t in query_terms)
        scored: list[ScoredChunk] = []
        for chunk, tf, dl in zip(self.chunks, self._tfs, self._lens):
            score, matched = 0.0, 0.0
            for t in terms:
                f = tf.get(t, 0)
                if f:
                    norm = self.k1 * (1 - self.b + self.b * dl / (self._avgdl or 1.0))
                    score += self._idf[t] * f * (self.k1 + 1) / (f + norm)
                    matched += self._idf[t]
            if score > 0:
                scored.append(ScoredChunk(chunk, score, matched / total_weight))
        # Stable tie-break on chunk_id so results are deterministic.
        scored.sort(key=lambda s: (-s.score, s.chunk.chunk_id))
        return scored[:top_k]


class Embedder(Protocol):
    """Pluggable embedding model. Not wired to any provider yet."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class DenseIndex:
    """Cosine-similarity retrieval over any ``Embedder``. A drop-in
    ``Retriever`` alternative to BM25; ``min_score`` likely needs
    re-tuning for it, since cosine relevance is distributed differently."""

    def __init__(self, chunks: Sequence[Chunk], embedder: Embedder) -> None:
        self.chunks = list(chunks)
        self.embedder = embedder
        self._vectors = embedder.embed([c.text for c in self.chunks]) if self.chunks else []

    def search(self, query: str, top_k: int) -> list[ScoredChunk]:
        if not self.chunks or top_k <= 0:
            return []
        q = self.embedder.embed([query])[0]
        scored = []
        for c, v in zip(self.chunks, self._vectors):
            cos = _cosine(q, v)
            scored.append(ScoredChunk(c, cos, max(0.0, min(1.0, cos))))
        scored.sort(key=lambda s: (-s.score, s.chunk.chunk_id))
        return scored[:top_k]
