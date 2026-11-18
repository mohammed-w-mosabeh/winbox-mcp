"""Minimal, dependency-free MikroTik RouterOS API client (the protocol WinBox-era tools use on 8728/8729).

Works with RouterOS v6 (incl. pre-6.43 challenge login) and v7.
"""
from __future__ import annotations

import binascii
import hashlib
import socket
import ssl
import threading
from typing import Any, Iterable


class RouterOSError(Exception):
    """A !trap / !fatal reply or a connection problem."""


def _encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    if n < 0x200000:
        return (n | 0xC00000).to_bytes(3, "big")
    if n < 0x10000000:
        return (n | 0xE0000000).to_bytes(4, "big")
    return b"\xF0" + n.to_bytes(4, "big")


def encode_word(word: str) -> bytes:
    data = word.encode("utf-8")
    return _encode_length(len(data)) + data


class RouterOSApi:
    def __init__(self, host: str, username: str, password: str, port: int | None = None,
                 use_ssl: bool = False, verify_ssl: bool = False, timeout: float = 15.0):
        self.host = host
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.port = port or (8729 if use_ssl else 8728)
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- transport
    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.use_ssl:
            ctx = ssl.create_default_context()
            if not self.verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                # RouterOS API-SSL without a certificate uses anonymous DH ciphers.
                try:
                    ctx.set_ciphers("ADH:ALL:@SECLEVEL=0")
                except ssl.SSLError:
                    pass
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        self.sock = raw
        self._login()

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RouterOSError("connection closed by router")
            buf += chunk
        return buf

    def _read_length(self) -> int:
        b = self._recv_exact(1)[0]
        if b < 0x80:
            return b
        if b < 0xC0:
            return ((b & 0x3F) << 8) | self._recv_exact(1)[0]
        if b < 0xE0:
            return ((b & 0x1F) << 16) | int.from_bytes(self._recv_exact(2), "big")
        if b < 0xF0:
            return ((b & 0x0F) << 24) | int.from_bytes(self._recv_exact(3), "big")
        return int.from_bytes(self._recv_exact(4), "big")

    def _read_sentence(self) -> list[str]:
        words = []
        while True:
            n = self._read_length()
            if n == 0:
                return words
            words.append(self._recv_exact(n).decode("utf-8", errors="replace"))

    def _write_sentence(self, words: Iterable[str]) -> None:
        assert self.sock
        self.sock.sendall(b"".join(encode_word(w) for w in words) + b"\x00")

    # ---------------------------------------------------------------- protocol
    def _login(self) -> None:
        replies = self._talk(["/login", f"=name={self.username}", f"=password={self.password}"])
        # Pre-6.43: router answers with a challenge in =ret=.
        for kind, attrs in replies:
            if kind == "!done" and "ret" in attrs:
                chal = binascii.unhexlify(attrs["ret"])
                md = hashlib.md5(b"\x00" + self.password.encode() + chal).hexdigest()
                self._talk(["/login", f"=name={self.username}", f"=response=00{md}"])

    def _talk(self, words: list[str]) -> list[tuple[str, dict[str, str]]]:
        self._write_sentence(words)
        out: list[tuple[str, dict[str, str]]] = []
        trap: dict[str, str] | None = None
        while True:
            sentence = self._read_sentence()
            if not sentence:
                continue
            kind, attrs = sentence[0], {}
            for w in sentence[1:]:
                if w.startswith("="):
                    k, _, v = w[1:].partition("=")
                    attrs[k] = v
                elif w.startswith(".tag="):
                    attrs[".tag"] = w[5:]
            if kind == "!trap":
                trap = attrs
            elif kind == "!fatal":
                self.close()
                raise RouterOSError("fatal: " + " ".join(sentence[1:]))
            out.append((kind, attrs))
            if kind == "!done":  # (v7.18+ may send !empty before !done)
                break
        if trap is not None:
            msg = trap.get("message", str(trap))
            raise RouterOSError(msg)
        return out

    def command(self, path: str, params: dict[str, Any] | None = None,
                queries: list[str] | None = None, proplist: list[str] | None = None) -> list[dict[str, str]]:
        """Run any API command, e.g. path='/ip/address/print'. Returns the !re rows (plus !done attrs if any)."""
        words = [path if path.startswith("/") else "/" + path]
        for k, v in (params or {}).items():
            if v is None:
                continue
            if isinstance(v, bool):
                v = "yes" if v else "no"
            words.append(f"={k}={v}")
        if proplist:
            words.append("=.proplist=" + ",".join(proplist))
        for q in queries or []:
            words.append(q if q.startswith("?") else "?" + q)
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self.connect()
                    replies = self._talk(words)
                    break
                except (OSError, RouterOSError) as e:
                    if isinstance(e, RouterOSError) and self.sock is not None:
                        raise  # a real trap from the router, not a dead link
                    self.close()
                    if attempt == 2:
                        raise RouterOSError(f"cannot reach {self.host}:{self.port}: {e}") from e
        rows = [a for k, a in replies if k == "!re"]
        done = [a for k, a in replies if k == "!done" and a]
        return rows + done
