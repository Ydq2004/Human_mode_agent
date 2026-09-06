"""主 Agent 使用的只读图片观察工具。

工具接受 Agent 找到的本地图片路径。框架先执行读取权限、文件格式和
SHA-256 复核，再根据当前模型能力选择两条通道：

* ``native``：返回一个不含图片二进制的临时引用，由模型中间件只在下一次
  主模型请求中展开为图片块；
* ``tool``：交给独立视觉模型，返回结构化观察。

图片内容不会进入工具结果字符串或 checkpoint。
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from uuid import uuid4

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from typing import Annotated

from config import VISION_MAX_CALLS_PER_TURN
from core.vision_input import image_content_block, inspect_image
from core.vision_service import VisionService
from tools.file_access_policy import check_path


@dataclass
class _Runtime:
    service: VisionService | None
    event: dict
    native_vision: bool = False
    count: int = 0
    pending_native: dict[str, dict] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)

    def _metadata_for_path(self, path: str) -> tuple[dict | None, dict | None]:
        requested = str(path or "").strip()

        # 兼容用户直接提交图片后模型主动再次观察的旧调用；新的路径型调用
        # 应始终显式传入 path，尤其是由 list_directory 返回的 entry.path。
        if not requested:
            if self.event.get("modality") != "image":
                return None, {
                    "status": "invalid_request",
                    "reason": "path 不能为空；请传入图片文件的本地绝对路径",
                }
            metadata = dict(self.event.get("metadata") or {})
            requested = str(metadata.get("path") or "").strip()
            if not requested:
                return None, {
                    "status": "invalid_request",
                    "reason": "当前图片事件没有可用的本地路径",
                }
            try:
                # 直接由用户提交的当前事件已经经过 create_image_event 校验；
                # 这里仍复核一次，防止文件在模型调用前被替换。
                current = inspect_image(requested)
                if current.get("sha256") != metadata.get("sha256"):
                    raise ValueError("图片已被替换，请重新提交")
                return current, None
            except (OSError, ValueError) as exc:
                return None, {"status": "failed", "reason": str(exc)}

        # Agent 找到的路径必须遵守与 read_file/list_directory 相同的读取策略。
        check = check_path(requested, "read")
        if check.status != "allow":
            return None, {
                "status": check.status,
                "path": requested,
                "reason": check.reason,
            }

        try:
            metadata = inspect_image(check.resolved_path)
        except (OSError, ValueError) as exc:
            return None, {
                "status": "invalid_request",
                "path": requested,
                "reason": str(exc),
            }
        return metadata, None

    def describe(self, path: str, focus: str) -> dict:
        if len(path) > 4096:
            return {"status": "invalid_request", "reason": "图片路径不能超过 4096 字符"}
        if len(focus) > 1000:
            return {"status": "invalid_request", "reason": "观察重点不能超过 1000 字"}

        with self.lock:
            if self.count >= VISION_MAX_CALLS_PER_TURN:
                return {"status": "call_limit_reached", "limit": VISION_MAX_CALLS_PER_TURN}
            self.count += 1

        metadata, error = self._metadata_for_path(path)
        if error:
            return error

        assert metadata is not None
        common = {
            "path": metadata["path"],
            "mime_type": metadata["mime_type"],
            "size_bytes": metadata["size_bytes"],
            "sha256": metadata["sha256"],
            "attachment_id": metadata["attachment_id"],
            "focus": focus,
            "source_event_id": self.event.get("event_id"),
        }

        if self.native_vision:
            request_id = f"vision_request_{uuid4().hex}"
            with self.lock:
                self.pending_native[request_id] = metadata
            return {
                "status": "attached",
                "mode": "native",
                "request_id": request_id,
                **common,
            }

        if self.service is None:
            return {
                "status": "unavailable",
                "mode": "tool",
                "reason": "当前没有配置独立视觉模型",
                **common,
            }

        result = self.service.describe(metadata, focus)
        result.update(common)
        result["mode"] = "tool"
        return result

    def take_native_attachment(self, request_id: str) -> dict | None:
        with self.lock:
            return self.pending_native.pop(str(request_id), None)


_RUNTIME: ContextVar[_Runtime | None] = ContextVar("vision_runtime", default=None)


def current_vision_event_id() -> str | None:
    """返回当前 Agent 调用绑定的事件 ID，供模型临时渲染使用。"""
    runtime = _RUNTIME.get()
    if runtime is None:
        return None
    return str(runtime.event.get("event_id") or "") or None


@contextmanager
def vision_tool_context(
    service: VisionService | None,
    event: dict,
    *,
    native_vision: bool = False,
) -> Iterator[None]:
    """把当前实例的视觉能力绑定到一次 Agent 调用，结束后立即清理。"""
    token = _RUNTIME.set(
        _Runtime(
            service=service,
            event=deepcopy(event),
            native_vision=bool(native_vision),
        )
    )
    try:
        yield
    finally:
        _RUNTIME.reset(token)


def render_native_vision_for_model(messages: Sequence) -> list:
    """只为本次主模型请求展开 native 视觉工具返回的图片引用。

    ToolMessage 本身仍只保存 JSON 引用；展开后的图片块位于
    ``request.override(messages=...)`` 的临时副本中，因此不会写入 checkpoint。
    每个引用只消费一次，后续轮次不会自动重复发送旧图片。
    """
    runtime = _RUNTIME.get()
    if runtime is None or not runtime.native_vision:
        return list(messages)

    rendered = []
    for message in messages:
        if getattr(message, "type", "") != "tool" or getattr(message, "name", "") != "vision_describe_image":
            rendered.append(message)
            continue
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            rendered.append(message)
            continue
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            rendered.append(message)
            continue
        if not isinstance(payload, dict) or payload.get("status") != "attached":
            rendered.append(message)
            continue
        if (
            payload.get("source_event_id")
            and payload.get("source_event_id") != current_vision_event_id()
        ):
            rendered.append(message)
            continue

        request_id = str(payload.get("request_id") or "")
        metadata = runtime.take_native_attachment(request_id)
        if not metadata:
            rendered.append(message)
            continue

        try:
            image_block = image_content_block(metadata)
        except (OSError, ValueError) as exc:
            failed = {
                "operation": "vision_describe_image",
                "status": "failed",
                "mode": "native",
                "path": metadata.get("path"),
                "reason": str(exc),
            }
            rendered.append(message.model_copy(update={"content": json.dumps(failed, ensure_ascii=False)}))
            continue

        focus = str(payload.get("focus") or "").strip()
        text = "视觉工具已把图片临时交给主模型。请直接观察可见内容；"
        text += "只报告看见的内容和不确定性，不要从图片推断身份、关系、同意、人格或隐藏情绪。"
        if focus:
            text += f" 观察重点：{focus}"
        # OpenAI-compatible接口通常只允许 user 消息携带 image_url；保留原工具
        # 结果作为文本协议，再追加一个仅存在于本次请求副本中的 HumanMessage。
        # 这样不会破坏 AI tool_call -> ToolMessage 配对，也不会把图片写回 checkpoint。
        rendered.append(
            message.model_copy(
                update={
                    "content": json.dumps({
                        "operation": "vision_describe_image",
                        "status": "attached",
                        "mode": "native",
                        "path": metadata.get("path"),
                        "focus": focus,
                        "request_id": request_id,
                    }, ensure_ascii=False)
                }
            )
        )
        rendered.append(
            HumanMessage(
                content=[
                    {"type": "text", "text": text},
                    image_block,
                ],
                additional_kwargs={
                    "vision_tool_attachment": {
                        "request_id": request_id,
                        "source_event_id": payload.get("source_event_id"),
                    }
                },
            )
        )
    return rendered


def _message(tool_call_id: str, payload: dict, *, artifact: dict | None = None) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        name="vision_describe_image",
        tool_call_id=tool_call_id,
        artifact=artifact if artifact is not None else payload,
    )


@tool
def vision_describe_image(
    path: str = "",
    focus: str = "",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> ToolMessage:
    """观察一张本地图片，返回可见内容、图片文字和不确定性。

    ``path`` 是要观察的图片文件本地绝对路径。它通常来自
    ``list_directory`` 返回的 entry.path，或者来自用户明确提供的路径；
    如果用户只给了文件夹，先调用 ``list_directory``，不要用 ``read_file``
    读取图片二进制。用户直接以 ``/image`` 提交当前图片时，path 也可以留空，
    此时工具复用当前事件附件。框架会检查读取白名单、普通文件、图片格式、大小和
    SHA-256，路径越界或敏感文件会被拒绝。

    ``focus`` 是可选的观察重点，例如“读取图片中的文字”“描述人物衣着和
    姿势”或“观察背景环境”。留空表示一般观察。它只能限定观察范围，不能
    要求工具猜测身份、关系、同意、人格或不可直接看见的情绪。工具只读图片，
    不修改文件、记忆或角色状态。原生视觉模式会把图片临时交给主模型；
    独立视觉模式会直接返回 observations、visible_text 和 uncertainties。
    """
    runtime = _RUNTIME.get()
    if runtime is None:
        return _message(tool_call_id, {
            "operation": "vision_describe_image",
            "status": "unavailable",
            "reason": "当前没有图片服务上下文",
        })
    result = runtime.describe(str(path or "").strip(), str(focus or "").strip())
    public = {
        "operation": "vision_describe_image",
        **result,
    }
    return _message(tool_call_id, public, artifact=public)


__all__ = [
    "vision_describe_image",
    "vision_tool_context",
    "render_native_vision_for_model",
    "current_vision_event_id",
]
