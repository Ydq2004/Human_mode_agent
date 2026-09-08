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
    IMAGE_GENERATION_OUTPUT_NODE_ID,
    IMAGE_GENERATION_POLL_INTERVAL_SECONDS,
    IMAGE_GENERATION_TIMEOUT_SECONDS,
    IMAGE_GENERATION_WORKFLOW_PATH,
    IMAGE_GENERATION_UPSCALE_FIELD_MAP,
    IMAGE_GENERATION_UPSCALE_OUTPUT_NODE_ID,
    IMAGE_GENERATION_UPSCALE_WORKFLOW_PATH,
)
from core.comfyui_client import ComfyUIClient


class ImageGenerationService:
    def __init__(self, client: ComfyUIClient | None = None, *, workflow_path=None, output_root=None, field_map=None, upscale_workflow_path=None, upscale_field_map=None, output_node_id=None, upscale_output_node_id=None) -> None:
        self.workflow_path = Path(workflow_path or IMAGE_GENERATION_WORKFLOW_PATH)
        self.upscale_workflow_path = Path(upscale_workflow_path or IMAGE_GENERATION_UPSCALE_WORKFLOW_PATH)
        self.output_root = Path(output_root or IMAGE_GENERATION_OUTPUT_ROOT).resolve(strict=False)
        self.field_map = copy.deepcopy(field_map or IMAGE_GENERATION_FIELD_MAP)
        self.upscale_field_map = copy.deepcopy(upscale_field_map or IMAGE_GENERATION_UPSCALE_FIELD_MAP)
        self.output_node_id = output_node_id if output_node_id is not None else IMAGE_GENERATION_OUTPUT_NODE_ID
        self.upscale_output_node_id = upscale_output_node_id if upscale_output_node_id is not None else IMAGE_GENERATION_UPSCALE_OUTPUT_NODE_ID
        self.client = client or ComfyUIClient(COMFYUI_BASE_URL, timeout_seconds=IMAGE_GENERATION_TIMEOUT_SECONDS, poll_interval_seconds=IMAGE_GENERATION_POLL_INTERVAL_SECONDS)

    def _load_workflow(self, workflow_path: Path | None = None) -> dict[str, Any]:
        workflow_path = workflow_path or self.workflow_path
        if not workflow_path.is_file():
            raise FileNotFoundError(f"工作流模板不存在：{workflow_path}")
        try:
            value = json.loads(workflow_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"工作流模板无法读取：{exc}") from exc
        if not isinstance(value, dict) or not value:
            raise ValueError("工作流模板必须是非空 JSON 对象")
        return value

    def _set_field(self, workflow: dict[str, Any], field: str, value: Any, field_map: dict[str, Any] | None = None) -> None:
        # 未提供的可选参数保留工作流模板中的默认值，尤其不能把 seed 写成 null。
        if value is None:
            return
        raw_mapping = (field_map or self.field_map).get(field) or []
        mappings = raw_mapping if isinstance(raw_mapping, list) else [raw_mapping]
        mappings = [item for item in mappings if isinstance(item, dict)]
        if not mappings:
            if value == "":
                return
            raise ValueError(f"工作流尚未配置字段映射：{field}")
        for mapping in mappings:
            node_id = str(mapping.get("node_id") or "").strip()
            input_name = str(mapping.get("input") or "").strip()
            if not node_id or not input_name:
                if value == "":
                    continue
                raise ValueError(f"工作流尚未配置字段映射：{field}")
            node = workflow.get(node_id)
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise ValueError(f"工作流字段映射无效：{field} -> {node_id}.{input_name}")
            if input_name not in node["inputs"]:
                raise ValueError(f"工作流中不存在字段：{node_id}.{input_name}")
            node["inputs"][input_name] = value

    def _build_workflow(self, *, prompt: str, negative_prompt: str, width: int | None, height: int | None, seed: int | None, upscale: bool = False) -> dict[str, Any]:
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
        workflow_path = self.upscale_workflow_path if upscale else self.workflow_path
        field_map = self.upscale_field_map if upscale else self.field_map
        workflow = self._load_workflow(workflow_path)
        self._set_field(workflow, "positive_prompt", prompt, field_map)
        self._set_field(workflow, "negative_prompt", str(negative_prompt or "").strip(), field_map)
        self._set_field(workflow, "width", width, field_map)
        self._set_field(workflow, "height", height, field_map)
        self._set_field(workflow, "seed", seed, field_map)
        return workflow

    def _output_metadata(self, history: dict[str, Any], prompt_id: str, output_node_id: str | None = None) -> dict[str, Any]:
        outputs = history.get("outputs") if isinstance(history, dict) else None
        if not isinstance(outputs, dict):
            raise FileNotFoundError("ComfyUI history 没有输出图片")
        node_outputs = [outputs.get(output_node_id)] if output_node_id and output_node_id in outputs else outputs.values()
        for node_output in node_outputs:
            images = node_output.get("images") if isinstance(node_output, dict) else None
            if not isinstance(images, list):
                continue
            for image in images:
                if not isinstance(image, dict) or not image.get("filename"):
                    continue
                if str(image.get("type") or "output") != "output":
                    continue
                filename = Path(str(image["filename"])).name
                if filename != str(image["filename"]):
                    raise PermissionError("ComfyUI 输出文件名包含非法路径")
                data = self.client.get_output_bytes(
                    filename=filename,
                    subfolder=str(image.get("subfolder") or ""),
                    output_type=str(image.get("type") or "output"),
                )
                if not data:
                    continue
                self.output_root.mkdir(parents=True, exist_ok=True)
                target = (self.output_root / f"{prompt_id}_{filename}").resolve(strict=False)
                if self.output_root not in target.parents:
                    raise PermissionError("生成结果路径越过配置的输出目录")
                target.write_bytes(data)
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return {"path": str(target), "format": target.suffix.lower().lstrip("."), "mime_type": mime, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "prompt_id": prompt_id}
        raise FileNotFoundError("ComfyUI history 中没有可读取的输出文件")

    def generate(self, *, prompt: str, negative_prompt: str = "", width: int | None = None, height: int | None = None, seed: int | None = None, upscale: bool = False, client_id: str | None = None) -> dict[str, Any]:
        job_id = f"image_job_{uuid4().hex}"
        try:
            workflow = self._build_workflow(prompt=prompt, negative_prompt=negative_prompt, width=width, height=height, seed=seed, upscale=upscale)
            job = self.client.submit(workflow, client_id=str(client_id or job_id))
            history = self.client.wait_for_history(job.prompt_id)
            image = self._output_metadata(history, job.prompt_id, self.upscale_output_node_id if upscale else self.output_node_id)
            selected_workflow_path = self.upscale_workflow_path if upscale else self.workflow_path
            return {"status": "succeeded", "job_id": job_id, "prompt_id": job.prompt_id, "image": image, "workflow_path": str(selected_workflow_path), "upscale": bool(upscale), "workflow_version": hashlib.sha256(json.dumps(workflow, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]}
        except TimeoutError as exc:
            return {"status": "timeout", "job_id": job_id, "error": str(exc)}
        except FileNotFoundError as exc:
            return {"status": "configuration_missing" if "模板" in str(exc) else "output_missing", "job_id": job_id, "error": str(exc)}
        except Exception as exc:
            return {"status": "failed", "job_id": job_id, "error": type(exc).__name__, "message": str(exc)}


__all__ = ["ImageGenerationService"]
