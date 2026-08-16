import sys
from pathlib import Path
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.retrieval_engine import retrieve_memories
from memory.schema import MemoryEntity


def make_entity(concept_id, subject, obj):
    return MemoryEntity(
        concept_id=concept_id,
        canonical_name=concept_id,
        aliases=[],
        memory_type="knowledge",
        identity_signature={
            "subject": subject,
            "relation": "测试关系",
            "object": obj,
        },
        summary=concept_id,
    )


class RetrievalEngineFilterTests(unittest.TestCase):

    def setUp(self):
        self.entities = [
            make_entity("moonwhite", "主人", "月白稿"),
            make_entity("cola", "主人", "可乐"),
            make_entity("other_owner", "其他用户", "月白稿"),
        ]

    def _retrieve(self, filters):
        # 本组测试只验证结构化路径，不让名称、向量或关系路径混入结果。
        with patch(
            "memory.retrieval_engine.search_by_name_or_alias",
            return_value=[],
        ), patch(
            "memory.retrieval_engine._retrieve_vector_candidates",
            return_value=[],
        ), patch(
            "memory.retrieval_engine._candidate_pool",
            return_value=self.entities,
        ):
            return retrieve_memories(
                "测试 query",
                filters=filters,
                top_k=10,
                include_related=False,
            )

    def test_subject_and_object_must_both_match(self):
        result = self._retrieve({
            "subject": "主人",
            "object": "月白稿",
        })

        self.assertEqual(
            [item.entity.concept_id for item in result.accepted],
            ["moonwhite"],
        )
        self.assertEqual(
            result.accepted[0].retrieval_sources,
            ["filter_subject", "filter_object"],
        )

    def test_single_subject_filter_keeps_all_subject_matches(self):
        result = self._retrieve({"subject": "主人"})

        self.assertEqual(
            {item.entity.concept_id for item in result.accepted},
            {"moonwhite", "cola"},
        )


if __name__ == "__main__":
    unittest.main()
