"""可选的只读浏览器服务。每个服务实例维护独立临时会话。"""
from __future__ import annotations
import re
from uuid import uuid4

class BrowserService:
    def __init__(self, *, max_text: int = 12000):
        self.max_text = max_text
        self._sessions = {}
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self.available = True
        except Exception:
            self._playwright = self._browser = None
            self.available = False

    def open(self, url: str) -> dict:
        if not self.available: return {"status": "unavailable", "reason": "Playwright 或浏览器内核未安装"}
        if not isinstance(url, str) or len(url) > 4000: return {"status": "invalid_input", "reason": "URL 无效"}
        from core.network_policy import check_url
        check = check_url(url)
        if not check.allowed: return {"status": "denied", "reason": check.reason}
        context = self._browser.new_context()
        page = context.new_page(); page.goto(url, wait_until="domcontentloaded", timeout=15000)
        final_check = check_url(page.url)
        if not final_check.allowed:
            context.close()
            return {"status": "denied", "reason": "页面重定向到不允许的地址"}
        sid = f"browser_{uuid4().hex}"; pid = f"page_{uuid4().hex}"
        self._sessions[sid] = {"context": context, "page": page, "page_id": pid}
        return {"status": "success", "session_id": sid, "page_id": pid, "url": page.url, "title": page.title()[:500]}

    def _get(self, session_id: str, page_id: str = ""):
        item = self._sessions.get(str(session_id or ""))
        if not item or (page_id and item["page_id"] != page_id): return None
        return item

    def snapshot(self, session_id: str, page_id: str = "") -> dict:
        item = self._get(session_id, page_id)
        if item is None: return {"status": "not_found"}
        page = item["page"]
        text = re.sub(r"\s+", " ", page.locator("body").inner_text())[:self.max_text]
        return {"status": "success", "session_id": session_id, "page_id": item["page_id"], "url": page.url, "title": page.title()[:500], "text": text, "truncated": len(text) >= self.max_text}

    def close(self, session_id: str) -> dict:
        item = self._sessions.pop(str(session_id or ""), None)
        if item is None: return {"status": "not_found"}
        item["context"].close(); return {"status": "success", "session_id": session_id}

    def shutdown(self):
        for sid in list(self._sessions): self.close(sid)
        if self._browser: self._browser.close()
        if self._playwright: self._playwright.stop()

__all__ = ["BrowserService"]
