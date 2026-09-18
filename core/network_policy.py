"""外部网络访问策略。

网络工具只能访问明确允许的公网 URL。每次重定向前都重新检查 URL，
避免把一次安全的公网请求重定向到本机或私网地址。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit


@dataclass(frozen=True)
class UrlCheck:
    allowed: bool
    url: str
    reason: str = ""
    host: str = ""


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _resolved_public(host: str) -> bool:
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        return False
    addresses = {item[4][0] for item in records if item[4]}
    return bool(addresses) and all(_is_public_ip(item) for item in addresses)


def check_url(url: str, *, allow_http: bool = False, allowed_hosts: set[str] | None = None) -> UrlCheck:
    raw = str(url or "").strip()
    try:
        parsed: SplitResult = urlsplit(raw)
    except ValueError:
        return UrlCheck(False, raw, "URL 格式无效")
    if parsed.scheme not in {"http", "https"}:
        return UrlCheck(False, raw, "只允许 http 或 https")
    if parsed.scheme == "http" and not allow_http:
        return UrlCheck(False, raw, "默认只允许 HTTPS")
    if parsed.username or parsed.password:
        return UrlCheck(False, raw, "URL 不允许携带用户名或密码")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return UrlCheck(False, raw, "URL 缺少主机名")
    allowed = {str(item).strip().lower().rstrip(".") for item in (allowed_hosts or set()) if str(item).strip()}
    if host in allowed:
        return UrlCheck(True, raw, host=host)
    if not _resolved_public(host):
        return UrlCheck(False, raw, "目标地址不是可访问的公网地址", host=host)
    return UrlCheck(True, raw, host=host)


__all__ = ["UrlCheck", "check_url"]
