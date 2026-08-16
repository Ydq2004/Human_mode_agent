"""Step 5 的单 worker 后台经验评价队列。"""

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from time import perf_counter
from typing import Any, Callable

from config import APPRAISAL_MAX_IN_FLIGHT
from core.experience import ExperienceSlice
from core.experience_appraisal import (
    ExperienceAppraisal,
    appraise_experience,
    compute_appraisal_effects,
)


PENDING = "pending"
COMPLETED = "completed"
FAILED = "failed"
QUEUE_FULL_ERROR = (
    "AppraisalQueueFull: 后台评价队列已达到容量上限，"
    "本轮没有生成评价结果。"
)


@dataclass
class _JobRecord:
    job_id: str
    event_id: str
    experience_slice_id: str
    # 事件序号和线程 id 在提交 appraisal 时冻结，后台完成时不能再从“当前状态”推测。
    event_sequence: int | None
    thread_id: str | None
    # 保存原始感知、动作和观察的最小证据，供后续内容修订判断使用。
    event_evidence: dict[str, Any]
    status: str
    submitted_at: str
    completed_at: str | None = None
    appraisal: ExperienceAppraisal | None = None
    effects: dict[str, Any] | None = None
    timings: dict[str, float] | None = None
    error: str | None = None
    delivered: bool = False


