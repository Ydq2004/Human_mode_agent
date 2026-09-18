"""受限 HTTP 传输层，不包含搜索或角色语义。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from email.message import Message
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from core.network_policy import check_url


class HttpClientError(RuntimeError):
    def __init__(self, message: str, *, status: str = "upstream_error", http_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status_code: int
    content_type: str
    body: bytes
    truncated: bool
    elapsed_seconds: float


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpClient:
    def __init__(self, *, timeout_seconds: float = 15, max_response_bytes: int = 2_000_000,
                 max_redirects: int = 3, opener: Callable[..., Any] | None = None,
                 allow_http: bool = False, allowed_hosts: set[str] | None = None):
        if timeout_seconds <= 0 or max_response_bytes < 1 or max_redirects < 0:
            raise ValueError("HTTP 客户端限制必须有效")
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.max_redirects = int(max_redirects)
        self.allow_http = bool(allow_http)
        self.allowed_hosts = set(allowed_hosts or set())
        self._opener = opener or build_opener(_NoRedirect())

    def get(self, url: str, *, query: dict[str, str] | None = None, accept: str = "text/plain, application/json", headers: dict[str, str] | None = None) -> HttpResponse:
        from urllib.parse import urlencode, urlsplit, urlunsplit
        current = str(url or "").strip()
        if query:
            parsed = urlsplit(current)
            current = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        started = time.monotonic()
        for _ in range(self.max_redirects + 1):
            check = check_url(current, allow_http=self.allow_http, allowed_hosts=self.allowed_hosts)
            if not check.allowed:
                raise HttpClientError(check.reason, status="denied")
            request_headers = {"Accept": accept, "User-Agent": "HumanModeAgent/1.0"}
            if headers:
                request_headers.update({str(k): str(v) for k, v in headers.items()})
            request = Request(current, method="GET", headers=request_headers)
            try:
                response = self._opener(request, timeout=self.timeout_seconds)
                status_code = int(getattr(response, "status", 200) or 200)
                headers: Message = getattr(response, "headers", Message())
                content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
                location = headers.get("Location")
                if status_code in {301, 302, 303, 307, 308} and location:
                    from urllib.parse import urljoin
                    current = urljoin(current, location)
                    continue
                try:
                    body = response.read(self.max_response_bytes + 1)
                except TypeError:
                    body = response.read()
                truncated = len(body) > self.max_response_bytes
                return HttpResponse(current, status_code, content_type, body[:self.max_response_bytes], truncated, round(time.monotonic() - started, 3))
            except HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                    from urllib.parse import urljoin
                    current = urljoin(current, exc.headers["Location"])
                    continue
                raise HttpClientError(f"HTTP {exc.code}", http_status=exc.code) from exc
            except (URLError, OSError, TimeoutError) as exc:
                raise HttpClientError(str(exc), status="timeout" if isinstance(exc, TimeoutError) else "upstream_error") from exc
        raise HttpClientError("重定向次数超过限制", status="redirect_limit")


__all__ = ["HttpClient", "HttpClientError", "HttpResponse"]
