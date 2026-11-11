"""Configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Redis
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_timeout: float = 0.5  # seconds; a slow/down Redis must not hang requests
    redis_username: str = ""
    redis_password: str = field(default="", repr=False)
    redis_tls: bool = False
    redis_tls_ca_certs: str = ""
    redis_tls_certfile: str = ""
    redis_tls_keyfile: str = field(default="", repr=False)
    redis_tls_check_hostname: bool = True
    # Cache
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600  # configurable expiration
    cache_prefix: str = "rag:answer"
    # RAG
    docs_dir: Path = ROOT / "data" / "docs"
    chunk_max_chars: int = 500
    top_k: int = 3
    min_score: float = 0.05
    llm_name: str = "extractive-v1"
    # LLM provider: "extractive" (local, default) or "openai_compatible" (any /chat/completions API,
    # e.g. OpenAI, Ollama, vLLM, LiteLLM). The API key is never logged and never part of cache keys.
    llm_provider: str = "extractive"
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    llm_timeout: float = 30.0
    # Stampede protection (single-flight lock in Redis)
    stampede_lock_ttl_seconds: int = 30
    stampede_wait_seconds: float = 5.0
    # Benchmark-only knob: adds an explicit, clearly-labelled sleep to the generator
    # to emulate a remote LLM call. 0 (default) means no artificial delay.
    llm_simulated_latency_ms: int = 0
    # HTTP
    http_host: str = "127.0.0.1"
    http_port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        d = cls()
        return cls(
            redis_host=e.get("REDIS_HOST", d.redis_host),
            redis_port=int(e.get("REDIS_PORT", d.redis_port)),
            redis_db=int(e.get("REDIS_DB", d.redis_db)),
            redis_timeout=float(e.get("REDIS_TIMEOUT", d.redis_timeout)),
            redis_username=e.get("REDIS_USERNAME", d.redis_username),
            redis_password=e.get("REDIS_PASSWORD", d.redis_password),
            redis_tls=_bool(e.get("REDIS_TLS"), d.redis_tls),
            redis_tls_ca_certs=e.get("REDIS_TLS_CA_CERTS", d.redis_tls_ca_certs),
            redis_tls_certfile=e.get("REDIS_TLS_CERTFILE", d.redis_tls_certfile),
            redis_tls_keyfile=e.get("REDIS_TLS_KEYFILE", d.redis_tls_keyfile),
            redis_tls_check_hostname=_bool(e.get("REDIS_TLS_CHECK_HOSTNAME"), d.redis_tls_check_hostname),
            cache_enabled=_bool(e.get("CACHE_ENABLED"), d.cache_enabled),
            cache_ttl_seconds=int(e.get("CACHE_TTL_SECONDS", d.cache_ttl_seconds)),
            cache_prefix=e.get("CACHE_PREFIX", d.cache_prefix),
            docs_dir=Path(e.get("DOCS_DIR", str(d.docs_dir))),
            chunk_max_chars=int(e.get("CHUNK_MAX_CHARS", d.chunk_max_chars)),
            top_k=int(e.get("TOP_K", d.top_k)),
            min_score=float(e.get("MIN_SCORE", d.min_score)),
            llm_name=e.get("LLM_NAME", d.llm_name),
            llm_provider=e.get("LLM_PROVIDER", d.llm_provider),
            llm_base_url=e.get("LLM_BASE_URL", d.llm_base_url),
            llm_api_key=e.get("LLM_API_KEY", d.llm_api_key),
            llm_model=e.get("LLM_MODEL", d.llm_model),
            llm_timeout=float(e.get("LLM_TIMEOUT", d.llm_timeout)),
            stampede_lock_ttl_seconds=int(e.get("STAMPEDE_LOCK_TTL_SECONDS", d.stampede_lock_ttl_seconds)),
            stampede_wait_seconds=float(e.get("STAMPEDE_WAIT_SECONDS", d.stampede_wait_seconds)),
            llm_simulated_latency_ms=int(e.get("LLM_SIMULATED_LATENCY_MS", d.llm_simulated_latency_ms)),
            http_host=e.get("HTTP_HOST", d.http_host),
            http_port=int(e.get("HTTP_PORT", d.http_port)),
        )
