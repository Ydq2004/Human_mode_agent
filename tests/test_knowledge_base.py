import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory import knowledge_base
from tools.knowledge_tools import search_knowledge_base


class _FakeVectorDb:
    def __init__(self):
        self.documents = []

    def similarity_search_with_score(self, query, k):
        return [(doc, 0.25) for doc, _ in self.documents[:k]]


class KnowledgeBaseTests(unittest.TestCase):
    def test_chunking_has_overlap_and_is_bounded(self):
        chunks = knowledge_base._chunk_text("甲" * 2000, size=500, overlap=50)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(item) <= 500 for item in chunks))
        self.assertEqual(chunks[0][-50:], chunks[1][:50])

    def test_search_tool_preserves_external_source_boundary(self):
        fake = _FakeVectorDb()
        from langchain_core.documents import Document
        fake.documents.append((Document(
            page_content="安装步骤需要先关闭服务。",
            metadata={"file_name": "manual.pdf", "source_path": "C:/manual.pdf", "page_number": 3,
                      "document_id": "doc-1", "chunk_id": "chunk-1"},
        ), "chunk-1"))
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "knowledge.db")
            with patch.object(knowledge_base, "SQLITE_DB_PATH", db_path), \
                 patch.object(knowledge_base, "_vector_db", return_value=fake):
                knowledge_base.ensure_knowledge_store()
                with knowledge_base._connect() as conn:
                    conn.execute(
                        "INSERT INTO knowledge_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("doc-1", "C:/manual.pdf", "manual.pdf", "hash", 3, 1,
                         "ready", "", "now", "now"),
                    )
                output = search_knowledge_base.invoke({"query": "如何关闭服务", "limit": 1})
        data = json.loads(output)
        self.assertEqual(data["status"], "found")
        self.assertEqual(data["results"][0]["source"], "manual.pdf")
        self.assertEqual(data["results"][0]["page"], 3)
        self.assertIn("不是个人经历", data["source_boundary"])


if __name__ == "__main__":
    unittest.main()
