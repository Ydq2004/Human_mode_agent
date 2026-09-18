"""浏览器只读工具。交互动作暂不开放，避免未经确认的外部副作用。"""
from __future__ import annotations
import json
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Iterator
from langchain_core.tools import tool
from core.browser_service import BrowserService
_SERVICE: ContextVar[BrowserService | None] = ContextVar("browser_service", default=None)
@contextmanager
def browser_context(service: BrowserService) -> Iterator[None]:
    token = _SERVICE.set(service)
    try: yield
    finally: _SERVICE.reset(token)
def _call(fn, *args):
    service = _SERVICE.get()
    if service is None: return {"status": "unavailable", "reason": "浏览器服务未配置"}
    try: return fn(*args)
    except Exception as exc: return {"status": "failed", "error": type(exc).__name__, "reason": str(exc)[:500]}
@tool
def browser_open(url: str) -> str:
    """在临时只读浏览器会话中打开公网网页。"""
    return json.dumps({"operation": "browser_open", **(_call(_SERVICE.get().open, url) if _SERVICE.get() else {"status":"unavailable"})}, ensure_ascii=False)
@tool
def browser_snapshot(session_id: str, page_id: str = "") -> str:
    """读取页面标题、URL 和有限文本。"""
    return json.dumps({"operation": "browser_snapshot", **(_call(_SERVICE.get().snapshot, session_id, page_id) if _SERVICE.get() else {"status":"unavailable"})}, ensure_ascii=False)
@tool
def browser_close(session_id: str) -> str:
    """关闭浏览器会话。"""
    return json.dumps({"operation": "browser_close", **(_call(_SERVICE.get().close, session_id) if _SERVICE.get() else {"status":"unavailable"})}, ensure_ascii=False)
@tool
def browser_click(session_id: str, page_id: str, ref: str) -> str:
    """请求点击快照中的元素；只提出确认请求，不直接执行。"""
    return json.dumps({"operation": "browser_click", **(_call(_SERVICE.get().request_action, session_id, page_id, "click", ref) if _SERVICE.get() else {"status":"unavailable"})}, ensure_ascii=False)
@tool
def browser_fill(session_id: str, page_id: str, ref: str, value: str) -> str:
    """请求填写快照中的输入框；只提出确认请求，不直接执行。"""
    return json.dumps({"operation": "browser_fill", **(_call(_SERVICE.get().request_action, session_id, page_id, "fill", ref, value) if _SERVICE.get() else {"status":"unavailable"})}, ensure_ascii=False)
__all__ = ["browser_context", "browser_open", "browser_snapshot", "browser_close", "browser_click", "browser_fill"]
