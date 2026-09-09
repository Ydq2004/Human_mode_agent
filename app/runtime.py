"""供终端、网页和桌面壳共用的单实例应用运行时。

该层属于应用编排层：它负责启动/关闭现有服务、串行提交同一线程的事件，
但不重新实现感知理解、记忆提交或图片生成规则。`main.py` 仍然保留为 CLI
适配器，逐步迁移时可以与这里共享同一套业务函数。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from config import (
    BACKGROUND_TASK_STALE_MINUTES,
    DEFAULT_THREAD_ID,
    MAX_RECENT_TURN,
    CARD_SLOT_ROOT,
    COMFYUI_ARGS,
    COMFYUI_BASE_URL,
    COMFYUI_CONFIG_ERROR,
    COMFYUI_ENABLED,
    COMFYUI_EXECUTABLE,
    COMFYUI_HEALTHCHECK_TIMEOUT_SECONDS,
    COMFYUI_LOG_ROOT,
    COMFYUI_POLL_INTERVAL_SECONDS,
    COMFYUI_STARTUP_TIMEOUT_SECONDS,
    COMFYUI_STOP_ON_APP_EXIT,
    COMFYUI_WORKDIR,
)
from core.agent_factory import create_agent_from_persona
from core.appraisal_worker import AppraisalWorker
from core.advanced_model_service import create_advanced_llm, run_code_review
from core.background_task_events import BackgroundTaskEventBus
from core.background_task_worker import BackgroundTaskWorker
from core.commit_worker import CommitWorker
from core.comfyui_process import ComfyUIProcessManager, ComfyUIStartupError
from core.image_generation_service import ImageGenerationService
from core.memory_commit import MemoryCommitService
from core.perception import PerceptionEvent, create_perception_event
from core.somatic_service import SomaticService
from core.vector_repair_worker import VectorRepairWorker
from core.vision_input import create_image_event
from core.vision_service import VisionService, llm_image_observer
from emotion.models import ensure_emotion_store
from main import (
    _build_capability_snapshot,
    _build_persona_context,
    _consume_finished_appraisal_jobs,
    _consume_finished_commit_jobs,
    _prepare_appraisal_recovery,
    _prepare_commit_recovery,
    _record_and_submit_commit,
    _working_context_from_experience,
    process_perception_event,
)
from memory import background_task_store, commit_store
from memory.experience_store import (
    get_next_event_sequence,
    list_experience_slices,
)
from memory.personality_store import ensure_personality_seed_from_persona
from memory.store_manager import (
    ensure_memory_store,
    initialize_genesis_memory,
    inspect_vector_consistency,
    repair_pending_vectors,
)
from memory.summary_scheduler import generate_missing_period_summaries
from memory.somatic_store import ensure_somatic_store
from memory.recall_tools import recall_memory
from tools.background_task_tools import (
    cancel_background_task,
    consult_advanced_model,
    get_background_task_status,
    list_background_tasks,
    start_code_review,
)
from tools.file_tools import apply_patch, list_directory, read_file, write_file
from tools.image_generation_tools import generate_visual
from tools.somatic_tools import somatic_action
from tools.vision_tools import vision_describe_image


class RuntimeClosedError(RuntimeError):
    """运行时已经关闭，不能继续提交事件。"""


class ApplicationRuntime:
    """一个角色槽位、一个线程和一组后台 worker 的生命周期容器。

    同一 `thread_id` 的事件必须在这里串行处理，保护 checkpoint、事件序号和
    当前动态注入。不同槽位不要在同一进程中热替换；槽位切换由上层在关闭后
    启动新进程或新运行时完成。
    """

    def __init__(
        self,
        *,
        thread_id: str = DEFAULT_THREAD_ID,
        file_confirm: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.thread_id = self._validate_thread_id(thread_id)
        self._file_confirm = file_confirm or (lambda _request: False)
        self._event_lock = threading.RLock()
        self._busy = False
        self._closed = False
        self._recent_context: list[str] = []
        self._initialise()

    @staticmethod
    def _validate_thread_id(value: str) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 200:
            raise ValueError("thread_id 必须是 1 到 200 个字符")
        if any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("thread_id 不能包含控制字符")
        return value

    @staticmethod
    def _tools() -> list[Any]:
        return [
            recall_memory,
            read_file,
            list_directory,
            write_file,
            apply_patch,
            somatic_action,
            vision_describe_image,
            consult_advanced_model,
            start_code_review,
            get_background_task_status,
            list_background_tasks,
            cancel_background_task,
            generate_visual,
        ]

    def _initialise(self) -> None:
        ensure_emotion_store()
        ensure_memory_store()
        background_task_store.ensure_background_task_store()
        background_task_store.recover_stale_tasks(
            max_age_minutes=BACKGROUND_TASK_STALE_MINUTES,
        )
        try:
            generate_missing_period_summaries(thread_id=self.thread_id)
        except Exception:
            # 摘要是派生数据，失败不能阻止聊天运行时启动。
            pass

        tools = self._tools()
        factory_result = create_agent_from_persona(
            tools=tools,
            file_confirm=self._file_confirm,
            return_resources=True,
        )
        self._agent_resources: dict[str, Any] = {}
        if len(factory_result) == 8:
            (
                self.agent,
                self.agent_config,
                self.persona,
                self.system_prompt,
                self.understanding_llm,
                self.retry_understanding_llm,
                self.appraisal_llm,
                self.somatic_llm,
            ) = factory_result
            self.vision_llm = None
        elif len(factory_result) == 9:
            (
                self.agent,
                self.agent_config,
                self.persona,
                self.system_prompt,
                self.understanding_llm,
                self.retry_understanding_llm,
                self.appraisal_llm,
                self.somatic_llm,
                self.vision_llm,
            ) = factory_result
        else:
            (
                self.agent,
                self.agent_config,
                self.persona,
                self.system_prompt,
                self.understanding_llm,
                self.retry_understanding_llm,
                self.appraisal_llm,
                self.somatic_llm,
                self.vision_llm,
                self._agent_resources,
            ) = factory_result

        self.agent_config.setdefault("configurable", {})["thread_id"] = self.thread_id
        self.vision_service = (
            VisionService(llm_image_observer(self.vision_llm))
            if self.vision_llm is not None
            else None
        )
        self.advanced_llm = create_advanced_llm()
        self.image_generation_service = ImageGenerationService()
        self.comfyui_manager = ComfyUIProcessManager(
            base_url=COMFYUI_BASE_URL,
            executable=COMFYUI_EXECUTABLE,
            args=COMFYUI_ARGS,
            cwd=COMFYUI_WORKDIR or None,
            log_root=COMFYUI_LOG_ROOT,
            enabled=COMFYUI_ENABLED,
            startup_timeout_seconds=COMFYUI_STARTUP_TIMEOUT_SECONDS,
            healthcheck_timeout_seconds=COMFYUI_HEALTHCHECK_TIMEOUT_SECONDS,
            poll_interval_seconds=COMFYUI_POLL_INTERVAL_SECONDS,
            stop_on_exit=COMFYUI_STOP_ON_APP_EXIT,
            config_error=COMFYUI_CONFIG_ERROR,
        )
        try:
            self.comfyui_manager.start_if_needed()
        except ComfyUIStartupError:
            # 文字/记忆链路不依赖 ComfyUI；生图工具会返回真实失败状态。
            pass

        personality_seed = ensure_personality_seed_from_persona(self.persona)
        self.persona_instance_id = str(personality_seed["persona_instance_id"])

        self.background_event_bus = BackgroundTaskEventBus()
        self.background_event_bus.recover(limit=100)
        self.background_worker = BackgroundTaskWorker(event_bus=self.background_event_bus)
        if self.advanced_llm is not None:
            self.background_worker.register(
                "code_review",
                lambda task: run_code_review(task, self.advanced_llm),
            )
        self.background_worker.submit_due(limit=100)

        ensure_somatic_store()
        embodiment = dict(self.persona.get("embodiment") or {})
        self.somatic_service = SomaticService(
            persona_instance_id=self.persona_instance_id,
            thread_id=self.thread_id,
            llm=self.somatic_llm,
            persona_context=_build_persona_context(self.persona),
            enabled=bool(embodiment.get("enabled", True)),
            allow_agent_initiated_actions=bool(
                embodiment.get("allow_agent_initiated_actions", True)
            ),
        )
        self.capability_snapshot = _build_capability_snapshot(tools, self.persona)
        self.next_event_sequence = get_next_event_sequence(self.thread_id)
        initialize_genesis_memory(self.persona)

        recovered_appraisals, _ = _prepare_appraisal_recovery(thread_id=self.thread_id)
        recovery_tasks, worker_start_sequence, processed_sequences, _ = (
            _prepare_commit_recovery(
                thread_id=self.thread_id,
                next_event_sequence=self.next_event_sequence,
            )
        )
        self.commit_worker = CommitWorker(
            MemoryCommitService(identity_llm=self.appraisal_llm),
            ledger=commit_store,
            start_sequence=worker_start_sequence,
            processed_sequences=processed_sequences,
        )
        self.appraisal_worker = AppraisalWorker(
            self.appraisal_llm,
            on_terminal=lambda job: _record_and_submit_commit(job, self.commit_worker),
        )
        for item in recovered_appraisals:
            row = item["row"]
            self.appraisal_worker.submit(
                experience=item["experience"],
                persona_context=item["persona_context"],
                mood_reactivity=item["mood_reactivity"],
                event_sequence=int(row["event_sequence"]),
                thread_id=self.thread_id,
            )
        for task in recovery_tasks:
            self.commit_worker.submit(task)

        self.vector_repair_worker = VectorRepairWorker(
            inspect_vector_consistency,
            repair_pending_vectors,
        )
        self.event_sequence = self.next_event_sequence - 1

    @property
    def slot_root(self) -> Path:
        return Path(CARD_SLOT_ROOT)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeClosedError("应用运行时已经关闭")

    def submit_event(self, event: PerceptionEvent) -> dict[str, Any]:
        """串行处理一个事件，返回 UI 可消费的结构化结果。"""
        if not isinstance(event, PerceptionEvent):
            raise TypeError("event 必须是 PerceptionEvent")
        with self._event_lock:
            self._ensure_open()
            self._busy = True
            try:
                self.event_sequence += 1
                result = process_perception_event(
                    event,
                    agent=self.agent,
                    agent_config=self.agent_config,
                    understanding_llm=self.understanding_llm,
                    retry_understanding_llm=self.retry_understanding_llm,
                    appraisal_worker=self.appraisal_worker,
                    thread_id=self.thread_id,
                    persona=self.persona,
                    recent_context=self._recent_context,
                    capability_snapshot=self.capability_snapshot,
                    event_sequence=self.event_sequence,
                    somatic_service=self.somatic_service,
                    vision_service=self.vision_service,
                    background_task_worker=self.background_worker,
                    background_task_event_bus=self.background_event_bus,
                    advanced_llm=self.advanced_llm,
                    persona_instance_id=self.persona_instance_id,
                    image_generation_service=self.image_generation_service,
                )
                experience = result["experience_slice"]
                self._recent_context.append(_working_context_from_experience(experience))
                if MAX_RECENT_TURN > 0:
                    del self._recent_context[:-MAX_RECENT_TURN]
                return result
            finally:
                self._busy = False

    def submit_text(self, content: str) -> dict[str, Any]:
        content = str(content or "").strip()
        if not content:
            raise ValueError("消息不能为空")
        return self.submit_event(
            create_perception_event(source="user", modality="text", content=content)
        )

    def submit_image(self, path: str | Path) -> dict[str, Any]:
        return self.submit_event(create_image_event(path))

    def restart_for_thread(self, thread_id: str) -> "ApplicationRuntime":
        """在设置页切换长期线程，返回新的运行时实例。

        线程切换不会删除旧数据，但必须先完整关闭当前 worker 和 checkpoint
        连接，不能让两个线程状态共享同一个正在运行的 Agent 对象。
        """
        new_thread_id = self._validate_thread_id(thread_id)
        with self._event_lock:
            self._ensure_open()
            if new_thread_id == self.thread_id:
                return self
            self.shutdown()
        return ApplicationRuntime(
            thread_id=new_thread_id,
            file_confirm=self._file_confirm,
        )

    def history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """从不可变 ExperienceSlice 生成 UI 历史，不解析终端输出。"""
        rows = list_experience_slices(thread_id=self.thread_id, limit=limit)
        messages: list[dict[str, Any]] = []
        for row in rows:
            experience = row.experience
            event = experience.perception_event
            messages.append({
                "kind": "user",
                "event_id": event.event_id,
                "occurred_at": event.occurred_at,
                "modality": event.modality,
                "content": event.content,
                "metadata": dict(event.metadata),
                "event_sequence": row.event_sequence,
            })
            for action in experience.response_or_actions:
                if action.action_type == "visible_response":
                    agent_message = {
                        "kind": "agent",
                        "event_id": event.event_id,
                        "occurred_at": action.occurred_at,
                        "content": action.content,
                        "event_sequence": row.event_sequence,
                    }
                    media = self._media_from_experience(experience)
                    if media:
                        agent_message["media"] = media
                    messages.append(agent_message)
        return messages

    def _media_from_experience(self, experience) -> list[dict[str, Any]]:
        """从历史工具观察恢复受控图片元数据。"""
        output_root = self.slot_root.joinpath("generated_images").resolve(strict=False)
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for observation in experience.observations:
            if observation.metadata.get("tool_name") != "generate_visual":
                continue
            try:
                payload = json.loads(observation.content)
            except (TypeError, json.JSONDecodeError):
                continue
            image = payload.get("image") if isinstance(payload, dict) else None
            if not isinstance(image, dict) or payload.get("status") != "succeeded":
                continue
            path = Path(str(image.get("path") or "")).resolve(strict=False)
            try:
                path.relative_to(output_root)
            except ValueError:
                continue
            if not path.is_file() or str(path) in seen:
                continue
            seen.add(str(path))
            results.append({
                "kind": "image",
                "filename": path.name,
                "mime_type": str(image.get("mime_type") or "image/png"),
                "size_bytes": int(image.get("size_bytes") or path.stat().st_size),
                "sha256": str(image.get("sha256") or ""),
                "prompt_id": str(payload.get("prompt_id") or ""),
            })
        return results

    def status(self) -> dict[str, Any]:
        return {
            "status": "closed" if self._closed else "ready",
            "slot_root": str(self.slot_root),
            "thread_id": self.thread_id,
            "event_sequence": self.event_sequence,
            "busy": bool(self._busy),
            "persona": self.persona.get("agent_name"),
            "advanced_model": self.advanced_llm is not None,
            "native_vision": bool(
                (self.capability_snapshot.get("vision") or {}).get("native")
            ),
            "comfyui": self.comfyui_manager.status().to_dict(),
        }

    def shutdown(self, *, cancel_pending: bool = False) -> None:
        with self._event_lock:
            if self._closed:
                return
            self._closed = True
            self.appraisal_worker.shutdown(
                wait=True,
                cancel_futures=cancel_pending,
            )
            _consume_finished_appraisal_jobs(
                self.appraisal_worker,
                self.commit_worker,
            )
            self.commit_worker.shutdown(wait=True)
            _consume_finished_commit_jobs(
                self.appraisal_worker,
                self.commit_worker,
            )
            self.vector_repair_worker.shutdown(wait=True)
            self.background_worker.shutdown(wait=True)
            self.comfyui_manager.stop()
            connection = self._agent_resources.get("checkpoint_connection")
            close = getattr(connection, "close", None)
            if callable(close):
                close()


__all__ = ["ApplicationRuntime", "RuntimeClosedError"]
