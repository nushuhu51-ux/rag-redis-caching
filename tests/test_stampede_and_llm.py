"""Stampede protection + real-LLM (OpenAI-compatible) generator, tested with a local stub server."""
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.factory import build_service
from app.generator import ExtractiveGenerator, GenerationError
from app.llm_http import NO_ANSWER_TOKEN, OpenAICompatibleGenerator
from app.redis_client import MiniRedis
from tests.conftest import CountingGenerator, CountingRetriever

Q = "What is load shedding?"


# ---------------------------------------------------------------- stampede ----------
class SlowGenerator(CountingGenerator):
    def generate(self, *a, **kw):
        time.sleep(0.4)  # long enough for all threads to pile up on the same miss
        return super().generate(*a, **kw)


def run_threads(svc, question, n):
    out, barrier = [None] * n, threading.Barrier(n)

    def work(i):
        barrier.wait()
        out[i] = svc.ask(question)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    return out


def test_stampede_only_one_rebuild_for_concurrent_identical_misses(settings, base_retriever):
    r, g = CountingRetriever(base_retriever), SlowGenerator(ExtractiveGenerator())
    svc = build_service(settings, retriever=r, generator=g)
    res = run_threads(svc, Q, 8)
    assert all(x is not None for x in res)
    assert g.calls == 1 and r.calls == 1  # without the lock this would be 8
    assert len({x["answer"] for x in res}) == 1
    assert len({json.dumps(x["sources"]) for x in res}) == 1
    statuses = sorted(x["cache_status"] for x in res)
    assert statuses.count("miss") == 1 and statuses.count("hit_after_wait") + statuses.count("hit") == 7
    # no lock keys left behind
    assert MiniRedis(settings.redis_host, settings.redis_port).dbsize() == 1


def test_stampede_different_questions_do_not_block_each_other(settings, base_retriever):
    g = SlowGenerator(ExtractiveGenerator())
    svc = build_service(settings, retriever=base_retriever, generator=g)
    qs = ["What is load shedding?", "What is denormalization?", "What is a CDN used for?"]
    out, ts = {}, []
    for q in qs:
        ts.append(threading.Thread(target=lambda q=q: out.__setitem__(q, svc.ask(q))))
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert g.calls == 3  # each distinct key rebuilt once, in parallel
    assert time.perf_counter() - t0 < 1.1  # ~0.4 s, not 3 x 0.4 s serialised


def test_lock_released_after_failed_generation_and_waiter_recovers(settings, base_retriever):
    class FailsOnce:
        name = "flaky"
        def __init__(self):
            self.calls = 0
        def generate(self, q, p):
            self.calls += 1
            time.sleep(0.3)
            if self.calls == 1:
                raise GenerationError("boom")
            return "recovered answer"
    g = FailsOnce()
    svc = build_service(settings, retriever=base_retriever, generator=g)
    results = []

    def work():
        try:
            results.append(svc.ask(Q))
        except GenerationError:
            results.append("error")

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert sorted(map(str, [r if isinstance(r, str) else "ok" for r in results])) == ["error", "ok"]
    assert MiniRedis(settings.redis_host, settings.redis_port).dbsize() == 1  # only the good answer, no stale lock


def test_lock_expires_so_crashed_holder_cannot_block_forever(settings, base_retriever):
    svc = build_service(replace(settings, stampede_lock_ttl_seconds=1, stampede_wait_seconds=3),
                        retriever=base_retriever)
    assert svc.cache.try_lock(Q)  # simulate a holder that crashed and never released
    t0 = time.perf_counter()
    out = svc.ask(Q)  # must wait for the 1 s lock TTL, then rebuild itself
    assert out["answered"] and out["cache_status"] == "miss"
    assert 0.9 < time.perf_counter() - t0 < 3.0


def test_wait_timeout_falls_back_to_computing(settings, base_retriever):
    svc = build_service(replace(settings, stampede_lock_ttl_seconds=30, stampede_wait_seconds=0.3),
                        retriever=base_retriever)
    svc.cache.try_lock(Q)  # held "forever" relative to the wait budget
    out = svc.ask(Q)
    assert out["answered"] is True  # served a correct fresh answer after the wait budget


