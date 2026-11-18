"""RouterOS v7 REST transport (/rest over the www / www-ssl service, e.g. a WebFig port).

Exposes the same `command()` interface as RouterOSApi, using REST's universal POST:
POST /rest/<menu>/<command> with a JSON body of parameters, `.proplist` and `.query`.
"""
from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .routeros_api import RouterOSError


class RouterOSRest:
    def __init__(self, host: str, username: str, password: str, port: int | None = None,
                 use_ssl: bool = False, verify_ssl: bool = False, timeout: float = 30.0):
        self.host, self.username, self.password = host, username, password
        self.use_ssl = use_ssl
        self.port = port or (443 if use_ssl else 80)
        self.timeout = timeout
        self.ctx = None
        if use_ssl:
            self.ctx = ssl.create_default_context()
            if not verify_ssl:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.auth = "Basic " + token

    def close(self) -> None:  # stateless
        pass

    def command(self, path: str, params: dict[str, Any] | None = None,
                queries: list[str] | None = None, proplist: list[str] | None = None) -> list[dict[str, Any]]:
        path = "/" + path.strip("/")
        body: dict[str, Any] = {}
        for k, v in (params or {}).items():
            if v is None:
                continue
            if isinstance(v, bool):
                v = "yes" if v else "no"
            body[k] = str(v)
        if proplist:
            body[".proplist"] = list(proplist)
        if queries:
            body[".query"] = [q[1:] if q.startswith("?") else q for q in queries]
        scheme = "https" if self.use_ssl else "http"
        url = f"{scheme}://{self.host}:{self.port}/rest{path}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": self.auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            try:
                j = json.loads(txt)
                msg = j.get("detail") or j.get("message") or txt
            except ValueError:
                msg = txt
            raise RouterOSError(f"HTTP {e.code}: {msg}") from e
        except (urllib.error.URLError, OSError) as e:
            raise RouterOSError(f"cannot reach {url}: {e}") from e
        if not raw.strip():
            return []
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        return [data] if data else []
