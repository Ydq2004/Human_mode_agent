"""后台检查并修复 SQLite/Chroma 索引不一致。"""

from __future__ import annotations

from threading import Event, Lock, Thread
from typing import Any, Callable


class VectorRepairWorker:
    """低频运行的索引修复线程。

    线程只处理可重建的 Chroma 索引；SQLite 中不存在的 Chroma 孤立 id
    只会记录在检查结果中，不会被自动删除。
    """

    def __init__(
        self,
        inspect: Callable[[], dict[str, Any]],
        repair: Callable[[int | None], list[dict[str, Any]]],
        *,
        interval_seconds: float = 300.0,
        batch_size: int = 10,
    ) -> None:
        if isinstance(interval_seconds, bool) or interval_seconds < 0:
            raise ValueError("interval_seconds 必须是非负数字")
        if isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("batch_size 必须是正整数")

        self._inspect = inspect
        self._repair = repair
        self._interval_seconds = float(interval_seconds)
        self._batch_size = int(batch_size)
        self._stop = Event()
        self._lock = Lock()
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._thread = Thread(target=self._run, name="vector-repair", daemon=True)
        self._thread.start()

    def run_once(self) -> dict[str, Any]:
        """执行一次检查和有界修复；失败只返回状态，不抛出到主循环。"""
        try:
            consistency = self._inspect()
            repaired = self._repair(self._batch_size)
            result = {"consistency": consistency, "repairs": repaired}
            with self._lock:
                self._last_result = result
                self._last_error = None
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._last_error = error
            return {"status": "failed", "error": error}

    def snapshot(self) -> dict[str, Any]:
        """返回最近一次状态，供调试或健康检查读取。"""
        with self._lock:
            return {
                "last_result": self._last_result,
                "last_error": self._last_error,
                "running": self._thread.is_alive() and not self._stop.is_set(),
            }

    def _run(self) -> None:
        # 启动后先检查一次，之后按固定间隔执行。
        while not self._stop.is_set():
            self.run_once()
            if self._stop.wait(self._interval_seconds):
                break

    def shutdown(self, *, wait: bool = True) -> None:
        """请求线程停止；不会取消正在进行的单次修复。"""
        self._stop.set()
        if wait and self._thread.is_alive():
            # 不把关闭等待时间绑定到下一次调度间隔；间隔可能是几分钟，
            # 关闭时只等待一个短的固定窗口，避免拖住 CLI 退出。
            self._thread.join(timeout=5.0)
