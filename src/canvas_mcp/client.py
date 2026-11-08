"""Read-only Canvas REST client.

Auth is either a Canvas access token (Authorization: Bearer) or the browser
session cookies captured after Microsoft SSO. Canvas accepts session cookies on
/api/v1 GET requests (that is exactly what its own web UI does), so after SSO
we can use the full documented REST API without ever seeing a password.

Guarantees:
* GET only. The client refuses any other HTTP method, so the AI cannot submit,
  post, delete or change anything in your Canvas account.
* Credentials are attached only to requests whose host == the Canvas host.
  Redirects (e.g. file downloads -> inst-fs / S3 signed URLs) are followed
  manually and auth is stripped when the host changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx

from .redact import scrub_url

log = logging.getLogger(__name__)

_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
USER_AGENT = "canvas-mcp/0.1 (+read-only)"


class AuthRequired(RuntimeError):
    """No valid session. User must run `canvas-mcp login` (never via the AI)."""


class CanvasError(RuntimeError):
    pass


@dataclass
class Session:
    kind: str  # "token" | "cookies"
    token: str | None = None
    cookies: list[dict[str, Any]] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "Session | None":
        if not payload:
            return None
        if payload.get("kind") == "token" and payload.get("token"):
            return cls(kind="token", token=payload["token"])
        if payload.get("kind") == "cookies" and payload.get("cookies"):
            return cls(kind="cookies", cookies=payload["cookies"])
        return None


class CanvasClient:
    def __init__(self, base_url: str, session: Session | None, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).hostname
        self.session = session
        self._http = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,  # we handle redirects to control credential scope
            headers={"User-Agent": USER_AGENT, "Accept": "application/json+canvas-string-ids, application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- auth plumbing --------------------------------------------------
    def _auth_headers(self, url: str) -> dict[str, str]:
        if urlparse(url).hostname != self.host or self.session is None:
            return {}
        if self.session.kind == "token":
            return {"Authorization": f"Bearer {self.session.token}"}
        parts = []
        for c in self.session.cookies or []:
            dom = (c.get("domain") or "").lstrip(".")
            if dom and not (self.host == dom or self.host.endswith("." + dom)):
                continue
            parts.append(f"{c['name']}={c['value']}")
        return {"Cookie": "; ".join(parts)} if parts else {}

    def _abs(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        return urljoin(self.base_url + "/", path_or_url.lstrip("/"))

    async def _send(self, url: str, params: Any = None, *, stream: bool = False, max_redirects: int = 8) -> httpx.Response:
        if self.session is None:
            raise AuthRequired("Not logged in.")
        for _ in range(max_redirects + 1):
            req = self._http.build_request("GET", url, params=params, headers=self._auth_headers(url))
            resp = await self._http.send(req, stream=stream)
            if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                loc = urljoin(str(resp.url), resp.headers["location"])
                await resp.aclose()
                lp = urlparse(loc)
                if lp.hostname == self.host and lp.path.startswith("/login"):
                    raise AuthRequired("Canvas session expired (redirected to login).")
                if lp.scheme != "https" and lp.hostname != self.host:
                    raise CanvasError("Refusing to follow redirect to non-HTTPS host.")
                url, params = loc, None
                continue
            if resp.status_code == 401:
                await resp.aclose()
                raise AuthRequired("Canvas rejected the stored session (401). It has probably expired.")
            if resp.status_code >= 400:
                body = (await resp.aread())[:300].decode("utf-8", "replace")
                await resp.aclose()
                raise CanvasError(f"Canvas returned HTTP {resp.status_code} for {scrub_url(str(resp.url))}: {body}")
            return resp
        raise CanvasError("Too many redirects.")

    # ---- public read API ------------------------------------------------
    async def get(self, path: str, params: Any = None) -> Any:
        resp = await self._send(self._abs(path), params)
        try:
            return resp.json()
        except ValueError as exc:
            # Canvas served HTML (e.g. SSO page) instead of JSON -> session gone.
            if "text/html" in resp.headers.get("content-type", ""):
                raise AuthRequired("Canvas returned an HTML page instead of JSON; session likely expired.") from exc
            raise CanvasError("Canvas returned non-JSON response") from exc

    async def paginate(self, path: str, params: dict[str, Any] | None = None, *, limit: int = 500) -> list[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out: list[Any] = []
        url: str | None = self._abs(path)
        first = True
        while url and len(out) < limit:
            resp = await self._send(url, params if first else None)
            first = False
            data = resp.json()
            if isinstance(data, dict):  # some endpoints wrap lists
                return [data]
            out.extend(data)
            m = _LINK_NEXT.search(resp.headers.get("link", ""))
            url = m.group(1) if m else None
            if url and urlparse(url).hostname != self.host:
                break  # never follow pagination off-host
        return out[:limit]

    async def stream_download(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """Download a file (following signed-URL redirects), capped at max_bytes."""
        resp = await self._send(self._abs(url), stream=True)
        try:
            ctype = resp.headers.get("content-type", "application/octet-stream")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise CanvasError(f"File exceeds download cap of {max_bytes // (1024*1024)} MB")
            return bytes(buf), ctype
        finally:
            await resp.aclose()

    async def whoami(self) -> dict[str, Any]:
        return await self.get("/api/v1/users/self")

    async def iter_pages(self, path: str, params: dict[str, Any] | None = None) -> AsyncIterator[Any]:  # pragma: no cover - convenience
        for item in await self.paginate(path, params):
            yield item
