"""Minimal Redis client (RESP2 over a TCP socket).

Why this exists: the `redis` PyPI package could not be installed in the build
environment (package installs disabled). The protocol subset needed here is tiny
(GET/SET EX/DEL/TTL/DBSIZE/FLUSHDB/PING), so a small client keeps the project
dependency-free. In a normal environment, swap this for `redis.Redis` — the
AnswerCache only calls `execute(...)`.
"""
from __future__ import annotations

import socket
import ssl
import threading
from typing import Any


class RedisError(Exception):
    """Any connection, timeout, protocol or server error."""


class MiniRedis:
    def __init__(self, host: str, port: int, db: int = 0, timeout: float = 0.5,
                 username: str = "", password: str = "", tls: bool = False,
                 tls_ca_certs: str = "", tls_certfile: str = "", tls_keyfile: str = "",
                 tls_check_hostname: bool = True):
        if not host or not (1 <= int(port) <= 65535) or int(db) < 0 or timeout <= 0:
            raise ValueError("invalid Redis host, port, database, or timeout")
        if username and not password:
            raise ValueError("REDIS_PASSWORD is required when REDIS_USERNAME is set")
        if tls_keyfile and not tls_certfile:
            raise ValueError("REDIS_TLS_CERTFILE is required when REDIS_TLS_KEYFILE is set")
        self.host, self.port, self.db, self.timeout = host, int(port), int(db), float(timeout)
        self.username, self.password = username, password
        self.tls, self.tls_ca_certs = tls, tls_ca_certs
        self.tls_certfile, self.tls_keyfile = tls_certfile, tls_keyfile
        self.tls_check_hostname = tls_check_hostname
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()

    # -- connection management -------------------------------------------------
    def _connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        if self.tls:
            context = ssl.create_default_context(cafile=self.tls_ca_certs or None)
            context.check_hostname = self.tls_check_hostname
            if not self.tls_check_hostname:
                context.verify_mode = ssl.CERT_REQUIRED
            if self.tls_certfile:
                context.load_cert_chain(self.tls_certfile, self.tls_keyfile or None)
            sock = context.wrap_socket(sock, server_hostname=self.host if self.tls_check_hostname else None)
        self._sock, self._file = sock, sock.makefile("rb")
        try:
            if self.password:
                if self.username:
                    self._roundtrip("AUTH", self.username, self.password)
                else:
                    self._roundtrip("AUTH", self.password)
            if self.db:
                self._roundtrip("SELECT", str(self.db))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for obj in (self._file, self._sock):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass
        self._sock = self._file = None

    # -- protocol ---------------------------------------------------------------
    @staticmethod
    def _encode(args: tuple) -> bytes:
        parts = [b"*%d\r\n" % len(args)]
        for a in args:
            b = a if isinstance(a, bytes) else str(a).encode("utf-8")
            parts.append(b"$%d\r\n%s\r\n" % (len(b), b))
        return b"".join(parts)

    def _read_reply(self) -> Any:
        line = self._file.readline()
        if not line:
            raise ConnectionError("connection closed by server")
        kind, payload = line[:1], line[1:-2]
        if kind == b"+":
            return payload.decode()
        if kind == b"-":
            raise RedisError(payload.decode())
        if kind == b":":
            return int(payload)
        if kind == b"$":
            n = int(payload)
            if n == -1:
                return None
            data = self._file.read(n + 2)
            if len(data) != n + 2:
                raise ConnectionError("short read")
            return data[:-2].decode("utf-8")
        if kind == b"*":
            n = int(payload)
            return None if n == -1 else [self._read_reply() for _ in range(n)]
        raise RedisError(f"unexpected reply type {kind!r}")

    def _roundtrip(self, *args) -> Any:
        self._sock.sendall(self._encode(args))
        return self._read_reply()

    # -- public API -------------------------------------------------------------
    def execute(self, *args) -> Any:
        """Run one command. Raises RedisError on any failure (never returns junk)."""
        with self._lock:
            had_connection = self._sock is not None
            for attempt in (0, 1):
                try:
                    if self._sock is None:
                        self._connect()
                    return self._roundtrip(*args)
                except RedisError:
                    raise  # server-side error reply: do not retry
                except (OSError, ConnectionError, ValueError) as exc:
                    self.close()
                    # retry once only if a pooled (possibly stale) connection failed
                    if attempt == 0 and had_connection:
                        had_connection = False
                        continue
                    raise RedisError(f"redis unavailable: {exc}") from exc
        raise RedisError("unreachable")  # pragma: no cover

    # convenience wrappers
    def ping(self) -> bool:
        return self.execute("PING") == "PONG"

    def get(self, key: str):
        return self.execute("GET", key)

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        """SET with optional EX/NX. With nx=True returns None if the key already exists."""
        return self.execute("SET", key, value, *(("EX", ex) if ex else ()), *(("NX",) if nx else ()))

    def delete(self, key: str):
        return self.execute("DEL", key)

    def compare_and_delete(self, key: str, expected_value: str) -> bool:
        """Atomically delete a lock only when its ownership token still matches."""
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
        return self.execute("EVAL", script, 1, key, expected_value) == 1

    def ttl(self, key: str) -> int:
        return self.execute("TTL", key)

    def dbsize(self) -> int:
        return self.execute("DBSIZE")

    def flushdb(self):
        return self.execute("FLUSHDB")
