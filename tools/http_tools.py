"""只读公网 HTTP 工具；凭据和请求头不交给 Agent 控制。"""
from __future__ import annotations
import json
from langchain_core.tools import tool
from core.http_client import HttpClient, HttpClientError

_CLIENT = HttpClient()

@tool
def http_request(url: str, accept: str = "text/plain, application/json") -> str:
    """读取一个受网络策略保护的公网 URL（仅 GET）。禁止发送凭据、写入或任意请求头。"""
    if len(str(url or "")) > 4000 or len(str(accept or "")) > 200:
        return json.dumps({"operation": "http_request", "status": "invalid_input", "error": "参数过长"}, ensure_ascii=False)
    try:
        response = _CLIENT.get(str(url or "").strip(), accept=str(accept or "text/plain, application/json"))
        body = response.body.decode("utf-8", errors="replace")
        return json.dumps({"operation": "http_request", "status": "success", "url": response.url, "status_code": response.status_code, "content_type": response.content_type, "body": body, "truncated": response.truncated, "elapsed_seconds": response.elapsed_seconds}, ensure_ascii=False)
    except HttpClientError as exc:
        return json.dumps({"operation": "http_request", "status": exc.status, "error": str(exc), "http_status": exc.http_status}, ensure_ascii=False)

__all__ = ["http_request"]