# ---------------------------------------------------------------- real-LLM client ------
class StubLLM:
    def __init__(self):
        self.mode, self.requests = "ok", []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if stub.mode == "500":
                    self.send_response(500); self.end_headers(); return
                text = {"ok": "Load shedding rejects selected work when requirements permit.",
                        "no_answer": NO_ANSWER_TOKEN, "empty": "  ", "garbage": None}[stub.mode]
                payload = b"not json" if stub.mode == "garbage" else json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"


@pytest.fixture
def stub():
    s = StubLLM()
    yield s
    s.srv.shutdown()


def llm_service(settings, retriever, stub):
    gen = OpenAICompatibleGenerator(stub.url, "test-model", api_key="sk-SECRET", timeout=2)
    return build_service(settings, retriever=retriever, generator=gen), gen


def test_llm_success_is_cached_and_hit_skips_the_llm(settings, base_retriever, stub):
    svc, gen = llm_service(settings, base_retriever, stub)
    a = svc.ask(Q)
    assert a["cache_status"] == "miss" and a["answer"].startswith("Load shedding")
    n = len(stub.requests)
    b = svc.ask(Q)
    assert b["cache_status"] == "hit" and b["answer"] == a["answer"] and b["sources"] == a["sources"]
    assert len(stub.requests) == n == 1  # cache hit made NO LLM HTTP call
    req = stub.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer sk-SECRET"
    assert "Question: " + Q in req["body"]["messages"][1]["content"]
    assert "[1]" in req["body"]["messages"][1]["content"]  # retrieved context was sent


def test_llm_api_key_not_in_cache_key_or_value(settings, base_retriever, stub):
    svc, gen = llm_service(settings, base_retriever, stub)
    svc.ask(Q)
    redis = MiniRedis(settings.redis_host, settings.redis_port)
    assert "sk-SECRET" not in svc.cache.make_key(Q)
    assert "sk-SECRET" not in redis.get(svc.cache.make_key(Q))
    assert "sk-SECRET" not in repr(replace(settings, llm_api_key="sk-SECRET"))


@pytest.mark.parametrize("mode", ["500", "empty", "garbage"])
def test_llm_failures_raise_and_are_not_cached(settings, base_retriever, stub, mode):
    stub.mode = mode
    svc, _ = llm_service(settings, base_retriever, stub)
    with pytest.raises(GenerationError) as ei:
        svc.ask(Q)
    assert "sk-SECRET" not in str(ei.value)
    assert MiniRedis(settings.redis_host, settings.redis_port).dbsize() == 0


def test_llm_no_answer_token_is_unanswered_and_not_cached(settings, base_retriever, stub):
    stub.mode = "no_answer"
    svc, _ = llm_service(settings, base_retriever, stub)
    out = svc.ask(Q)
    assert out["answered"] is False
    assert MiniRedis(settings.redis_host, settings.redis_port).dbsize() == 0


def test_llm_unreachable_is_generation_error_not_cached(settings, base_retriever):
    from tests.conftest import free_port
    gen = OpenAICompatibleGenerator(f"http://127.0.0.1:{free_port()}/v1", "m", timeout=1)
    svc = build_service(settings, retriever=base_retriever, generator=gen)
    with pytest.raises(GenerationError):
        svc.ask(Q)
    assert MiniRedis(settings.redis_host, settings.redis_port).dbsize() == 0


def test_cache_key_differs_between_llm_providers(settings, base_retriever, stub):
    a = build_service(settings, retriever=base_retriever)  # extractive
    b, _ = llm_service(settings, base_retriever, stub)
    assert a.cache.make_key(Q) != b.cache.make_key(Q)  # answers from different models never mix


def test_provider_config_validation(settings):
    with pytest.raises(ValueError):
        build_service(replace(settings, llm_provider="openai_compatible"))  # missing url/model
    with pytest.raises(ValueError):
        build_service(replace(settings, llm_provider="nope"))
