"""外部搜索工具；返回外部资料，不写入长期记忆。"""
from __future__ import annotations
import json
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Iterator
from langchain_core.tools import tool
from core.web_search_service import WebSearchService

_SERVICE: ContextVar[WebSearchService | None] = ContextVar("web_search_service", default=None)
@contextmanager
def web_search_context(service: WebSearchService) -> Iterator[None]:
    token = _SERVICE.set(service)
    try: yield
    finally: _SERVICE.reset(token)

@tool
def web_search(query: str, max_results: int = 5, recency: str = "", domains: list[str] | None = None) -> str:
    """搜索公开网页。结果是外部资料，不能当作角色亲身记忆。"""
    service = _SERVICE.get()
    if service is None:
        return json.dumps({"operation": "web_search", "status": "unavailable", "error": "搜索服务未配置"}, ensure_ascii=False)
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 10:
        return json.dumps({"operation": "web_search", "status": "invalid_input", "error": "max_results 必须为 1 到 10"}, ensure_ascii=False)
    result = service.search(query, max_results=max_results, recency=recency, domains=domains)
    return json.dumps({"operation": "web_search", **result}, ensure_ascii=False)

__all__ = ["web_search", "web_search_context"]
