"""后台任务完成通知。

通知队列只是唤醒提示，SQLite 账本才是真实来源。进程重启后可重新读取未投递
事件，避免后台任务完成但主程序没有看到通知。
"""

from __future__ import annotations

from queue import Empty, Queue
from threading import Lock

from memory import background_task_store as store


class BackgroundTaskEventBus:
    def __init__(self) -> None:
        self._queue: Queue[str] = Queue()
        self._queued: set[str] = set()
        self._lock = Lock()

    def publish(self, event_id: str) -> None:
        event_id = str(event_id or "").strip()
        if not event_id:
            return
        with self._lock:
            if event_id in self._queued:
                return
            self._queued.add(event_id)
            self._queue.put(event_id)

    def recover(self, *, limit: int = 50) -> int:
        count = 0
        for event in store.list_pending_events(limit=limit):
            self.publish(event["event_id"])
            count += 1
        return count

    def drain(self, *, limit: int = 50) -> list[dict]:
        output = []
        for _ in range(limit):
            try:
                event_id = self._queue.get_nowait()
            except Empty:
                break
            with self._lock:
                self._queued.discard(event_id)
            event = store.get_event(event_id)
            if event is not None and event.get("delivered_at") is not None:
                event = None
            if event is None:
                continue
            output.append(event)
        return output

    def requeue(self, event_id: str) -> None:
        """消费方处理失败时放回内存队列；SQLite 事件仍保持未投递。"""
        self.publish(event_id)

    def acknowledge(self, event_id: str) -> bool:
        return store.mark_event_delivered(event_id)


__all__ = ["BackgroundTaskEventBus"]
