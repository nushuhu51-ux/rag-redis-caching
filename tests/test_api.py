"""End-to-end over real HTTP + real Redis."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from app.api import make_server


@pytest.fixture
def server(env):
    svc = env[0]
    srv = make_server(svc, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", env
    srv.shutdown()


def post(url, payload):
    req = urllib.request.Request(url + "/ask", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_miss_then_hit_with_sources(server):
    url, (svc, r, g, _) = server
    s1, b1 = post(url, {"question": "What is an index?"})
    s2, b2 = post(url, {"question": "What is an index?"})
    assert (s1, s2) == (200, 200)
    assert (b1["cache_status"], b2["cache_status"]) == ("miss", "hit")
    assert b1["answer"] == b2["answer"] and b1["sources"] == b2["sources"] and b2["sources"]
    assert (r.calls, g.calls) == (1, 1)
    with urllib.request.urlopen(url + "/stats") as resp:
        stats = json.loads(resp.read())
    assert stats["cache_hits"] == 1 and stats["cache_misses"] == 1 and stats["hit_rate"] == 0.5


def raw_post(url, headers, body=b""):
    import http.client
    host, port = url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=3)
    conn.putrequest("POST", "/ask", skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders(body)
    resp = conn.getresponse()
    code = resp.status
    conn.close()
    return code


def test_http_negative_content_length_is_rejected_not_hung(server):
    url, _ = server
    assert raw_post(url, {"Content-Length": "-1"}) == 400  # timeout=3 would raise if the handler hung


def test_http_non_numeric_content_length(server):
    url, _ = server
    assert raw_post(url, {"Content-Length": "abc"}) == 400


def test_http_oversized_body_rejected_before_reading(server):
    url, (svc, r, g, _) = server
    assert raw_post(url, {"Content-Length": str(5_000_000)}) == 413  # sends no body at all
    assert (r.calls, g.calls) == (0, 0)


def test_http_malformed_bodies(server):
    url, _ = server
    for body in (b"not json", b"[1, 2]", b'{"question": 5}', b'{"question": null}'):
        assert raw_post(url, {"Content-Length": str(len(body))}, body) == 400


def test_http_unicode_question(server):
    url, _ = server
    s, b = post(url, {"question": "¿Qué es un índice? 索引"})
    assert s == 200 and b["cache_status"] in ("miss",)


def test_http_validation(server):
    url, _ = server
    assert post(url, {"question": ""})[0] == 400
    assert post(url, {})[0] == 400
    assert post(url, {"question": "x" * 1001})[0] == 400
