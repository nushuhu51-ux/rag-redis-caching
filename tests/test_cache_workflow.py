"""Tests for the 10 mandatory scenarios (numbers in docstrings match the assignment)."""
import json
import time
from dataclasses import replace

import pytest

from app.chunking import load_chunks
from app.factory import build_service
from app.generator import ExtractiveGenerator, GenerationError
from app.retriever import TfidfRetriever

Q1 = "Why can an index slow down writes?"
Q2 = "What is a cache stampede and how can it be reduced?"
Q3 = "What does vertical partitioning mean?"


def test_01_first_question_is_cache_miss(env):
    svc, r, g, redis = env
    out = svc.ask(Q1)
    assert out["cache_status"] == "miss"
    assert out["answered"] is True
    assert redis.dbsize() == 1  # stored after the miss


def test_02_repeated_question_is_cache_hit(env):
    svc, *_ = env
    first, second = svc.ask(Q1), svc.ask(Q1)
    assert (first["cache_status"], second["cache_status"]) == ("miss", "hit")
    assert second["answer"] == first["answer"]


def test_03_cached_answer_keeps_correct_sources(env):
    svc, *_ = env
    first, second = svc.ask(Q1), svc.ask(Q1)
    assert first["sources"], "miss must return sources"
    assert second["sources"] == first["sources"]
    assert all({"source", "chunk_id", "heading", "snippet"} <= s.keys() for s in second["sources"])
    # sources are tied to *this* answer: a different question caches different sources
    other = svc.ask(Q3)
    assert other["sources"] != first["sources"]
    assert svc.ask(Q1)["sources"] == first["sources"]


def test_04_cache_hit_does_not_retrieve(env):
    svc, r, g, _ = env
    svc.ask(Q1)
    before = r.calls
    assert svc.ask(Q1)["cache_status"] == "hit"
    assert r.calls == before


def test_05_cache_hit_does_not_generate(env):
    svc, r, g, _ = env
    svc.ask(Q1)
    before = g.calls
    assert svc.ask(Q1)["cache_status"] == "hit"
    assert g.calls == before


def test_06_cache_miss_runs_retrieval_and_generation(env):
    svc, r, g, _ = env
    svc.ask(Q1)
    assert (r.calls, g.calls) == (1, 1)


def test_07a_nothing_relevant_is_not_cached(env):
    svc, r, g, redis = env
    out = svc.ask("zebra quantum kombucha spaceship")
    assert out["answered"] is False and out["sources"] == []
    assert redis.dbsize() == 0
    again = svc.ask("zebra quantum kombucha spaceship")
    assert again["cache_status"] == "miss"  # still not served from cache
    assert svc.stats.snapshot()["not_cached_unsuccessful"] == 2


def test_07b_generation_failure_is_not_cached(settings, base_retriever):
    class Boom:
        name = "boom"
        def generate(self, q, p):
            raise GenerationError("llm down")
    svc = build_service(settings, retriever=base_retriever, generator=Boom())
    from app.redis_client import MiniRedis
    redis = MiniRedis(settings.redis_host, settings.redis_port)
    with pytest.raises(GenerationError):
        svc.ask(Q1)
    assert redis.dbsize() == 0


def test_07c_empty_answer_is_not_cached(settings, base_retriever):
    class Empty:
        name = "empty"
        def generate(self, q, p):
            return "   "
    svc = build_service(settings, retriever=base_retriever, generator=Empty())
    from app.redis_client import MiniRedis
    redis = MiniRedis(settings.redis_host, settings.redis_port)
    with pytest.raises(GenerationError):
        svc.ask(Q1)
    assert redis.dbsize() == 0


def test_08_expiration_follows_configured_ttl(settings, base_retriever):
    from app.redis_client import MiniRedis
    svc = build_service(replace(settings, cache_ttl_seconds=2), retriever=base_retriever)
    redis = MiniRedis(settings.redis_host, settings.redis_port)
    svc.ask(Q1)
    key = svc.cache.make_key(Q1)
    assert 0 < redis.ttl(key) <= 2
    assert svc.ask(Q1)["cache_status"] == "hit"
    time.sleep(2.3)
    assert redis.get(key) is None
    assert svc.ask(Q1)["cache_status"] == "miss"  # recomputed after expiry


def test_08b_ttl_must_be_positive(settings, base_retriever):
    with pytest.raises(ValueError):
        build_service(replace(settings, cache_ttl_seconds=0), retriever=base_retriever)


def test_09a_redis_down_returns_fresh_correct_answer_and_flags_it(settings, base_retriever):
    from tests.conftest import free_port
    dead = replace(settings, redis_port=free_port())  # nothing listens here
    svc = build_service(dead, retriever=base_retriever)
    out = svc.ask(Q1)
    assert out["cache_status"] == "unavailable"  # visible, not silent
    assert out["answered"] and out["sources"]
    assert out["answer"] == build_service(replace(settings, cache_enabled=False),
                                          retriever=base_retriever).ask(Q1)["answer"]
    assert svc.stats.snapshot()["cache_writes"] == 0


