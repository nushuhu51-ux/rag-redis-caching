"""HTTP API (stdlib). POST /ask {"question": "..."}, GET /health, GET /stats.
Also serves the web UI: GET / (app/static/index.html) and GET /static/<file>.

Deviation note: FastAPI could not be installed in the build environment, so the
standard-library ThreadingHTTPServer is used. Handlers are thin; all logic is in RAGService.
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cache import CacheUnavailable
from .generator import GenerationError

log = logging.getLogger(__name__)
MAX_QUESTION_CHARS = 1000
MAX_BODY_BYTES = 16 * 1024  # a 1000-char question (even all non-ASCII) fits comfortably

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {  # only these extensions are ever served
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
# The UI is same-origin and uses no inline script/style, so a strict policy costs nothing.
UI_CSP = ("default-src 'self'; img-src 'self' data:; base-uri 'none'; "
          "form-action 'self'; frame-ancestors 'none'")


def resolve_static(url_path: str) -> Path | None:
    """Map a URL path to a file inside STATIC_DIR, or None. Blocks traversal (../, encoded
    separators, absolute paths) by resolving the final path and requiring it to stay inside."""
    if url_path in ("/", "/index.html"):
        name = "index.html"
    elif url_path.startswith("/static/"):
        name = unquote(url_path[len("/static/"):])
    else:
        return None
    try:
        target = (STATIC_DIR / name).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()):
            return None
        if target.suffix.lower() not in STATIC_TYPES or not target.is_file():
            return None
    except (ValueError, OSError):  # e.g. NUL byte in the path
        return None
    return target


def make_server(service, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_static(self, target: Path) -> None:
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES[target.suffix.lower()])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")  # always revalidate; files are tiny
            self.send_header("X-Content-Type-Options", "nosniff")
            if target.suffix.lower() == ".html":
                self.send_header("Content-Security-Policy", UI_CSP)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                # Liveness only: the HTTP process is running.
                self._send(200, {"status": "ok"})
            elif path == "/ready":
                # Readiness includes Redis when answer caching is enabled.
                cache = service.cache
                if cache is None:
                    self._send(200, {"status": "ready", "redis": "disabled"})
                else:
                    try:
                        cache.ping()
                    except CacheUnavailable:
                        self._send(503, {"status": "not_ready", "redis": "unavailable"})
                    else:
                        self._send(200, {"status": "ready", "redis": "ok"})
            elif path == "/stats":
                self._send(200, service.stats.snapshot())
            elif (target := resolve_static(path)) is not None:
                self._send_static(target)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/ask":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return self._send(400, {"error": "invalid Content-Length"})
            # Validate the size BEFORE reading: a negative value would make read() block until EOF
            # (hung worker thread) and a huge one would be buffered in memory.
            if n < 0:
                return self._send(400, {"error": "invalid Content-Length"})
            if n > MAX_BODY_BYTES:
                self.close_connection = True  # body is not consumed; do not reuse the connection
                return self._send(413, {"error": f"request body exceeds {MAX_BODY_BYTES} bytes"})
            try:
                question = json.loads(self.rfile.read(n) or b"{}").get("question")
                if not isinstance(question, str) or not question.strip():
                    raise ValueError("'question' must be a non-empty string")
                if len(question) > MAX_QUESTION_CHARS:
                    raise ValueError(f"'question' exceeds {MAX_QUESTION_CHARS} characters")
            except (ValueError, AttributeError) as exc:
                return self._send(400, {"error": str(exc)})
            try:
                self._send(200, service.ask(question))
            except GenerationError as exc:
                log.error("generation failed: %s", exc)
                self._send(502, {"error": "answer generation failed"})
            except Exception:  # noqa: BLE001
                log.exception("unexpected error")
                self._send(500, {"error": "internal error"})

        def log_message(self, fmt, *args):  # quiet
            log.debug(fmt, *args)

    return ThreadingHTTPServer((host, port), Handler)
