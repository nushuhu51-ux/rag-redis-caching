
import socket
import subprocess
import shutil
import time
import uuid
from dataclasses import replace

import pytest

from app.config import Settings
from app.factory import build_service
from app.generator import ExtractiveGenerator
from app.redis_client import MiniRedis
from app.chunking import load_chunks
from app.retriever import TfidfRetriever


def free_port() -> int:
    """Return an available localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DockerRedisProcess:
    """Process-like wrapper for an independently managed Redis container."""

    def __init__(self, container_id: str):
        self.container_id = container_id

    def kill(self):
        subprocess.run(
            ["docker", "kill", self.container_id],
            check=True,
            capture_output=True,
            text=True,
        )

    def wait(self, timeout=None):
        result = subprocess.run(
            ["docker", "wait", self.container_id],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        subprocess.run(
            ["docker", "rm", self.container_id],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(result.stdout.strip())


def spawn_redis(port: int) -> DockerRedisProcess:
    """Start an isolated Redis Docker container on the requested host port."""
    container_name = f"rag-test-redis-{uuid.uuid4().hex[:12]}"

    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"127.0.0.1:{port}:6379",
            "redis:7",
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    proc = DockerRedisProcess(result.stdout.strip())
    client = MiniRedis("127.0.0.1", port, timeout=0.2)

    for _ in range(50):
        try:
            if client.ping():
                return proc
        except Exception:
            time.sleep(0.1)

    subprocess.run(
        ["docker", "rm", "-f", proc.container_id],
        capture_output=True,
        text=True,
    )
    raise RuntimeError("Docker Redis container did not become ready")


class CountingRetriever:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0
        self.fingerprint = inner.fingerprint

    def retrieve(self, *a, **kw):
        self.calls += 1
        return self.inner.retrieve(*a, **kw)


class CountingGenerator:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0
        self.name = inner.name

    def generate(self, *a, **kw):
        self.calls += 1
        return self.inner.generate(*a, **kw)


@pytest.fixture(scope="session")
def redis_port():
    """Use an existing local Redis, or start an isolated redis-server for the test session."""
    # Honour an explicitly running development/test Redis first.
    existing = MiniRedis("127.0.0.1", 6379, timeout=0.2)
    try:
        if existing.ping():
            yield 6379
            return
    except Exception:
        pass
    finally:
        existing.close()

    binary = shutil.which("redis-server")
    if not binary:
        raise RuntimeError(
            "Redis integration tests require redis-server on PATH or an existing Redis at "
            "127.0.0.1:6379. Install Redis or start it with: docker compose up -d redis"
        )

    port = free_port()
    process = subprocess.Popen(
        [binary, "--bind", "127.0.0.1", "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = MiniRedis("127.0.0.1", port, timeout=0.2)
    try:
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("redis-server exited before becoming ready")
            try:
                if client.ping():
                    yield port
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("redis-server did not become ready within five seconds")
    finally:
        client.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture(scope="session")
def base_retriever():
    s = Settings()
    return TfidfRetriever(load_chunks(s.docs_dir, s.chunk_max_chars))


@pytest.fixture
def settings(redis_port):
    return replace(
        Settings(),
        redis_port=redis_port,
        cache_ttl_seconds=60,
        cache_prefix="test:rag",
    )


@pytest.fixture(autouse=True)
def clean_redis(request):
    # Only integration tests that request redis_port touch Redis. Unit tests using fakes
    # remain runnable without Redis and never flush a developer's unrelated Redis DB.
    if "redis_port" not in request.fixturenames:
        return
    port = request.getfixturevalue("redis_port")
    client = MiniRedis("127.0.0.1", port, timeout=0.2)
    try:
        client.flushdb()
    finally:
        client.close()


@pytest.fixture
def env(settings, base_retriever):
    """(service, counting retriever, counting generator, raw redis client)"""
    r = CountingRetriever(base_retriever)
    g = CountingGenerator(ExtractiveGenerator())
    svc = build_service(settings, retriever=r, generator=g)
    return svc, r, g, MiniRedis(settings.redis_host, settings.redis_port)
