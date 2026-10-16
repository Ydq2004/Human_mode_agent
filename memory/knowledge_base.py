"""本地文档知识库。

知识库属于框架层的外部资料来源，与 ``memory_entities``/ExperienceSlice
完全分离。SQLite 保存文档和分块的可追踪元数据，Chroma 只保存可重建的
向量索引。Agent 只能通过只读搜索工具读取带来源的片段，不能把文档内容
自动写入个人长期认知。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pypdf import PdfReader
from langchain_core.documents import Document

from config import (
    KNOWLEDGE_CHUNK_OVERLAP,
    KNOWLEDGE_CHUNK_SIZE,
    KNOWLEDGE_CHROMA_DIR,
    KNOWLEDGE_MAX_PDF_BYTES,
    KNOWLEDGE_COLLECTION_NAME,
    SQLITE_DB_PATH,
)
from memory.vector_store import embeddings
from tools.file_access_policy import check_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_knowledge_store() -> None:
    """创建知识库元数据表；不会扫描文件，也不会调用模型。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                document_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document "
            "ON knowledge_chunks(document_id, page_number, chunk_index)"
        )


def _vector_db():
    # 延迟创建独立 collection，避免没有搜索/导入时触碰知识库目录。
    from langchain_chroma import Chroma

    Path(KNOWLEDGE_CHROMA_DIR).mkdir(parents=True, exist_ok=True)
    return Chroma(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        embedding_function=embeddings,
        persist_directory=str(KNOWLEDGE_CHROMA_DIR),
    )


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _chunk_text(text: str, *, size: int = KNOWLEDGE_CHUNK_SIZE,
                overlap: int = KNOWLEDGE_CHUNK_OVERLAP) -> list[str]:
    text = _clean_text(text)
    if not text:
        return []
    if size < 1 or overlap < 0 or overlap >= size:
        raise ValueError("知识库分块参数无效")
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("。", start + size // 2, end),
                           text.rfind("！", start + size // 2, end),
                           text.rfind("？", start + size // 2, end),
                           text.rfind(".", start + size // 2, end),
                           text.rfind("\n", start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _read_pdf(path: Path) -> tuple[list[tuple[int, str]], int]:
    reader = PdfReader(str(path))
    pages: list[tuple[int, str]] = []
    for page_number, page in enumerate(reader.pages, 1):
        text = _clean_text(page.extract_text() or "")
        if text:
            pages.append((page_number, text))
    return pages, len(reader.pages)


def _document_id(source_path: str, digest: str) -> str:
    return "doc_" + hashlib.sha256(f"{source_path}\0{digest}".encode()).hexdigest()[:24]


def _chunk_id(document_id: str, page: int, index: int, content: str) -> str:
    raw = f"{document_id}\0{page}\0{index}\0{content}".encode("utf-8")
    return "chunk_" + hashlib.sha256(raw).hexdigest()[:28]


def _delete_document(document_id: str) -> None:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM knowledge_chunks WHERE document_id = ?", (document_id,)
        ).fetchall()
        ids = [row["chunk_id"] for row in rows]
        conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM knowledge_documents WHERE document_id = ?", (document_id,))
    if ids:
        _vector_db().delete(ids=ids)


def ingest_pdf(path: str | Path, *, force: bool = False) -> dict[str, Any]:
    """导入一个 PDF；同一路径和同一内容重复导入是幂等的。"""
    requested = str(path or "").strip()
    check = check_path(requested, "read")
    if check.status != "allow":
        return {"status": check.status, "source_path": requested, "error": check.reason}
    source = Path(check.resolved_path)
    if source.suffix.lower() != ".pdf":
        return {"status": "invalid_input", "source_path": str(source), "error": "只支持 PDF 文件"}
    if not source.is_file():
        return {"status": "not_found", "source_path": str(source), "error": "文件不存在"}
    size = source.stat().st_size
    if size > KNOWLEDGE_MAX_PDF_BYTES:
        return {"status": "too_large", "source_path": str(source), "error": f"PDF 超过 {KNOWLEDGE_MAX_PDF_BYTES} 字节限制"}
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    ensure_knowledge_store()
    with _connect() as conn:
        old = conn.execute(
            "SELECT * FROM knowledge_documents WHERE source_path = ? ORDER BY updated_at DESC LIMIT 1",
            (str(source),),
        ).fetchone()
    if old is not None and old["sha256"] == digest and not force and old["status"] == "ready":
        return {"status": "already_indexed", "document_id": old["document_id"], "source_path": str(source), "chunk_count": old["chunk_count"]}
    if old is not None:
        _delete_document(old["document_id"])

    document_id = _document_id(str(source), digest)
    now = _now()
    try:
        pages, page_count = _read_pdf(source)
        chunks: list[dict[str, Any]] = []
        for page_number, text in pages:
            for chunk_index, content in enumerate(_chunk_text(text)):
                chunk_id = _chunk_id(document_id, page_number, chunk_index, content)
                chunks.append({
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "page_number": page_number,
                    "chunk_index": chunk_index,
                    "content": content,
                    "metadata": {
                        "document_id": document_id,
                        "chunk_id": chunk_id,
                        "source_path": str(source),
                        "file_name": source.name,
                        "page_number": page_number,
                        "chunk_index": chunk_index,
                    },
                })
        if not chunks:
            status = "no_text"
            error = "PDF 没有可提取文本；扫描件需要先 OCR"
        else:
            status = "ready"
            error = ""
        with _connect() as conn:
            conn.execute(
                "INSERT INTO knowledge_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, str(source), source.name, digest, page_count,
                 len(chunks), status, error, now, now),
            )
            conn.executemany(
                "INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?, ?, ?)",
                [(item["chunk_id"], document_id, item["page_number"], item["chunk_index"],
                  item["content"], json.dumps(item["metadata"], ensure_ascii=False)) for item in chunks],
            )
        if chunks:
            db = _vector_db()
            db.add_documents(
                [Document(page_content=item["content"], metadata=item["metadata"]) for item in chunks],
                ids=[item["chunk_id"] for item in chunks],
            )
        return {"status": status, "document_id": document_id, "source_path": str(source),
                "page_count": page_count, "chunk_count": len(chunks), "error": error}
    except Exception as exc:
        # 向量写入可能在批处理中途失败；失败文档不能继续参与搜索，
        # 先清理已经落下的分块和向量，再保留可诊断的失败记录。
        try:
            _delete_document(document_id)
        except Exception:
            # 清理失败不覆盖原始导入错误；下次显式 force 导入仍可重建。
            pass
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO knowledge_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, str(source), source.name, digest, 0, 0, "failed",
                 f"{type(exc).__name__}: {exc}", now, _now()),
            )
        return {"status": "failed", "document_id": document_id, "source_path": str(source),
                "error": f"{type(exc).__name__}: {exc}"}


def search_knowledge(query: str, *, limit: int = 5) -> dict[str, Any]:
    """只读检索外部文档，返回带来源的片段；不返回内部向量字段。"""
    query = str(query or "").strip()
    if not query or len(query) > 1000:
        return {"status": "invalid_input", "query": query, "results": [], "error": "query 不能为空且不能超过 1000 字符"}
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        return {"status": "invalid_input", "query": query, "results": [], "error": "limit 必须在 1 到 20 之间"}
    ensure_knowledge_store()
    with _connect() as conn:
        ready = conn.execute("SELECT COUNT(*) AS count FROM knowledge_documents WHERE status = 'ready'").fetchone()["count"]
    if not ready:
        return {"status": "empty", "query": query, "results": [], "source_boundary": "外部文档知识库；不是个人经历"}
    try:
        matches = _vector_db().similarity_search_with_score(query, k=limit)
    except Exception as exc:
        return {"status": "failed", "query": query, "results": [], "error": f"{type(exc).__name__}: {exc}"}
    results = []
    for doc, distance in matches:
        metadata = dict(doc.metadata or {})
        results.append({
            "content": str(doc.page_content),
            "source": metadata.get("file_name") or metadata.get("source_path", ""),
            "source_path": metadata.get("source_path", ""),
            "page": metadata.get("page_number"),
            "document_id": metadata.get("document_id", ""),
            "chunk_id": metadata.get("chunk_id", ""),
            "relevance": round(1.0 / (1.0 + float(distance)), 6),
        })
    return {"status": "found" if results else "no_match", "query": query,
            "results": results, "source_boundary": "外部文档知识库；不是个人经历"}


def list_knowledge_documents() -> list[dict[str, Any]]:
    ensure_knowledge_store()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM knowledge_documents ORDER BY updated_at DESC").fetchall()
    return [dict(row) for row in rows]


__all__ = ["ensure_knowledge_store", "ingest_pdf", "search_knowledge", "list_knowledge_documents"]
