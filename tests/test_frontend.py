"""The web UI is served by the same stdlib server as the API (app/static/)."""
import http.client
import json
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from app.api import STATIC_DIR, make_server, resolve_static


@pytest.fixture
def base(env):
    srv = make_server(env[0], "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, dict(r.headers), r.read()
    except HTTPError as e:
        return e.code, dict(e.headers), e.read()


def raw_get(base, path):
    """Send the path exactly as given (urllib would normalise '..')."""
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=3)
    conn.request("GET", path)
    resp = conn.getresponse()
    code, body = resp.status, resp.read()
    conn.close()
    return code, body


def test_index_is_served_with_security_headers(base):
    status, headers, body = get(base + "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"Ask the scaling guide" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("name,ctype", [("styles.css", "text/css"), ("app.js", "text/javascript")])
def test_static_assets_are_served(base, name, ctype):
    status, headers, body = get(f"{base}/static/{name}")
    assert status == 200 and headers["Content-Type"].startswith(ctype) and body


def test_page_has_no_inline_script_or_style(base):
    """The CSP forbids inline code; the page must not rely on it."""
    html = get(base + "/")[2].decode()
    assert "<style" not in html and ' style="' not in html
    assert "<script>" not in html and "onclick=" not in html


@pytest.mark.parametrize("path", [
    "/static/../api.py", "/static/..%2fapi.py", "/static/%2e%2e/config.py",
    "/static/../../.env", "/static/%2e%2e%2f%2e%2e%2f.env", "/static//etc/passwd",
    "/static/", "/static/missing.js", "/static/%00.js", "/static/..\\api.py", "/app/api.py",
])
def test_static_rejects_traversal_and_unknown_files(base, path):
    status, body = raw_get(base, path)
    assert status == 404
    assert b"import " not in body and b"LLM_API_KEY" not in body


def test_only_whitelisted_extensions_are_served(tmp_path, monkeypatch):
    (tmp_path / "secret.txt").write_text("nope")
    (tmp_path / "ok.css").write_text("a{}")
    monkeypatch.setattr("app.api.STATIC_DIR", tmp_path)
    assert resolve_static("/static/secret.txt") is None
    assert resolve_static("/static/ok.css") == (tmp_path / "ok.css").resolve()


def test_symlink_escaping_static_dir_is_not_served(tmp_path, monkeypatch):
    outside = tmp_path / "outside.js"
    outside.write_text("secret()")
    root = tmp_path / "static"
    root.mkdir()
    try:
        (root / "link.js").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted here")
    monkeypatch.setattr("app.api.STATIC_DIR", root)
    assert resolve_static("/static/link.js") is None


def test_api_routes_still_work_next_to_the_ui(base):
    assert json.loads(get(base + "/health")[2]) == {"status": "ok"}
    assert "hit_rate" in json.loads(get(base + "/stats?cache_bust=1")[2])  # query strings are ignored
    assert get(base + "/nope")[0] == 404


def test_ui_files_exist_where_the_server_looks():
    assert {"index.html", "styles.css", "app.js"} <= {p.name for p in STATIC_DIR.iterdir()}