def test_09b_redis_killed_midway(settings, base_retriever):
    from tests.conftest import free_port, spawn_redis
    port = free_port()
    proc = spawn_redis(port)
    svc = build_service(replace(settings, redis_port=port), retriever=base_retriever)
    assert svc.ask(Q1)["cache_status"] == "miss"
    assert svc.ask(Q1)["cache_status"] == "hit"
    proc.kill(); proc.wait()
    out = svc.ask(Q1)  # pooled connection is now dead
    assert out["cache_status"] == "unavailable"
    assert out["answered"] and out["sources"]


def test_09c_corrupt_cache_entry_is_not_served(env):
    svc, r, g, redis = env
    key = svc.cache.make_key(Q1)
    redis.set(key, "{not json", ex=60)
    out = svc.ask(Q1)
    assert out["cache_status"] == "miss" and out["answered"]
    # entry with an empty answer is also rejected
    redis.set(key, json.dumps({"q": Q1.casefold(), "answer": "", "sources": []}), ex=60)
    assert svc.ask(Q1)["cache_status"] == "miss"


def test_10_different_questions_do_not_share_entries(env):
    svc, r, g, redis = env
    a, b = svc.ask(Q1), svc.ask(Q2)
    assert a["answer"] != b["answer"]
    assert redis.dbsize() == 2
    assert svc.ask(Q1)["answer"] == a["answer"]
    assert svc.ask(Q2)["answer"] == b["answer"]
    assert svc.cache.make_key(Q1) != svc.cache.make_key(Q2)


# ---- extra: key design requirements from section 5 --------------------------------
def test_key_normalizes_case_and_whitespace(env):
    svc, *_ = env
    svc.ask(Q1)
    assert svc.ask("  WHY can an INDEX   slow down writes? ")["cache_status"] == "hit"


def test_key_changes_with_context(settings, base_retriever):
    a = build_service(settings, retriever=base_retriever)
    b = build_service(replace(settings, top_k=5), retriever=base_retriever)
    assert a.cache.make_key(Q1) != b.cache.make_key(Q1)


def test_key_changes_when_documents_change(settings, tmp_path):
    (tmp_path / "d.md").write_text("# A\nIndexes cost write time.")
    ret1 = TfidfRetriever(load_chunks(tmp_path, 500))
    (tmp_path / "d.md").write_text("# A\nIndexes cost write time. New fact added.")
    ret2 = TfidfRetriever(load_chunks(tmp_path, 500))
    k1 = build_service(settings, retriever=ret1).cache.make_key(Q1)
    k2 = build_service(settings, retriever=ret2).cache.make_key(Q1)
    assert k1 != k2  # stale answers are never served after a corpus change


def test_cache_disabled_never_touches_redis(settings, base_retriever, redis_port):
    from app.redis_client import MiniRedis
    svc = build_service(replace(settings, cache_enabled=False), retriever=base_retriever)
    assert svc.ask(Q1)["cache_status"] == "disabled"
    assert svc.ask(Q1)["cache_status"] == "disabled"
    assert MiniRedis("127.0.0.1", redis_port).dbsize() == 0
    assert svc.stats.snapshot()["retrievals"] == 2


def test_order_redis_is_checked_before_retrieval(settings, base_retriever):
    """Explicit ordering proof: record the sequence of calls."""
    events = []

    class R:
        fingerprint = base_retriever.fingerprint
        def retrieve(self, *a, **k):
            events.append("retrieve"); return base_retriever.retrieve(*a, **k)

    class G(ExtractiveGenerator):
        def generate(self, *a, **k):
            events.append("generate"); return super().generate(*a, **k)

    svc = build_service(settings, retriever=R(), generator=G())
    orig_get, orig_set = svc.cache.get, svc.cache.set
    svc.cache.get = lambda q: (events.append("redis_get"), orig_get(q))[1]
    svc.cache.set = lambda *a: (events.append("redis_set"), orig_set(*a))[1]
    svc.ask(Q1)
    # first event is the Redis check; retrieval/generation only come after it. (A second
    # redis_get is the post-lock re-check made by the stampede protection.)
    assert events[0] == "redis_get"
    assert events[-3:] == ["retrieve", "generate", "redis_set"]
    assert "retrieve" not in events[:-3] and "generate" not in events[:-3]
    events.clear()
    svc.ask(Q1)
    assert events == ["redis_get"]


def test_empty_question_rejected(env):
    svc, *_ = env
    with pytest.raises(ValueError):
        svc.ask("   ")
