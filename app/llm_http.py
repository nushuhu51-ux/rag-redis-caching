"""Real-LLM generator for any OpenAI-compatible /chat/completions endpoint
(OpenAI, Ollama, vLLM, LiteLLM, ...). Drop-in for ExtractiveGenerator.

Verification status: exercised in tests against a LOCAL STUB HTTP server that speaks the
same protocol. It has NOT been run against a live hosted model (no network/key in the
build environment).
"""
from __future__ import annotations

import hashlib
import requests
from urllib.parse import urlsplit, urlunsplit

from .generator import GenerationError, NoAnswerError
from .retriever import Passage

NO_ANSWER_TOKEN = "NO_ANSWER"
SYSTEM_PROMPT = (
    "You answer questions using ONLY the numbered context passages provided. "
    "Be concise and do not use outside knowledge. If the passages do not contain the answer, "
    f"reply with exactly {NO_ANSWER_TOKEN}."
)


class OpenAICompatibleGenerator:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 30.0,
                 session: requests.Session | None = None):
        if not base_url or not model:
            raise ValueError("LLM_BASE_URL and LLM_MODEL are required for the openai_compatible provider")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.timeout = model, timeout
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._session = session or requests.Session()
        # Cache identity must separate providers/endpoints using the same model name.
        # Hash a credential-free canonical endpoint; never expose URL userinfo/query secrets.
        parts = urlsplit(self.url)
        hostname = (parts.hostname or "").lower()
        port = parts.port
        netloc = hostname if port is None else f"{hostname}:{port}"
        canonical_endpoint = urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))
        endpoint_id = hashlib.sha256(canonical_endpoint.encode("utf-8")).hexdigest()[:16]
        self.name = f"http:{model}:endpoint:{endpoint_id}"

    def generate(self, question: str, passages: list[Passage]) -> str:
        context = "\n\n".join(f"[{i}] ({p.heading}) {p.text}" for i, p in enumerate(passages, 1))
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}]}
        try:
            resp = self._session.post(self.url, json=body, headers=self._headers, timeout=self.timeout)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            # Message deliberately excludes headers/body so the API key can never leak into logs.
            raise GenerationError(f"LLM request failed: {type(exc).__name__}") from exc
        if not isinstance(text, str) or not text.strip():
            raise GenerationError("LLM returned an empty answer")
        if text.strip() == NO_ANSWER_TOKEN:
            raise NoAnswerError("LLM reported the context does not contain the answer")
        return text.strip()