class AppraisalWorker:
    """
    按提交顺序执行 Step 5 appraisal。

    当前 worker 只计算候选，不写 mood 或长期认知。任务状态和结果保留到
    消费者 acknowledge；pending 不能被伪装成保守 fallback。
    """

    def __init__(
        self,
        llm,
        *,
        appraise_fn: Callable[..., ExperienceAppraisal] = appraise_experience,
        effects_fn: Callable[..., dict[str, Any]] = compute_appraisal_effects,
        on_terminal: Callable[[dict[str, Any]], None] | None = None,
        max_in_flight: int = APPRAISAL_MAX_IN_FLIGHT,
    ) -> None:
        if (
            isinstance(max_in_flight, bool)
            or not isinstance(max_in_flight, int)
            or max_in_flight < 1
        ):
            raise ValueError("max_in_flight 必须是大于等于 1 的整数")

        self._llm = llm
        self._appraise_fn = appraise_fn
        self._effects_fn = effects_fn
        # 回调只负责把终态快照投递给下游，不允许在本 worker 中写库。
        self._on_terminal = on_terminal
        self._max_in_flight = int(max_in_flight)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="experience-appraisal",
        )
        self._lock = Lock()
        self._jobs: dict[str, _JobRecord] = {}
        self._futures: dict[str, Future] = {}
        self._closed = False

    def submit(
        self,
        *,
        experience: ExperienceSlice,
        persona_context: dict[str, Any],
        mood_reactivity: float,
        event_sequence: int | None = None,
        thread_id: str | None = None,
    ) -> str:
        """提交冻结的 ExperienceSlice，并立即返回稳定 job id。"""
        job_id = f"appraisal_{experience.slice_id}"
        rejected_snapshot = None

        with self._lock:
            if job_id in self._jobs:
                return job_id

            if self._closed:
                raise RuntimeError("AppraisalWorker 已关闭，不能再提交任务")

            pending_count = sum(
                record.status == PENDING
                for record in self._jobs.values()
            )

            if pending_count >= self._max_in_flight:
                now = datetime.now().isoformat()
                self._jobs[job_id] = _JobRecord(
                    job_id=job_id,
                    event_id=experience.perception_event.event_id,
                    experience_slice_id=experience.slice_id,
                    event_sequence=event_sequence,
                    thread_id=thread_id,
                    event_evidence={
                        "perception_event": experience.perception_event.to_dict(),
                        "response_or_actions": [
                            action.to_dict()
                            for action in experience.response_or_actions
                        ],
                        "observations": [
                            item.to_dict()
                            for item in experience.observations
                        ],
                    },
                    status=FAILED,
                    submitted_at=now,
                    completed_at=now,
                    error=QUEUE_FULL_ERROR,
                )
                terminal_snapshot = self._snapshot_record(self._jobs[job_id])
                # 容量失败也必须占据提交序号，否则后续事件会永远等待缺口。
                rejected_snapshot = terminal_snapshot
            else:
                self._jobs[job_id] = _JobRecord(
                    job_id=job_id,
                    event_id=experience.perception_event.event_id,
                    experience_slice_id=experience.slice_id,
                    event_sequence=event_sequence,
                    thread_id=thread_id,
                    event_evidence={
                        "perception_event": experience.perception_event.to_dict(),
                        "response_or_actions": [
                            action.to_dict()
                            for action in experience.response_or_actions
                        ],
                        "observations": [
                            item.to_dict()
                            for item in experience.observations
                        ],
                    },
                    status=PENDING,
                    submitted_at=datetime.now().isoformat(),
                )

                self._futures[job_id] = self._executor.submit(
                    self._run_job,
                    job_id,
                    experience,
                    deepcopy(persona_context),
                    float(mood_reactivity),
                )

        # 外部回调不能在 AppraisalWorker 自己的锁内执行：回调可能再次访问本 worker，
        # 锁内调用会造成等待甚至死锁。先释放锁，再把终态快照交给 CommitWorker。
        if self._on_terminal is not None and rejected_snapshot is not None:
            self._on_terminal(rejected_snapshot)

        return job_id

    def _run_job(
        self,
        job_id: str,
        experience: ExperienceSlice,
        persona_context: dict[str, Any],
        mood_reactivity: float,
    ) -> None:
        terminal_snapshot = None
        try:
            started = perf_counter()
            appraisal = self._appraise_fn(
                experience=experience,
                persona_context=persona_context,
                llm=self._llm,
            )
            appraisal_seconds = perf_counter() - started

            started = perf_counter()
            effects = self._effects_fn(#计算评估里具体数值
                experience=experience,
                appraisal=appraisal,
                mood_reactivity=mood_reactivity,
            )
            rules_seconds = perf_counter() - started

            with self._lock:
                record = self._jobs[job_id]
                record.status = COMPLETED
                record.completed_at = datetime.now().isoformat()
                record.appraisal = appraisal
                record.effects = deepcopy(effects)
                record.timings = {
                    "appraisal_seconds": appraisal_seconds,
                    "rules_seconds": rules_seconds,
                }
                terminal_snapshot = self._snapshot_record(record)
        except Exception as exc:
            with self._lock:
                record = self._jobs[job_id]
                record.status = FAILED
                record.completed_at = datetime.now().isoformat()
                record.error = f"{type(exc).__name__}: {exc}"
                terminal_snapshot = self._snapshot_record(record)

        # terminal_snapshot 是独立快照，不把可变的内部 JobRecord 直接交给别的线程。
        if self._on_terminal is not None and terminal_snapshot is not None:
            try:
                self._on_terminal(terminal_snapshot)
            except Exception as exc:
                # 投递失败不能伪装成 appraisal 失败。结果仍保留，主线程可以
                # 通过 drain_finished 观察并按相同 job_id 兜底重投。
                with self._lock:
                    record = self._jobs[job_id]
                    record.error = (
                        f"CommitHandoffFailed: {type(exc).__name__}: {exc}"
                    )

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        """读取任务当前真值；不等待，不改变任务状态。"""
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            return self._snapshot_record(record)

    def wait(
        self,
        job_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """仅供测试、关闭流程或显式同步点等待任务。"""
        with self._lock:
            future = self._futures.get(job_id)
            record = self._jobs.get(job_id)

        if future is None:
            if record is None:
                return None
            return self._snapshot_record(record)

        try:
            future.result(timeout=timeout)
        except CancelledError:
            # shutdown(cancel_futures=True) 已把取消写成显式 failed 真值。
            pass
        return self.snapshot(job_id)

    def acknowledge(self, job_id: str) -> bool:
        """
        确认消费者已经处理终态结果，并释放进程内引用。

        当前 CLI 在打印后确认；Step 6 接入后必须在有序写入成功后确认。
        pending 任务不能提前删除。
        """
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.status == PENDING:
                return False

            future = self._futures.get(job_id)
            if future is not None and not future.done():
                return False

            self._jobs.pop(job_id, None)
            self._futures.pop(job_id, None)
            return True

    def release_delivery(self, job_id: str) -> bool:
        """消费者处理失败时，允许终态结果在下一次 drain 中重新交付。"""
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.status == PENDING:
                return False
            record.delivered = False
            return True

    def stats(self) -> dict[str, int | bool]:
        """返回轻量生命周期统计，不暴露评价正文。"""
        with self._lock:
            pending = sum(
                record.status == PENDING
                for record in self._jobs.values()
            )
            return {
                "pending": pending,
                "retained": len(self._jobs),
                "futures": len(self._futures),
                "max_in_flight": self._max_in_flight,
                "closed": self._closed,
            }

    def drain_finished(self) -> list[dict[str, Any]]:
        """交付尚未被消费者领取的 completed/failed 结果。"""
        snapshots = []

        with self._lock:
            for record in self._jobs.values():
                if record.status == PENDING or record.delivered:
                    continue
                record.delivered = True
                snapshots.append(self._snapshot_record(record))

        return snapshots

    def shutdown(
        self,
        *,
        wait: bool = True,
        cancel_futures: bool = False,
    ) -> None:
        """停止接收新任务；可选取消尚未开始的排队任务。"""
        with self._lock:
            self._closed = True

        self._executor.shutdown(
            wait=wait,
            cancel_futures=cancel_futures,
        )

        if not cancel_futures:
            return

        cancelled_snapshots = []
        with self._lock:
            now = datetime.now().isoformat()
            for job_id, future in self._futures.items():
                record = self._jobs.get(job_id)
                if (
                    future.cancelled()
                    and record is not None
                    and record.status == PENDING
                ):
                    record.status = FAILED
                    record.completed_at = now
                    record.error = (
                        "AppraisalCancelled: worker 关闭时任务尚未开始。"
                    )
                    cancelled_snapshots.append(
                        self._snapshot_record(record)
                    )

        # 被取消的事件仍要作为显式失败交给提交水位，不能制造序号缺口。
        if self._on_terminal is not None:
            for snapshot in cancelled_snapshots:
                self._on_terminal(snapshot)

    @staticmethod
    def _snapshot_record(record: _JobRecord) -> dict[str, Any]:
        return {
            "job_id": record.job_id,
            "event_id": record.event_id,
            "experience_slice_id": record.experience_slice_id,
            "event_sequence": record.event_sequence,
            "thread_id": record.thread_id,
            "event_evidence": deepcopy(record.event_evidence),
            "status": record.status,
            "submitted_at": record.submitted_at,
            "completed_at": record.completed_at,
            "appraisal": record.appraisal,
            "effects": deepcopy(record.effects),
            "timings": deepcopy(record.timings),
            "error": record.error,
        }
