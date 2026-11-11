"""Answer generation.

Deviation note: no hosted-LLM SDK, API key or network access was available in the
build environment, and no local LLM weights. `ExtractiveGenerator` is therefore a
deterministic, local, *context-grounded* generator: it builds the answer only from
sentences of the retrieved passages that overlap the question. It is NOT a neural
LLM. Anything exposing `name` and `generate(question, passages) -> str` can replace
it (e.g. a Claude/OpenAI/Ollama client) without touching the cache logic.
"""
from __future__ import annotations

import re
import time

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

from .retriever import Passage


class GenerationError(Exception):
    """The generator failed. Result must not be cached."""


class NoAnswerError(GenerationError):
    """The retrieved context does not support an answer. Not an error, but not cacheable."""


def _terms(text: str) -> set[str]:
    return {t.rstrip("s") for t in re.findall(r"[a-z0-9_]+", text.lower())
            if t not in ENGLISH_STOP_WORDS and len(t) > 2}


class ExtractiveGenerator:
    name = "extractive-v1"

    def __init__(self, simulated_latency_ms: int = 0, max_sentences: int = 3):
        self.simulated_latency_ms = simulated_latency_ms
        self.max_sentences = max_sentences

    def generate(self, question: str, passages: list[Passage]) -> str:
        if self.simulated_latency_ms:  # benchmark-only, explicitly configured
            time.sleep(self.simulated_latency_ms / 1000)
        q_terms = _terms(question)
        scored = []
        for rank, p in enumerate(passages):
            for pos, sent in enumerate(re.split(r"(?<=[.!?])\s+", p.text)):
                overlap = len(q_terms & _terms(sent))
                if overlap:
                    scored.append((overlap, -rank, -pos, rank, pos, sent.strip()))
        if not scored:
            raise NoAnswerError("retrieved passages contain no sentence relevant to the question")
        best = sorted(scored, reverse=True)[: self.max_sentences]
        best.sort(key=lambda t: (t[3], t[4]))  # reading order
        return " ".join(t[5] for t in best)
