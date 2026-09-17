"""图片文件元数据读取：只返回文件参数，不解释画面内容。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from langchain_core.tools import tool
from tools.file_access_policy import check_path, max_bytes
from tools.tool_contracts import tool_result


def _png_size(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR": return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _jpeg_size(data: bytes):
    if data[:2] != b"\xff\xd8": return None
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            index += 1; continue
        while index < len(data) and data[index] == 0xFF: index += 1
        if index >= len(data): return None
        marker = data[index]; index += 1
        if marker in {0xD8, 0xD9}: continue
        if index + 2 > len(data): return None
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data): return None
        if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
            if length < 7: return None
            return int.from_bytes(data[index + 3:index + 5], "big"), int.from_bytes(data[index + 5:index + 7], "big")
        index += length
    return None


def _webp_size(data: bytes):
    """读取常见 WEBP VP8X/VP8L/VP8 头部尺寸；未知变体保持不确定。"""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


@tool
def read_image_metadata(path: str) -> str:
    """读取白名单内 PNG/JPEG/GIF/WEBP 的格式、尺寸、大小和哈希。"""
    check = check_path(str(path or ""), "read")
    if check.status != "allow": return tool_result("read_image_metadata", check.status, error=check.reason, path=path)
    target = Path(check.resolved_path)
    try:
        data = target.read_bytes()
        if len(data) > max_bytes("read"): return tool_result("read_image_metadata", "failed", error="文件超过读取大小限制", path=path)
        size = _png_size(data); fmt = "PNG" if size else ""
        if not size and data[:3] == b"\xff\xd8\xff": fmt, size = "JPEG", _jpeg_size(data)
        if not size and data[:6] in {b"GIF87a", b"GIF89a"}: fmt, size = "GIF", (int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
        if not size and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt, size = "WEBP", _webp_size(data)
        if not fmt: return tool_result("read_image_metadata", "failed", error="不支持或损坏的图片格式", path=path)
        return tool_result("read_image_metadata", "success", path=path, result={"format": fmt, "width": size[0] if size else None, "height": size[1] if size else None, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    except OSError as exc:
        return tool_result("read_image_metadata", "failed", error=str(exc), path=path)


__all__ = ["read_image_metadata"]
