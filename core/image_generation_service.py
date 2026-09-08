"""通用图片生成服务。

服务层把 Agent 的通用请求映射到固定 ComfyUI API 工作流，并只根据 ComfyUI
真实 history 和输出文件返回成功。它不会把工作流节点细节暴露给模型。
"""

from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import (
    COMFYUI_BASE_URL,
    IMAGE_GENERATION_FIELD_MAP,
    IMAGE_GENERATION_MAX_HEIGHT,
    IMAGE_GENERATION_MAX_PROMPT_CHARS,
    IMAGE_GENERATION_MAX_WIDTH,
    IMAGE_GENERATION_OUTPUT_ROOT,
    IMAGE_GENERATION_POLL_INTERVAL_SECONDS,
    IMAGE_GENERATION_TIMEOUT_SECONDS,
    IMAGE_GENERATION_WORKFLOW_PATH,
)
from core.comfyui_client import ComfyUIClient


class ImageGenerationService:
    def __init__(self, client: ComfyUIClient | None = None, *, workflow_path=None, output_root=None, field_map=None) -> None:
        self.workflow_path = Path(workflow_path or IMAGE_GENERATION_WORKFLOW_PATH)
        self.output_root = Path(output_root or IMAGE_GENERATION_OUTPUT_ROOT).resolve(strict=False)
        self.field_map = copy.deepcopy(field_map or IMAGE_GENERATION_FIELD_MAP)
        self.client = client or ComfyUIClient(COMFYUI_BASE_URL, timeout_seconds=IMAGE_GENERATION_TIMEOUT_SECONDS, poll_interval_seconds=IMAGE_GENERATION_POLL_INTERVAL_SECONDS)

    def _load_workflow(self) -> dict[str, Any]:
        if not self.workflow_path.is_file():
            raise FileNotFoundError(f"工作流模板不存在：{self.workflow_path}")
        try:
            value = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"工作流模板无法读取：{exc}") from exc
        if not isinstance(value, dict) or not value:
            raise ValueError("工作流模板必须是非空 JSON 对象")
        return value

    def _set_field(self, workflow: dict[str, Any], field: str, value: Any) -> None:
        mapping = self.field_map.get(field) or {}
        node_id = str(mapping.get("node_id") or "").strip()
        input_name = str(mapping.get("input") or "").strip()
        if not node_id or not input_name:
            if value in (None, ""):
                return
            raise ValueError(f"工作流尚未配置字段映射：{field}")
        node = workflow.get(node_id)
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ValueError(f"工作流字段映射无效：{field} -> {node_id}.{input_name}")
        if input_name not in node["inputs"]:
            raise ValueError(f"工作流中不存在字段：{node_id}.{input_name}")
        node["inputs"][input_name] = value

    def _build_workflow(self, *, prompt: str, negative_prompt: str, width: int | None, height: int | None, seed: int | None) -> dict[str, Any]:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        if len(prompt) > IMAGE_GENERATION_MAX_PROMPT_CHARS:
            raise ValueError(f"prompt 不能超过 {IMAGE_GENERATION_MAX_PROMPT_CHARS} 个字符")
        if len(str(negative_prompt or "")) > IMAGE_GENERATION_MAX_PROMPT_CHARS:
            raise ValueError(f"negative_prompt 不能超过 {IMAGE_GENERATION_MAX_PROMPT_CHARS} 个字符")
        for name, value, maximum in (("width", width, IMAGE_GENERATION_MAX_WIDTH), ("height", height, IMAGE_GENERATION_MAX_HEIGHT)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 64 or value > maximum or value % 8):
                raise ValueError(f"{name} 必须是 64 到 {maximum} 之间的 8 的倍数")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
            raise ValueError("seed 必须是非负整数")
        workflow = self._load_workflow()
        self._set_field(workflow, "positive_prompt", prompt)
        self._set_field(workflow, "negative_prompt", str(negative_prompt or "").strip())
        self._set_field(workflow, "width", width)
        self._set_field(workflow, "height", height)
        self._set_field(workflow, "seed", seed)
        return workflow

    def _output_metadata(self, history: dict[str, Any], prompt_id: str) -> dict[str, Any]:
        outputs = history.get("outputs") if isinstance(history, dict) else None
        if not isinstance(outputs, dict):
            raise FileNotFoundError("ComfyUI history 没有输出图片")
        for node_output in outputs.values():
            images = node_output.get("images") if isinstance(node_output, dict) else None
            if not isinstance(images, list):
                continue
            for image in images:
                if not isinstance(image, dict) or not image.get("filename"):
                    continue
                if str(image.get("type") or "output") != "output":
                    continue
                relative = Path(str(image.get("subfolder") or "")) / str(image["filename"])
                target = (self.output_root / relative).resolve(strict=False)
                if target != self.output_root and self.output_root not in target.parents:
                    raise PermissionError("ComfyUI 输出路径越过配置的输出目录")
                if not target.is_file():
                    continue
                data = target.read_bytes()
                if not data:
                    continue
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return {"path": str(target), "format": target.suffix.lower().lstrip("."), "mime_type": mime, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "prompt_id": prompt_id}
        raise FileNotFoundError("ComfyUI history 中没有可读取的输出文件")

    def generate(self, *, prompt: str, negative_prompt: str = "", width: int | None = None, height: int | None = None, seed: int | None = None, client_id: str | None = None) -> dict[str, Any]:
        job_id = f"image_job_{uuid4().hex}"
        try:
            workflow = self._build_workflow(prompt=prompt, negative_prompt=negative_prompt, width=width, height=height, seed=seed)
            job = self.client.submit(workflow, client_id=str(client_id or job_id))
            history = self.client.wait_for_history(job.prompt_id)
            image = self._output_metadata(history, job.prompt_id)
            return {"status": "succeeded", "job_id": job_id, "prompt_id": job.prompt_id, "image": image, "workflow_path": str(self.workflow_path), "workflow_version": hashlib.sha256(json.dumps(workflow, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]}
        except TimeoutError as exc:
            return {"status": "timeout", "job_id": job_id, "error": str(exc)}
        except FileNotFoundError as exc:
            return {"status": "configuration_missing" if "模板" in str(exc) else "output_missing", "job_id": job_id, "error": str(exc)}
        except Exception as exc:
            return {"status": "failed", "job_id": job_id, "error": type(exc).__name__, "message": str(exc)}


__all__ = ["ImageGenerationService"]
