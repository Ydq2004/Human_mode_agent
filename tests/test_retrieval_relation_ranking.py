import unittest
from unittest.mock import patch

from memory.retrieval_engine import retrieve_memories
from memory.schema import MemoryEntity, MemoryRelation


def _entity(name):
    return MemoryEntity(
        concept_id=name, canonical_name=name, aliases=[], memory_type="knowledge",
        identity_signature={}, summary=name,
    )


class RetrievalRelationRankingTests(unittest.TestCase):
    def test_related_neighbors_use_weight_and_confidence(self):
        root = _entity("root")
        strong = _entity("strong")
        weak = _entity("weak")
        relations = [
            MemoryRelation("r1", "root", "related_to", "strong", weight=0.9, confidence=0.9),
            MemoryRelation("r2", "root", "related_to", "weak", weight=0.8, confidence=0.2),
        ]

        def read(concept_id):
            return {"root": root, "strong": strong, "weak": weak}.get(concept_id)

        with patch("memory.retrieval_engine.search_by_name_or_alias", return_value=[root]), \
             patch("memory.retrieval_engine._retrieve_vector_candidates", return_value=[]), \
             patch("memory.retrieval_engine.list_relations_for_entity", return_value=relations), \
             patch("memory.retrieval_engine.read_entity", side_effect=read):
            result = retrieve_memories("root", top_k=3, include_related=True)

        self.assertEqual([item.entity.concept_id for item in result.accepted], ["root", "strong", "weak"])
        self.assertGreater(result.accepted[1].relation_score, result.accepted[2].relation_score)


if __name__ == "__main__":
    unittest.main()
