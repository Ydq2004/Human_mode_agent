"""Agent 使用的本地文档只读检索工具。"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from memory.knowledge_base import search_knowledge


@tool
def search_knowledge_base(query: str, limit: int = 5) -> str:
    """搜索已导入的本地 PDF 知识库。返回带文件名和页码的原文片段；不会修改知识库，也不是个人经历记忆。"""
    return json.dumps(search_knowledge(query, limit=limit), ensure_ascii=False)


__all__ = ["search_knowledge_base"]
