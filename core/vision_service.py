"""视觉观察服务。视觉模型只提供观察，不替其他模块做事实裁决。"""
from __future__ import annotations

from typing import Any, Callable, Mapping
import json
from core.vision_input import image_content_block
from langchain_core.messages import HumanMessage


class VisionService:
    def __init__(self, observer: Callable[[str, str], dict[str, Any]] | None = None):
        self.observer = observer

    def describe(self, attachment_path: str | Mapping[str, Any], focus: str = "") -> dict[str, Any]:
        if self.observer is None:
            return {"status": "unavailable", "observations": [], "uncertainties": ["当前没有配置视觉模型"]}
        try:
            raw = self.observer(attachment_path, focus)
            if not isinstance(raw, dict):
                raise ValueError("视觉模型返回格式错误")
            return {
                "status": "success",
                "observations": [str(x) for x in raw.get("observations", []) if str(x).strip()][:12],
                "visible_text": [str(x) for x in raw.get("visible_text", []) if str(x).strip()][:12],
                "uncertainties": [str(x) for x in raw.get("uncertainties", []) if str(x).strip()][:12],
            }
        except Exception as exc:
            return {"status": "failed", "observations": [], "uncertainties": [str(exc)]}


def llm_image_observer(llm):
    """把独立视觉 LLM 适配成服务需要的观察函数。"""
    def observe(attachment: str | Mapping[str, Any], focus: str) -> dict[str, Any]:
        metadata = dict(attachment) if isinstance(attachment, Mapping) else {"path": str(attachment)}
        prompt = (
            "Describe the image in Chinese. Return three sections: "
            "observations, visible_text, uncertainties. "
            "Only report visible content; do not infer identity, relationship, "
            "consent, personality, or hidden emotions."
        )
        if focus:
            prompt += f"\n观察重点：{focus}"
        # ChatOpenAI 接受“消息列表”；图片块必须放在 HumanMessage.content 内，
        # 不能把 type/image_url 字典误当成顶层消息。
        response = llm.invoke([HumanMessage(content=[
            {"type": "text", "text": prompt},
            image_content_block(metadata),
        ])])
        content = response.content if hasattr(response, "content") else response
        if isinstance(content, list):
            content = "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
        text = str(content).strip()
        if text.startswith("```"):
            text = text.strip("`").replace("json", "", 1).strip()
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        # OpenAI 兼容中转站可能忽略 JSON 要求，普通描述仍是有效的视觉观察。
        return {
            "observations": [text[:1000]],
            "visible_text": [],
            "uncertainties": ["模型未按结构化格式返回，以上为原始观察文本"],
        }
    return observe


__all__ = ["VisionService", "llm_image_observer"]
