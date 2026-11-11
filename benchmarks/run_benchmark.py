"""Reproducible benchmark: Scenario A (cache disabled) vs Scenario B (Redis answer cache).

Usage:  python benchmarks/run_benchmark.py
Starts its own throw-away redis-server on a free port. Workload is fixed (seeded):
10 distinct questions x 10 repeats = 100 requests, shuffled. Run twice:
  * "native"    : generator exactly as shipped (no artificial delay)
  * "sim-llm"   : generator sleeps 300 ms per call to EMULATE a remote LLM (clearly labelled;
                  this is an assumption, not a measurement of a real LLM)
"""
import json
import platform
import random
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.factory import build_service  # noqa: E402
from app.generator import ExtractiveGenerator  # noqa: E402
from app.redis_client import MiniRedis  # noqa: E402
from app.chunking import load_chunks  # noqa: E402
from app.retriever import TfidfRetriever  # noqa: E402
from tests.conftest import free_port, spawn_redis, CountingGenerator, CountingRetriever  # noqa: E402

QUESTIONS = [
    "Why can an index slow down writes?",
    "What are read replicas and what is their limitation?",
    "What is vertical partitioning?",
    "How does a write queue handle bursts?",
    "What is load shedding?",
    "What is denormalization?",
    "What does sharding do and what is a hot key?",
    "What is the trade-off of batching?",
    "When can an application cache help with reads?",
    "What is a CDN used for?",
]
REPEATS = 10
SEED = 42


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def run(cache_enabled: bool, sim_ms: int, port: int, base_ret) -> dict:
    s = replace(Settings(), redis_port=port, cache_enabled=cache_enabled, cache_ttl_seconds=600,
                cache_prefix="bench", llm_simulated_latency_ms=sim_ms)
    MiniRedis("127.0.0.1", port).flushdb()
    ret, gen = CountingRetriever(base_ret), CountingGenerator(ExtractiveGenerator(sim_ms))
    svc = build_service(s, retriever=ret, generator=gen)
    workload = QUESTIONS * REPEATS
    random.Random(SEED).shuffle(workload)
    for q in QUESTIONS[:2]:  # warm-up (imports, sockets); excluded from stats
        build_service(replace(s, cache_enabled=False), retriever=base_ret,
                      generator=ExtractiveGenerator(0)).ask(q)
    lat, hit_lat, miss_lat = [], [], []
    for q in workload:
        t = time.perf_counter()
        out = svc.ask(q)
        ms = (time.perf_counter() - t) * 1000
        lat.append(ms)
        (hit_lat if out["cache_status"] == "hit" else miss_lat).append(ms)
    st = svc.stats.snapshot()
    return {
        "requests": len(workload),
        "avg_latency_ms": round(statistics.mean(lat), 3),
        "p50_ms": round(pct(lat, 50), 3), "p95_ms": round(pct(lat, 95), 3),
        "avg_hit_ms": round(statistics.mean(hit_lat), 3) if hit_lat else None,
        "avg_miss_or_uncached_ms": round(statistics.mean(miss_lat), 3) if miss_lat else None,
        "cache_hits": st["cache_hits"], "cache_misses": st["cache_misses"],
        "hit_rate": st["hit_rate"], "miss_rate": st["miss_rate"],
        "retrieval_ops": ret.calls, "generation_ops": gen.calls,
        "total_time_s": round(sum(lat) / 1000, 3),
    }


def _stop_redis_process(proc) -> None:
    """Stop and reap the DockerRedisProcess returned by spawn_redis()."""
    try:
        proc.kill()
    finally:
        # Always wait/remove the container even if Docker reports that it
        # was already stopped before cleanup began.
        proc.wait(timeout=5)


def main():
    from app.config import Settings as S
    s0 = S()
    base_ret = TfidfRetriever(load_chunks(s0.docs_dir, s0.chunk_max_chars))
    port = free_port()
    proc = spawn_redis(port)
    results = {"environment": {
        "python": platform.python_version(), "platform": platform.platform(),
        "redis": "redis-server 7.0.15, local, same host, loopback TCP, no persistence",
        "corpus_chunks": len(base_ret.chunks), "distinct_questions": len(QUESTIONS),
        "repeats_per_question": REPEATS, "seed": SEED},
        "runs": {}}
    try:
        for label, ms in (("native", 0), ("sim-llm-300ms", 300)):
            a = run(False, ms, port, base_ret)
            b = run(True, ms, port, base_ret)
            results["runs"][label] = {
                "A_no_cache": a, "B_with_cache": b,
                "avg_latency_speedup_x": round(a["avg_latency_ms"] / b["avg_latency_ms"], 2),
                "avg_latency_reduction_pct": round(100 * (1 - b["avg_latency_ms"] / a["avg_latency_ms"]), 1),
                "generation_ops_saved": a["generation_ops"] - b["generation_ops"],
                "retrieval_ops_saved": a["retrieval_ops"] - b["retrieval_ops"],
            }
    finally:
        # spawn_redis() returns DockerRedisProcess, whose supported lifecycle
        # API is kill() followed by wait() (not subprocess.Popen.terminate()).
        _stop_redis_process(proc)
    out = Path(__file__).with_name("results.json")
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
