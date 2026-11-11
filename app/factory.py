"""Wiring: build a RAGService from Settings (dependencies can be overridden in tests)."""
from __future__ import annotations

from .cache import AnswerCache
from .chunking import load_chunks
from .config import Settings
from .generator import ExtractiveGenerator
from .rag import RAGService, Stats
from .redis_client import MiniRedis
from .retriever import TfidfRetriever

PROMPT_VERSION = "v1"  # bump when answer-generation logic changes -> old cache entries unused


def build_generator(settings: Settings):
    if settings.llm_provider == "extractive":
        return ExtractiveGenerator(settings.llm_simulated_latency_ms)
    if settings.llm_provider == "openai_compatible":
        from .llm_http import OpenAICompatibleGenerator
        return OpenAICompatibleGenerator(settings.llm_base_url, settings.llm_model,
                                         settings.llm_api_key, settings.llm_timeout)
    raise ValueError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")


def build_service(settings: Settings, retriever=None, generator=None, stats: Stats | None = None) -> RAGService:
    retriever = retriever or TfidfRetriever(load_chunks(settings.docs_dir, settings.chunk_max_chars))
    generator = generator or build_generator(settings)
    cache = None
    if settings.cache_enabled:
        context = {"corpus": retriever.fingerprint, "top_k": settings.top_k,
                   "min_score": settings.min_score, "llm": generator.name, "prompt": PROMPT_VERSION}
        client = MiniRedis(
            settings.redis_host, settings.redis_port, settings.redis_db, settings.redis_timeout,
            username=settings.redis_username, password=settings.redis_password,
            tls=settings.redis_tls, tls_ca_certs=settings.redis_tls_ca_certs,
            tls_certfile=settings.redis_tls_certfile, tls_keyfile=settings.redis_tls_keyfile,
            tls_check_hostname=settings.redis_tls_check_hostname,
        )
        cache = AnswerCache(client, settings.cache_ttl_seconds, settings.cache_prefix, context,
                            settings.stampede_lock_ttl_seconds, settings.stampede_wait_seconds)
    return RAGService(retriever, generator, cache, settings.top_k, settings.min_score, stats)
