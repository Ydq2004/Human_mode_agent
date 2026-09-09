"""ComfyUI 外部进程监督器（应用基础设施层）。

它不参与工作流编排，只负责本机进程生命周期：先复用已有服务，再按明确
配置启动自己的子进程；关闭时只终止自己拥有的进程。未配置 executable 时
返回 unavailable，让文字 Agent 继续运行，生图工具再如实报告不可用。
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ComfyUIStartupError(RuntimeError):
    """ComfyUI 已按配置启动但没有在截止时间内就绪。"""


@dataclass(frozen=True)
class ComfyUIProcessStatus:
    state: str
    base_url: str
    owned: bool
    pid: int | None = None
    log_path: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "base_url": self.base_url,
            "owned": self.owned,
            "pid": self.pid,
            "log_path": self.log_path,
            "error": self.error,
        }


class ComfyUIProcessManager:
    def __init__(
        self,
        *,
        base_url: str,
        executable: str = "",
        args: Sequence[str] = (),
        cwd: str | Path | None = None,
        log_root: str | Path | None = None,
        enabled: bool = True,
        startup_timeout_seconds: float = 120.0,
        healthcheck_timeout_seconds: float = 2.0,
        poll_interval_seconds: float = 0.5,
        stop_on_exit: bool = False,
        config_error: str = "",
        opener: Callable[..., Any] = urlopen,
        popen: Callable[..., Any] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        base_url = str(base_url or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("ComfyUI base_url 不能为空")
        if startup_timeout_seconds <= 0 or healthcheck_timeout_seconds <= 0:
            raise ValueError("ComfyUI 超时必须为正数")
        if poll_interval_seconds < 0:
            raise ValueError("ComfyUI 轮询间隔不能为负数")
        self.base_url = base_url
        self.executable = str(executable or "").strip()
        self.args = tuple(str(item) for item in args)
        self.cwd = str(cwd) if cwd else None
        self.log_root = Path(log_root).resolve(strict=False) if log_root else None
        self.enabled = bool(enabled)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.healthcheck_timeout_seconds = float(healthcheck_timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.stop_on_exit = bool(stop_on_exit)
        self.config_error = str(config_error or "").strip()
        self._opener = opener
        self._popen = popen
        self._sleep = sleep
        self._process = None
        self._owned = False
        self._log_handle = None
        self._status = ComfyUIProcessStatus("unknown", self.base_url, False)

    def _health_url(self) -> str:
        return f"{self.base_url}/system_stats"

    def is_ready(self) -> bool:
        request = Request(self._health_url(), method="GET")
        try:
            with self._opener(request, timeout=self.healthcheck_timeout_seconds) as response:
                if int(getattr(response, "status", 200)) >= 400:
                    return False
                raw = response.read()
                json.loads(raw.decode("utf-8"))
                return True
        except (OSError, ValueError, HTTPError, URLError, TimeoutError):
            return False

    def _open_log(self) -> tuple[Any, Path]:
        root = self.log_root or Path.cwd() / "runtime_logs"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"comfyui_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        return path.open("ab"), path

    def start_if_needed(self) -> ComfyUIProcessStatus:
        if not self.enabled:
            self._status = ComfyUIProcessStatus("disabled", self.base_url, False)
            return self._status
        if self.is_ready():
            self._status = ComfyUIProcessStatus("ready", self.base_url, False)
            return self._status
        if not self.executable:
            self._status = ComfyUIProcessStatus(
                "unavailable",
                self.base_url,
                False,
                error=self.config_error or "未配置 COMFYUI_EXECUTABLE，未启动外部进程",
            )
            return self._status

        command = [self.executable, *self.args]
        log_handle, log_path = self._open_log()
        try:
            process = self._popen(
                command,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=False,
            )
        except Exception as exc:
            log_handle.close()
            self._status = ComfyUIProcessStatus(
                "failed", self.base_url, False, log_path=str(log_path),
                error=f"ComfyUI 进程启动失败：{type(exc).__name__}: {exc}",
            )
            raise ComfyUIStartupError(self._status.error) from exc
        self._process = process
        self._owned = True
        self._log_handle = log_handle
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.is_ready():
                self._status = ComfyUIProcessStatus(
                    "ready", self.base_url, True, getattr(process, "pid", None), str(log_path)
                )
                return self._status
            if getattr(process, "poll", lambda: None)() is not None:
                break
            self._sleep(self.poll_interval_seconds)

        self._terminate_owned()
        self._status = ComfyUIProcessStatus(
            "failed", self.base_url, True, getattr(process, "pid", None), str(log_path),
            "ComfyUI 未在启动时限内通过健康检查",
        )
        raise ComfyUIStartupError(self._status.error)

    def _terminate_owned(self) -> None:
        process = self._process
        if not self._owned or process is None:
            return
        try:
            if getattr(process, "poll", lambda: None)() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
        finally:
            self._process = None
            self._owned = False
            if self._log_handle is not None:
                self._log_handle.close()
                self._log_handle = None

    def stop(self) -> ComfyUIProcessStatus:
        if self._owned and self.stop_on_exit:
            self._terminate_owned()
            self._status = ComfyUIProcessStatus("stopped", self.base_url, False)
        elif self._owned:
            self._status = ComfyUIProcessStatus(
                "ready", self.base_url, True, getattr(self._process, "pid", None), self._status.log_path
            )
        return self._status

    def status(self) -> ComfyUIProcessStatus:
        return self._status


__all__ = ["ComfyUIProcessManager", "ComfyUIProcessStatus", "ComfyUIStartupError"]
