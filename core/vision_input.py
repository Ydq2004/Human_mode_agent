"""图片感知输入适配（框架层）。只保存可追踪的引用，不保存图片二进制。"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

DEFAULT_MAX_IMAGE_BYTES = 15 * 1024 * 1024
IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"RIFF": "image/webp",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


def inspect_image(path: str | Path, *, max_bytes: int = DEFAULT_MAX_IMAGE_BYTES) -> dict[str, Any]:
    """校验图片并返回元数据；失败时抛出 ValueError。"""
    candidate = Path(str(path).strip().strip('"')).expanduser().resolve()
    if not candidate.is_file():
        raise ValueError("图片文件不存在或不是普通文件")
    with candidate.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise ValueError("图片为空或超过读取上限")
    mime_type = next((mime for signature, mime in IMAGE_SIGNATURES.items() if data.startswith(signature)), None)
    if mime_type is None:
        raise ValueError("图片格式不支持或文件已损坏")
    if mime_type == "image/gif" and b"NETSCAPE2.0" in data[:4096]:
        raise ValueError("当前只支持静态图片")
    digest = hashlib.sha256()
    digest.update(data)
    return {
        "path": str(candidate.resolve()),
        "mime_type": mime_type,
        "size_bytes": len(data),
        "sha256": digest.hexdigest(),
        "attachment_id": f"img_{digest.hexdigest()[:24]}",
    }


def image_content_block(metadata: dict[str, Any]) -> dict:
    current = inspect_image(str(metadata.get("path") or ""))
    if current.get("sha256") != metadata.get("sha256"):
        raise ValueError("图片已被替换，请重新提交")
    # 再次读取仍遵守同一大小上限，避免文件在校验后被替换/膨胀时
    # 用无界 read_bytes 把任意大文件送进模型请求。
    with Path(current["path"]).open("rb") as stream:
        data = stream.read(DEFAULT_MAX_IMAGE_BYTES + 1)
    if len(data) > DEFAULT_MAX_IMAGE_BYTES:
        raise ValueError("图片超过读取上限")
    digest = hashlib.sha256(data).hexdigest()
    if digest != metadata.get("sha256"):
        raise ValueError("图片已被替换，请重新提交")
    return {"type": "image_url", "image_url": {"url": f"data:{current['mime_type']};base64,{base64.b64encode(data).decode('ascii')}"}}


def create_image_event(path: str | Path, *, occurred_at: str | None = None):
    from core.perception import create_perception_event
    metadata = inspect_image(path)
    return create_perception_event(
        source="user", modality="image", content="用户提交了一张图片。",
        metadata=metadata, occurred_at=occurred_at,
    )
