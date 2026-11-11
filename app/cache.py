"""Redis answer cache: key generation, storage of answer + sources, error mapping."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass

from .redis_client import RedisError

log = logging.getLogger(__name__)


class CacheUnavailable(Exception):
    """Redis could not be reached / returned an error. Caller must NOT treat this as a hit."""


@dataclass(frozen=True)
class CachedAnswer:
    answer: str
    sources: list[dict]


def normalize_question(question: str) -> str:
    """Case-fold and collapse whitespace so trivially different spellings share a key."""
    return " ".join(question.casefold().split())


class AnswerCache:
    """
    Cache key = prefix : sha256({normalized question, context}).

    `context` carries everything that changes what a correct answer would be:
    corpus fingerprint (documents changed -> old answers must not be served),
    top_k / min_score (retrieval config), LLM name and prompt version.

    There is deliberately NO user id in the key: answers are generated from a shared,
    non-personal document collection, so sharing them across users is correct and
    maximises hit rate. If per-user documents/ACLs were ever added, the user/tenant
    id MUST be added to `context` (see README, "Security / sharing").
    """

    def __init__(self, client, ttl_seconds: int, prefix: str, context: dict,
                 lock_ttl_seconds: int = 30, lock_wait_seconds: float = 5.0):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0 (every cached answer must expire)")
        if lock_ttl_seconds <= 0:
            raise ValueError("lock_ttl_seconds must be > 0 (a crashed holder must not block forever)")
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.prefix = prefix
        self.context = dict(context)
        self.lock_ttl_seconds = lock_ttl_seconds
        self.lock_wait_seconds = lock_wait_seconds

    # -- stampede protection: single-flight lock per cache key ------------------------
    def _lock_key(self, question: str) -> str:
        return self.make_key(question) + ":lock"

    def try_lock(self, question: str) -> str | None:
        """Try to become the one request that rebuilds this entry. Returns a token or None."""
        token = uuid.uuid4().hex
        try:
            ok = self.client.set(self._lock_key(question), token, ex=self.lock_ttl_seconds, nx=True)
        except RedisError as exc:
            raise CacheUnavailable(str(exc)) from exc
        return token if ok == "OK" else None

    def release_lock(self, question: str, token: str) -> None:
        """Atomically release only our own lock; never delete a successor's lock."""
        key = self._lock_key(question)
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
        try:
            if hasattr(self.client, "compare_and_delete"):
                self.client.compare_and_delete(key, token)
            elif hasattr(self.client, "eval"):
                # redis-py-compatible client
                self.client.eval(script, 1, key, token)
            else:
                # Minimal clients exposing only a generic Redis command executor
                self.client.execute("EVAL", script, 1, key, token)
        except Exception:
            # The lock has a TTL; cleanup failure must not replace the request result.
            log.debug("could not release Redis stampede lock", exc_info=True)

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception as exc:
            raise CacheUnavailable(str(exc)) from exc

    @staticmethod
    def _valid_sources(sources: object) -> bool:
        if not isinstance(sources, list) or not sources:
            return False
        required = {"source", "chunk_id", "heading", "score", "snippet"}
        for source in sources:
            if not isinstance(source, dict) or not required.issubset(source):
                return False
            if not all(isinstance(source[k], str) and source[k].strip()
                       for k in ("source", "chunk_id", "heading", "snippet")):
                return False
            if (isinstance(source["score"], bool)
                    or not isinstance(source["score"], (int, float))
                    or not math.isfinite(source["score"])):
                return False
        return True

    def make_key(self, question: str) -> str:
        payload = json.dumps({"q": normalize_question(question), "ctx": self.context},
                             sort_keys=True, separators=(",", ":"))
        return f"{self.prefix}:{hashlib.sha256(payload.encode()).hexdigest()}"

    def get(self, question: str) -> CachedAnswer | None:
        key = self.make_key(question)
        try:
            raw = self.client.get(key)
        except RedisError as exc:
            raise CacheUnavailable(str(exc)) from exc
        if raw is None:
            return None
        try:
            doc = json.loads(raw)
            if doc["q"] != normalize_question(question):  # collision / tamper guard
                raise ValueError("question mismatch")
            answer, sources = doc["answer"], doc["sources"]
            if not isinstance(answer, str) or not answer.strip() or not self._valid_sources(sources):
                raise ValueError("malformed entry")
        except (ValueError, KeyError, TypeError) as exc:
            # A corrupt entry is treated as a miss and removed, never returned.
            log.warning("discarding corrupt cache entry %s: %s", key, exc)
            try:
                self.client.delete(key)
            except RedisError:
                pass
            return None
        return CachedAnswer(answer=answer, sources=sources)

    def set(self, question: str, answer: str, sources: list[dict]) -> None:
        """Store answer + its sources atomically in ONE value (they can never diverge)."""
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("only non-empty successful answers may be cached")
        if not self._valid_sources(sources):
            raise ValueError("sources must contain valid source reference objects")
        doc = json.dumps({"q": normalize_question(question), "answer": answer,
                          "sources": sources, "cached_at": time.time()}, ensure_ascii=False)
        try:
            self.client.set(self.make_key(question), doc, ex=self.ttl_seconds)
        except RedisError as exc:
            raise CacheUnavailable(str(exc)) from exc
