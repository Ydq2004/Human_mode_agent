"""可选的只读浏览器服务。每个服务实例维护独立临时会话。"""
from __future__ import annotations
import re
from uuid import uuid4

class BrowserService:
    def __init__(self, *, max_text: int = 12000):
        self.max_text = max_text
        self._sessions = {}
        self._pending_actions = {}
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
        elements = []
        locator = page.locator("a,button,input,textarea,select")
        for index in range(min(locator.count(), 50)):
            node = locator.nth(index)
            try:
                tag = node.evaluate("element => element.tagName.toLowerCase()")
                label = (node.get_attribute("aria-label") or node.inner_text() or node.get_attribute("placeholder") or "").strip()[:200]
                elements.append({"ref": f"e{index + 1}", "kind": tag, "label": label})
            except Exception:
                continue
        item["elements"] = {entry["ref"]: index for index, entry in enumerate(elements)}
        return {"status": "success", "session_id": session_id, "page_id": item["page_id"], "url": page.url, "title": page.title()[:500], "text": text, "elements": elements, "truncated": len(text) >= self.max_text}

    def request_action(self, session_id: str, page_id: str, action: str, ref: str, value: str = "") -> dict:
        item = self._get(session_id, page_id)
        if item is None:
            return {"status": "not_found"}
        if action not in {"click", "fill"} or not re.fullmatch(r"e\d+", str(ref or "")):
            return {"status": "invalid_input", "reason": "只支持快照中的 eN 元素引用和 click/fill 动作"}
        if action == "fill" and len(value) > 2000:
            return {"status": "invalid_input", "reason": "输入内容过长"}
        if ref not in item.get("elements", {}):
            return {"status": "invalid_input", "reason": "元素引用已失效，请重新获取快照"}
        confirmation_id = f"browser_confirmation_{uuid4().hex}"
        self._pending_actions[confirmation_id] = {"session_id": session_id, "page_id": page_id, "action": action, "ref": ref, "value": value}
        return {"status": "confirmation_required", "confirmation_id": confirmation_id, "action": action, "ref": ref, "warning": "该动作可能改变页面或提交外部数据，必须由宿主界面确认后执行。"}

    def confirm_action(self, confirmation_id: str, *, approved: bool) -> dict:
        pending = self._pending_actions.pop(str(confirmation_id or ""), None)
        if pending is None:
            return {"status": "not_found"}
        if not approved:
            return {"status": "cancelled", "confirmation_id": confirmation_id}
        item = self._get(pending["session_id"], pending["page_id"])
        if item is None:
            return {"status": "not_found", "reason": "浏览器会话已关闭"}
        node = item["page"].locator("a,button,input,textarea,select").nth(item["elements"].get(pending["ref"], -1))
        try:
            if pending["action"] == "click":
                node.click(timeout=10000)
            else:
                node.fill(pending["value"], timeout=10000)
            return {"status": "success", "confirmation_id": confirmation_id, "action": pending["action"], "ref": pending["ref"], "url": item["page"].url}
        except Exception as exc:
            return {"status": "failed", "error": type(exc).__name__, "reason": str(exc)[:500]}

    def close(self, session_id: str) -> dict:
        item = self._sessions.pop(str(session_id or ""), None)
        if item is None: return {"status": "not_found"}
        for key, pending in list(self._pending_actions.items()):
            if pending.get("session_id") == session_id:
                self._pending_actions.pop(key, None)
        item["context"].close(); return {"status": "success", "session_id": session_id}

    def shutdown(self):
        for sid in list(self._sessions): self.close(sid)
        if self._browser: self._browser.close()
        if self._playwright: self._playwright.stop()

__all__ = ["BrowserService"]
