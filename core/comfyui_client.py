"""ComfyUI API 适配器（框架层）。

这里只处理 HTTP 协议和真实任务状态，不理解角色意图，也不决定工作流节点。
所有网络调用都可通过 ``opener`` 注入测试替身，避免测试依赖本地 ComfyUI。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ComfyUIError(RuntimeError):
    """ComfyUI 不可用、拒绝任务或返回无效协议时抛出。"""


@dataclass(frozen=True)
class ComfyUIJob:
    prompt_id: str
    client_id: str


class ComfyUIClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        base = str(base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("ComfyUI 地址不能为空")
        if timeout_seconds <= 0 or poll_interval_seconds < 0:
            raise ValueError("ComfyUI 超时和轮询间隔必须有效")
        self.base_url = base
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._opener = opener or urlopen
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            response = self._opener(request, timeout=min(self.timeout_seconds, 30.0))
            raw = response.read()
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise ComfyUIError(f"ComfyUI 请求失败：{exc}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyUIError("ComfyUI 返回了无效 JSON") from exc
        if not isinstance(value, dict):
            raise ComfyUIError("ComfyUI 返回格式不是对象")
        return value

    def submit(self, workflow: dict[str, Any], *, client_id: str) -> ComfyUIJob:
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("workflow 必须是非空对象")
        result = self._request("POST", "/prompt", {"prompt": workflow, "client_id": str(client_id)})
        prompt_id = str(result.get("prompt_id") or "").strip()
        if not prompt_id:
            error = result.get("error") or result.get("node_errors") or "ComfyUI 没有返回 prompt_id"
            raise ComfyUIError(f"ComfyUI 拒绝任务：{error}")
        return ComfyUIJob(prompt_id=prompt_id, client_id=str(client_id))

    def history(self, prompt_id: str) -> dict[str, Any]:
        prompt_id = str(prompt_id or "").strip()
        if not prompt_id:
            raise ValueError("prompt_id 不能为空")
        return self._request("GET", f"/history/{prompt_id}")

    def wait_for_history(self, prompt_id: str) -> dict[str, Any]:
        started = self._clock()
        while True:
            history = self.history(prompt_id)
            record = history.get(prompt_id) if isinstance(history.get(prompt_id), dict) else history
            status = record.get("status") if isinstance(record, dict) else None
            if isinstance(status, dict) and status.get("status_str") in {"error", "failed"}:
                raise ComfyUIError(f"ComfyUI 任务失败：{status}")
            if isinstance(record, dict) and record.get("outputs"):
                return record
            if self._clock() - started >= self.timeout_seconds:
                raise TimeoutError(f"ComfyUI 任务超过 {self.timeout_seconds:g} 秒")
            self._sleeper(self.poll_interval_seconds)


__all__ = ["ComfyUIClient", "ComfyUIError", "ComfyUIJob"]
