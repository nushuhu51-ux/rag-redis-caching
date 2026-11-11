"""RAG service with the mandatory Redis-first workflow.

ask(question):
  1. Redis check (BEFORE retrieval or generation)
  2. hit  -> return cached answer + sources (no retrieval, no generation)
  3. miss -> [single-flight lock] -> retrieve -> generate -> cache (answer + sources) -> return
"""
from __future__ import annotations

import logging
import threading
import time

from .cache import AnswerCache, CacheUnavailable
from .generator import GenerationError, NoAnswerError

log = logging.getLogger(__name__)

NO_ANSWER_TEXT = "I could not find an answer to that in the document collection."
POLL_INTERVAL_S = 0.05


class Stats:
    """Thread-safe counters used by /stats and the benchmark."""
    FIELDS = ("requests", "cache_hits", "cache_misses", "cache_unavailable", "cache_writes",
              "retrievals", "generations", "not_cached_unsuccessful", "coalesced_hits")

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._c = {f: 0 for f in self.FIELDS}

    def inc(self, name: str) -> None:
        with self._lock:
            self._c[name] += 1

    def snapshot(self) -> dict:
        with self._lock:
            c = dict(self._c)
        lookups = c["cache_hits"] + c["cache_misses"] + c["cache_unavailable"]
        c["cache_lookups"] = lookups
        c["hit_rate"] = round(c["cache_hits"] / lookups, 4) if lookups else None
        c["miss_rate"] = round(c["cache_misses"] / lookups, 4) if lookups else None
        return c


class RAGService:
    def __init__(self, retriever, generator, cache: AnswerCache | None,
                 top_k: int, min_score: float, stats: Stats | None = None):
        self.retriever, self.generator, self.cache = retriever, generator, cache
        self.top_k, self.min_score = top_k, min_score
        self.stats = stats or Stats()

    def ask(self, question: str) -> dict:
        t0 = time.perf_counter()
        question = (question or "").strip()
        if not question:
            raise ValueError("question must be a non-empty string")
        self.stats.inc("requests")

        # ---- Step 1: check Redis FIRST -------------------------------------------
        cache_status = "disabled"
        if self.cache is not None:
            try:
                hit = self.cache.get(question)
            except CacheUnavailable as exc:
                # Not a hit and not a miss: Redis is down. Fall through to the normal
                # pipeline (a freshly generated answer is always correct) but report it.
                log.warning("redis unavailable on lookup: %s", exc)
                self.stats.inc("cache_unavailable")
                cache_status = "unavailable"
            else:
                if hit is not None:
                    # ---- Step 2: cache hit -> return immediately -------------------
                    self.stats.inc("cache_hits")
                    return self._result(question, hit.answer, hit.sources, "hit", t0)
                self.stats.inc("cache_misses")
                cache_status = "miss"

        # ---- Step 3: cache miss -> (lock) -> retrieve -> generate -> cache -> return
        lock_token = None
        if cache_status == "miss":
            got = self._acquire_or_wait(question, t0)
            if isinstance(got, dict):  # another request finished the rebuild while we waited
                return got
            lock_token = got  # token, or None if Redis failed / waited too long (compute anyway)
        try:
            return self._compute(question, cache_status, t0)
        finally:
            if lock_token:
                self.cache.release_lock(question, lock_token)

    # -- miss path ---------------------------------------------------------------
    def _compute(self, question: str, cache_status: str, t0: float) -> dict:
        self.stats.inc("retrievals")
        passages = self.retriever.retrieve(question, self.top_k, self.min_score)
        if not passages:
            return self._unsuccessful(question, cache_status, t0)

        self.stats.inc("generations")
        try:
            answer = self.generator.generate(question, passages)
        except NoAnswerError:
            return self._unsuccessful(question, cache_status, t0)
        # any other GenerationError / exception propagates -> nothing is cached
        if not isinstance(answer, str) or not answer.strip():
            raise GenerationError("generator returned an empty answer")

        sources = [p.to_source() for p in passages]
        if self.cache is not None and cache_status == "miss":
            try:
                self.cache.set(question, answer, sources)
                self.stats.inc("cache_writes")
            except CacheUnavailable as exc:
                log.warning("redis unavailable on store: %s", exc)
                cache_status = "miss_not_stored"
        return self._result(question, answer, sources, cache_status, t0)

    def _acquire_or_wait(self, question: str, t0: float):
        """Stampede protection (single-flight). Only one concurrent request per key rebuilds the
        entry; the others poll Redis and are served the freshly cached answer.
        Returns a lock token (we rebuild), None (rebuild without a lock), or a result dict."""
        deadline = time.monotonic() + self.cache.lock_wait_seconds
        while True:
            token = None
            try:
                token = self.cache.try_lock(question)
                # Re-check the cache: the previous holder may have just finished (post-lock check),
                # or the holder we waited for may have stored the answer.
                hit = self.cache.get(question)
            except CacheUnavailable as exc:
                # try_lock() may have succeeded before the follow-up cache read
                # failed. Release only that acquired token; release_lock() uses
                # atomic compare-and-delete, so a successor's lock is protected.
                if token:
                    self.cache.release_lock(question, token)
                log.warning("redis unavailable during stampede lock: %s", exc)
                return None
            if hit is not None:
                if token:
                    self.cache.release_lock(question, token)
                self.stats.inc("coalesced_hits")
                return self._result(question, hit.answer, hit.sources, "hit_after_wait", t0)
            if token:
                return token
            if time.monotonic() >= deadline:
                log.warning("stampede wait timed out; computing without lock")
                return None
            time.sleep(POLL_INTERVAL_S)

    # -- helpers ----------------------------------------------------------------
    def _unsuccessful(self, question, cache_status, t0) -> dict:
        self.stats.inc("not_cached_unsuccessful")  # deliberately never written to Redis
        out = self._result(question, NO_ANSWER_TEXT, [], cache_status, t0)
        out["answered"] = False
        return out

    @staticmethod
    def _result(question, answer, sources, cache_status, t0) -> dict:
        return {"question": question, "answer": answer, "sources": sources,
                "answered": True, "cache_status": cache_status,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3)}
