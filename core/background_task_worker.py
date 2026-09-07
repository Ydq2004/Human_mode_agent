"""后台任务执行器。

Worker 只负责领取任务、调用已经注册的业务处理器并写入状态；它不会调用主
Agent，也不会直接修改 checkpoint。任务结果通过持久化事件交给主循环。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from time import sleep
from time import monotonic
from uuid import uuid4
from typing import Any, Callable

from memory import background_task_store as store


TaskHandler = Callable[[dict[str, Any]], dict[str, Any]]


class BackgroundTaskWorker:
    def __init__(
        self,
        handlers: dict[str, TaskHandler] | None = None,
        *,
        max_workers: int = 1,
        event_bus=None,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers 必须是正整数")
        self.handlers = dict(handlers or {})
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="background-task")
        self._worker_id = f"worker_{uuid4().hex}"
        self._lock = Lock()
        self._scheduled: set[str] = set()
        self._closed = False
        self._event_bus = event_bus
        self._stop_event = Event()
        self._poll_interval = 1.0
        self._poller = Thread(target=self._poll_loop, name="background-task-dispatch", daemon=False)
        self._poller.start()

    def register(self, task_type: str, handler: TaskHandler) -> None:
        if not str(task_type or "").strip() or not callable(handler):
            raise ValueError("task_type 和 handler 必须有效")
        self.handlers[str(task_type).strip()] = handler

    def submit(self, task_id: str) -> bool:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise ValueError("task_id 不能为空")
        with self._lock:
            if self._closed:
                raise RuntimeError("BackgroundTaskWorker 已关闭")
            if task_id in self._scheduled:
                return False
            self._scheduled.add(task_id)
            self._executor.submit(self._run, task_id)
        return True

    def submit_due(self, *, limit: int = 50) -> int:
        """把启动恢复或重试到期的 queued 任务重新交给执行器。"""
        tasks = [
            task for task in store.list_tasks(limit=limit)
            if task.get("status") in {"queued", "retry_wait"}
        ]
        count = 0
        for task in tasks:
            if self.submit(task["task_id"]):
                count += 1
        return count

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.submit_due(limit=50)
            except Exception:
                # 任务本身的账本会保留状态；调度线程异常不能杀死主程序。
                pass
            self._stop_event.wait(self._poll_interval)

    def _run(self, task_id: str) -> None:
        try:
            task = store.claim_task(task_id, worker_id=self._worker_id)
            if task is None:
                return
            handler = self.handlers.get(str(task.get("task_type") or ""))
            if handler is None:
                self._finish_failure(task, "handler_missing", "没有注册对应的后台任务处理器")
                return
            started = monotonic()
            try:
                result = handler(task)
                if not isinstance(result, dict):
                    result = {"value": result}
                timeout = float((task.get("request") or {}).get("timeout_seconds") or 0)
                if timeout > 0 and monotonic() - started > timeout:
                    self._finish_failure(task, "timeout", f"任务超过 {timeout:g} 秒限制")
                    return
                latest = store.get_task(task_id, include_result=False) or task
                if latest.get("status") == "cancel_requested":
                    store.transition_task(task_id, "cancelled", expected_status="cancel_requested")
                    return
                self._finish_success(task, result)
            except TimeoutError as exc:
                self._finish_failure(task, "timeout", str(exc) or "后台任务超时")
            except Exception as exc:
                self._finish_failure(task, type(exc).__name__, str(exc))
        finally:
            with self._lock:
                self._scheduled.discard(task_id)

    def _finish_success(self, task: dict[str, Any], result: dict[str, Any]) -> None:
        updated = store.transition_task(
            task["task_id"], "succeeded", expected_status="running", result=result,
        )
        if updated is not None:
            event = store.append_event(task["task_id"], "completed", {
                "task_id": task["task_id"],
                "task_type": task.get("task_type"),
                "status": "succeeded",
                "summary": str(result.get("summary") or result.get("answer") or "任务已完成")[:2000],
            })
            self._publish(event)

    def _finish_failure(self, task: dict[str, Any], code: str, message: str) -> None:
        latest = store.get_task(task["task_id"], include_result=False) or task
        attempts = int(latest.get("attempt_count") or 0)
        max_attempts = int(latest.get("max_attempts") or 1)
        if attempts < max_attempts and code not in {"cancelled", "handler_missing"}:
            from datetime import datetime, timedelta
            retry_seconds = float((latest.get("request") or {}).get("retry_delay_seconds") or 1)
            next_retry = (datetime.now() + timedelta(seconds=max(0.0, retry_seconds))).isoformat()
            updated = store.transition_task(
                task["task_id"], "retry_wait", expected_status="running",
                error_code=code, error_message=message, next_retry_at=next_retry,
            )
            if updated is not None:
                event = store.append_event(task["task_id"], "retry_scheduled", {
                    "task_id": task["task_id"], "status": "retry_wait",
                    "error_code": code, "error_message": message,
                })
                self._publish(event)
            return
        updated = store.transition_task(
            task["task_id"], "exhausted" if attempts >= max_attempts and max_attempts > 1 else "failed",
            expected_status="running", error_code=code, error_message=message,
        )
        if updated is not None:
            event = store.append_event(task["task_id"], "failed", {
                "task_id": task["task_id"], "task_type": task.get("task_type"),
                "status": updated.get("status"), "error_code": code,
                "error_message": message,
            })
            self._publish(event)

    def _publish(self, event: dict[str, Any]) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(event["event_id"])

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._stop_event.set()
        if wait:
            self._poller.join(timeout=5)
        self._executor.shutdown(wait=wait)


__all__ = ["BackgroundTaskWorker"]
