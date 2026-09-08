"""主 Agent 使用的图片生成工具。

工具只暴露通用生成参数；ComfyUI 工作流、节点和模型选择留在服务配置中。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from langchain_core.tools import tool

from core.image_generation_service import ImageGenerationService


_SERVICE: ContextVar[ImageGenerationService | None] = ContextVar("image_generation_service", default=None)


@contextmanager
def image_generation_context(service: ImageGenerationService) -> Iterator[None]:
    token = _SERVICE.set(service)
    try:
        yield
    finally:
        _SERVICE.reset(token)


@tool
def generate_visual(
    prompt: str,
    negative_prompt: str = "",
    width: int | None = None,
    height: int | None = None,
    seed: int | None = None,
) -> str:
    """根据文字描述生成一张图片，并返回真实任务状态和图片引用。

    只填写画面描述及可选尺寸/种子；工具会使用固定的本地 ComfyUI 工作流。
    返回 ``succeeded`` 才表示图片文件真实生成。生成图片和再次观察图片是两个
    独立动作，需要理解生成结果时再调用视觉观察工具。
    """
    service = _SERVICE.get()
    if service is None:
        return json.dumps({"operation": "generate_visual", "status": "unavailable", "reason": "当前没有图片生成服务上下文"}, ensure_ascii=False)
    result = service.generate(prompt=prompt, negative_prompt=negative_prompt, width=width, height=height, seed=seed)
    return json.dumps({"operation": "generate_visual", **result}, ensure_ascii=False)


__all__ = ["generate_visual", "image_generation_context"]
