"""Step 6 的有序提交线程。

AppraisalWorker 只负责评价，CommitWorker 才负责消费已经完成的评价结果。
两者分开是为了保护一个重要边界：LLM 完成的先后顺序不能改变事件发生的
先后顺序。提交函数由上层注入，便于单元测试，也避免这个调度器偷偷承担
记忆语义判断。
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from threading import Condition, Thread
from time import monotonic
from typing import Any, Callable


WAITING = "waiting"
COMMITTING = "committing"
COMMITTED = "committed"
COMMIT_FAILED = "commit_failed"


@dataclass(frozen=True)
class CommitTask:
    """一个已经完成或失败的 appraisal 结果及其事件顺序。"""

    job_id: str
    event_sequence: int
    thread_id: str
    appraisal_job: dict[str, Any]


@dataclass
class _CommitRecord:
    task: CommitTask
    status: str
    submitted_at: str
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    delivered: bool = False


class CommitWorker:
    """单线程、按 event_sequence 提交 Step 6 结果。"""

    def __init__(
        self,
        commit_fn: Callable[[CommitTask], dict[str, Any]],
        *,
        ledger=None,
    ) -> None:
        self._commit_fn = commit_fn
        self._ledger = ledger
        self._condition = Condition()
        self._pending: dict[int, CommitTask] = {}
        self._records: dict[str, _CommitRecord] = {}
        # acknowledge 只释放大对象；这个轻量集合保留到进程结束，确保同一
        # job_id 在整个运行期最多执行一次。
        self._seen_job_ids: set[str] = set()
        self._seen_sequences: set[int] = set()
        self._next_sequence = 1
        self._closed = False
        self._thread = Thread(
            target=self._run,
            name="memory-commit",
            daemon=False,
        )
        self._thread.start()

    def submit(self, task: CommitTask) -> str:
        """提交一个任务；同一 job_id 重复提交时保持进程内幂等。"""
        if not isinstance(task.event_sequence, int) or task.event_sequence < 1:
            raise ValueError("event_sequence 必须是从 1 开始的整数")
        if not task.job_id:
            raise ValueError("job_id 不能为空")

        with self._condition:
            if task.job_id in self._seen_job_ids:
                return task.job_id
            if self._closed:
                raise RuntimeError("CommitWorker 已关闭，不能再提交任务")
            if task.event_sequence in self._pending:
                raise ValueError("event_sequence 不能重复")
            if (
                task.event_sequence < self._next_sequence
                or task.event_sequence in self._seen_sequences
            ):
                raise ValueError("event_sequence 已经提交或越过水位")

            self._seen_job_ids.add(task.job_id)
            self._seen_sequences.add(task.event_sequence)
            self._records[task.job_id] = _CommitRecord(
                task=task,
                status=WAITING,
                submitted_at=datetime.now().isoformat(),
            )
            self._pending[task.event_sequence] = task
            self._condition.notify_all()

        return task.job_id

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        """读取提交状态，不等待也不改变状态。"""
        with self._condition:
            record = self._records.get(job_id)
            return self._snapshot_record(record) if record else None

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        """等待单个任务进入 committed 或 commit_failed。"""
        with self._condition:
            record = self._records.get(job_id)
            if record is None:
                return None

            if record.status in {COMMITTED, COMMIT_FAILED}:
                return self._snapshot_record(record)

            end = None if timeout is None else monotonic() + timeout
            while record.status not in {COMMITTED, COMMIT_FAILED}:
                remaining = None if end is None else end - monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            return self._snapshot_record(record)

    def drain_finished(self) -> list[dict[str, Any]]:
        """交付尚未读取的终态结果。"""
        with self._condition:
            output = []
            for record in self._records.values():
                if record.status in {WAITING, COMMITTING} or record.delivered:
                    continue
                record.delivered = True
                output.append(self._snapshot_record(record))
            return output

    def acknowledge(self, job_id: str) -> bool:
        """提交结果已被上层消费后释放任务和结果引用。"""
        with self._condition:
            record = self._records.get(job_id)
            if record is None or record.status in {WAITING, COMMITTING}:
                return False
            self._records.pop(job_id, None)
            return True

    def release_delivery(self, job_id: str) -> bool:
        """上层处理失败时允许下一轮重新交付。"""
        with self._condition:
            record = self._records.get(job_id)
            if record is None or record.status in {WAITING, COMMITTING}:
                return False
            record.delivered = False
            return True

    def stats(self) -> dict[str, int | bool]:
        with self._condition:
            return {
                "waiting": sum(r.status == WAITING for r in self._records.values()),
                "committing": sum(r.status == COMMITTING for r in self._records.values()),
                "retained": len(self._records),
                "next_sequence": self._next_sequence,
                "closed": self._closed,
            }

    def shutdown(self, *, wait: bool = True) -> None:
        """停止接收任务，并在必要时把无法填补的序号标为失败。"""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

        if wait:
            self._thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._next_sequence not in self._pending:
                    if self._closed:
                        # 关闭时仍缺前序事件，不能无限等待。剩余任务都显式
                        # 失败，调用方可以看到数据缺口，而不是假装已提交。
                        if self._pending:
                            self._fail_missing_sequences_locked()
                        return
                    self._condition.wait()

                task = self._pending.pop(self._next_sequence)
                record = self._records[task.job_id]
                record.status = COMMITTING

            try:
                if self._ledger is not None:
                    self._ledger.mark_commit_started(task.job_id)
                result = self._commit_fn(task)
                if not isinstance(result, dict):
                    result = {"value": result}
                with self._condition:
                    record.status = COMMITTED
                    record.result = deepcopy(result)
                    record.completed_at = datetime.now().isoformat()
                if self._ledger is not None:
                    self._ledger.mark_commit_terminal(
                        task.job_id,
                        status=COMMITTED,
                        result=result,
                    )
            except Exception as exc:
                with self._condition:
                    record.status = COMMIT_FAILED
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.completed_at = datetime.now().isoformat()
                if self._ledger is not None:
                    self._ledger.mark_commit_terminal(
                        task.job_id,
                        status=COMMIT_FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            finally:
                with self._condition:
                    self._next_sequence += 1
                    self._condition.notify_all()

    def _fail_missing_sequences_locked(self) -> None:
        """关闭时防止因丢失一个序号而永远挂起。需在 condition 内调用。"""
        for sequence, task in list(self._pending.items()):
            record = self._records[task.job_id]
            record.status = COMMIT_FAILED
            record.error = (
                f"MissingEventSequence: 等待事件序号 {self._next_sequence} 时关闭"
            )
            record.completed_at = datetime.now().isoformat()
            self._pending.pop(sequence, None)
        self._condition.notify_all()

    @staticmethod
    def _snapshot_record(record: _CommitRecord) -> dict[str, Any]:
        return {
            "job_id": record.task.job_id,
            "event_sequence": record.task.event_sequence,
            "thread_id": record.task.thread_id,
            "status": record.status,
            "submitted_at": record.submitted_at,
            "completed_at": record.completed_at,
            "result": deepcopy(record.result),
            "error": record.error,
        }
