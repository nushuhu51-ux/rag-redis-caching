"""Dependency-free regression tests for cache/configuration hardening."""
import json

import pytest

from app.cache import AnswerCache
from app.llm_http import OpenAICompatibleGenerator
from app.redis_client import MiniRedis


VALID_SOURCE = {
    "source": "guide.md",
    "chunk_id": "guide.md:0",
    "heading": "Caching",
    "score": 0.75,
    "snippet": "Redis stores answer references together.",
}


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.released = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return "OK"

    def delete(self, key):
        self.values.pop(key, None)

    def compare_and_delete(self, key, token):
        self.released.append((key, token))
        if self.values.get(key) == token:
            del self.values[key]
            return True
        return False

    def ping(self):
        return True


def make_cache(client=None):
    return AnswerCache(client or MemoryRedis(), 60, "test:answer", {"model": "unit"})


def test_cache_accepts_and_roundtrips_valid_source_references():
    cache = make_cache()
    cache.set("Question?", "A grounded answer.", [VALID_SOURCE])
    result = cache.get("question?")
    assert result.answer == "A grounded answer."
    assert result.sources == [VALID_SOURCE]


@pytest.mark.parametrize("sources", [
    [],
    [None],
    [{"source": "x"}],
    [{**VALID_SOURCE, "score": "high"}],
    [{**VALID_SOURCE, "score": float("nan")}],
    [{**VALID_SOURCE, "snippet": " "}],
])
def test_cache_rejects_invalid_source_objects(sources):
    cache = make_cache()
    with pytest.raises(ValueError):
        cache.set("Question?", "A grounded answer.", sources)


def test_cache_discards_malformed_source_objects_on_read():
    client = MemoryRedis()
    cache = make_cache(client)
    key = cache.make_key("Question?")
    client.values[key] = json.dumps({
        "q": "question?", "answer": "answer", "sources": [{"source": "only"}]
    })
    assert cache.get("Question?") is None
    assert key not in client.values


def test_lock_release_uses_atomic_compare_and_delete():
    client = MemoryRedis()
    cache = make_cache(client)
    key = cache._lock_key("Question?")
    client.values[key] = "successor-token"
    cache.release_lock("Question?", "expired-owner-token")
    assert client.values[key] == "successor-token"  # stale owner cannot delete successor lock
    assert client.released == [(key, "expired-owner-token")]


def test_miniredis_compare_and_delete_uses_single_atomic_lua_command(monkeypatch):
    client = MiniRedis("localhost", 6379)
    calls = []
    monkeypatch.setattr(client, "execute", lambda *args: calls.append(args) or 1)
    assert client.compare_and_delete("lock:key", "token") is True
    assert calls[0][0] == "EVAL" and calls[0][2:] == (1, "lock:key", "token")


def test_openai_compatible_cache_identity_includes_endpoint_without_secrets():
    first = OpenAICompatibleGenerator("https://provider-a.example/v1", "same-model", "secret-a")
    second = OpenAICompatibleGenerator("https://provider-b.example/v1", "same-model", "secret-b")
    assert first.name != second.name
    assert "secret-a" not in first.name
    assert "secret-b" not in second.name


def test_redis_auth_and_tls_settings_are_accepted_without_connecting():
    client = MiniRedis(
        "localhost", 6379, username="app", password="secret", tls=True,
        tls_ca_certs="/tmp/ca.pem", tls_check_hostname=True,
    )
    assert client.username == "app" and client.password == "secret" and client.tls is True
    with pytest.raises(ValueError):
        MiniRedis("localhost", 6379, username="app")


def test_readiness_distinguishes_redis_failure_from_process_liveness():
    import http.client
    import threading
    from types import SimpleNamespace

    from app.api import make_server
    from app.cache import CacheUnavailable

    class DownCache:
        def ping(self):
            raise CacheUnavailable("redis unavailable")

    service = SimpleNamespace(cache=DownCache(), stats=SimpleNamespace(snapshot=lambda: {}))
    server = make_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        conn.request("GET", "/health")
        health = conn.getresponse()
        assert health.status == 200
        health.read()
        conn.request("GET", "/ready")
        ready = conn.getresponse()
        assert ready.status == 503
        assert json.loads(ready.read()) == {"status": "not_ready", "redis": "unavailable"}
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)



def test_corpus_fingerprint_changes_when_only_heading_changes():
    from app.chunking import Chunk
    from app.retriever import TfidfRetriever

    original = TfidfRetriever([
        Chunk("guide.md", "guide.md#0", "Original heading", "Identical body text."),
    ])
    renamed = TfidfRetriever([
        Chunk("guide.md", "guide.md#0", "Updated heading", "Identical body text."),
    ])

    assert original.fingerprint != renamed.fingerprint


def test_corpus_fingerprint_changes_when_source_changes():
    from app.chunking import Chunk
    from app.retriever import TfidfRetriever

    original = TfidfRetriever([
        Chunk("first.md", "first.md#0", "Heading", "Identical body text."),
    ])
    renamed = TfidfRetriever([
        Chunk("second.md", "second.md#0", "Heading", "Identical body text."),
    ])

    assert original.fingerprint != renamed.fingerprint


def test_stampede_lock_is_released_if_cache_recheck_fails():
    from app.cache import CacheUnavailable
    from app.rag import RAGService

    class CacheThatFailsOnRecheck:
        lock_wait_seconds = 0.1

        def __init__(self):
            self.released = []

        def try_lock(self, question):
            return "owned-token"

        def get(self, question):
            raise CacheUnavailable("Redis became unavailable after lock acquisition")

        def release_lock(self, question, token):
            self.released.append((question, token))

    cache = CacheThatFailsOnRecheck()
    service = RAGService(
        retriever=None,
        generator=None,
        cache=cache,
        top_k=1,
        min_score=0.0,
    )

    assert service._acquire_or_wait("question", 0.0) is None
    assert cache.released == [("question", "owned-token")]



def test_stampede_lock_cleanup_does_not_mask_initial_lock_failure():
    from app.cache import CacheUnavailable
    from app.rag import RAGService

    class CacheThatFailsToAcquire:
        lock_wait_seconds = 0.1

        def try_lock(self, question):
            raise CacheUnavailable("Redis unavailable during lock acquisition")

        def get(self, question):
            raise AssertionError("cache recheck must not run after lock acquisition fails")

        def release_lock(self, question, token):
            raise AssertionError("no lock token was acquired")

    service = RAGService(None, None, CacheThatFailsToAcquire(), 1, 0.0)
    assert service._acquire_or_wait("question", 0.0) is None



def test_benchmark_redis_cleanup_uses_process_api_and_always_waits():
    from benchmarks.run_benchmark import _stop_redis_process

    class ProcessLike:
        def __init__(self, fail_kill=False):
            self.calls = []
            self.fail_kill = fail_kill

        def kill(self):
            self.calls.append("kill")
            if self.fail_kill:
                raise RuntimeError("container already stopped")

        def wait(self, timeout=None):
            self.calls.append(("wait", timeout))

    process = ProcessLike()
    _stop_redis_process(process)
    assert process.calls == ["kill", ("wait", 5)]

    already_stopped = ProcessLike(fail_kill=True)
    with pytest.raises(RuntimeError, match="already stopped"):
        _stop_redis_process(already_stopped)
    assert already_stopped.calls == ["kill", ("wait", 5)]
