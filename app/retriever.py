"""Vector retrieval: TF-IDF vectors (word 1-2grams) + cosine similarity, in memory.

Deviation note: a neural embedding model (e.g. Hugging Face sentence-transformers)
was not installable in the build environment, so sparse TF-IDF vectors are used as
the "embeddings". The Retriever interface (`retrieve`, `fingerprint`) is unchanged
if a dense model is swapped in.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from .chunking import Chunk


@dataclass(frozen=True)
class Passage:
    source: str
    chunk_id: str
    heading: str
    text: str
    score: float

    def to_source(self) -> dict:
        """Serializable source reference stored next to the answer."""
        return {"source": self.source, "chunk_id": self.chunk_id, "heading": self.heading,
                "score": round(self.score, 4), "snippet": self.text[:200]}


class TfidfRetriever:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self._vec = TfidfVectorizer(ngram_range=(1, 2), stop_words="english", sublinear_tf=True)
        self._matrix = self._vec.fit_transform(f"{c.heading}. {c.text}" for c in chunks)
        # Include every chunk field that can change retrieved context or returned
        # source metadata. JSON encoding gives unambiguous field boundaries, so
        # heading-only and source-only edits invalidate cached answers as well.
        corpus_identity = [
            [c.source, c.chunk_id, c.heading, c.text]
            for c in chunks
        ]
        encoded_identity = json.dumps(
            corpus_identity,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.fingerprint = hashlib.sha256(encoded_identity).hexdigest()[:16]

    def retrieve(self, question: str, top_k: int, min_score: float) -> list[Passage]:
        scores = linear_kernel(self._vec.transform([question]), self._matrix).ravel()
        order = scores.argsort()[::-1][:top_k]
        return [Passage(self.chunks[i].source, self.chunks[i].chunk_id, self.chunks[i].heading,
                        self.chunks[i].text, float(scores[i]))
                for i in order if scores[i] >= min_score]
