"""受文件读取白名单约束的文本搜索工具。"""

from __future__ import annotations

from pathlib import Path
import os
from langchain_core.tools import tool
from tools.file_access_policy import check_path, max_bytes
from tools.tool_contracts import tool_result


@tool
def search_text(query: str, path: str = "", glob: str = "", max_results: int = 50) -> str:
    """在允许读取的目录中搜索文本，返回有限的路径、行号和匹配行。"""
    query = str(query or "")
    pattern = str(glob or "*").strip()
    if (not query or len(query) > 500 or isinstance(max_results, bool)
            or not isinstance(max_results, int) or not 1 <= max_results <= 200
            or not pattern or len(pattern) > 200 or os.path.isabs(pattern)
            or any(part == ".." for part in Path(pattern).parts)):
        return tool_result("search_text", "invalid_input", error="query 或 max_results 无效")
    target_text = str(path or ".")
    check = check_path(target_text, "list")
    if check.status != "allow":
        check = check_path(target_text, "read")
    if check.status != "allow":
        return tool_result("search_text", check.status, error=check.reason, path=target_text)
    target = Path(check.resolved_path)
    files = [target] if target.is_file() else sorted(target.rglob(pattern), key=lambda item: str(item).lower()) if target.is_dir() else []
    matches, truncated = [], False
    for item in files:
        if len(matches) >= max_results:
            truncated = True; break
        read_check = check_path(str(item), "read")
        if read_check.status != "allow" or not item.is_file(): continue
        try:
            if item.stat().st_size > max_bytes("read"): continue
            lines = item.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(lines, 1):
            if query in line:
                matches.append({"path": str(item.resolve()), "line": number, "text": line[:1000]})
                if len(matches) >= max_results:
                    truncated = True; break
    return tool_result("search_text", "success", result={"matches": matches, "truncated": truncated, "query": query})


__all__ = ["search_text"]
