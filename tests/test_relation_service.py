import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.relation_service import build_relation_for_candidate


class RelationServiceTests(unittest.TestCase):

    def test_directional_relation_uses_candidate_as_source(self):
        relation = build_relation_for_candidate(
            "candidate", "existing", "belongs_to", "candidate_to_existing"
        )
        self.assertEqual(relation.source_concept_id, "candidate")
        self.assertEqual(relation.target_concept_id, "existing")

    def test_reverse_direction_uses_existing_as_source(self):
        relation = build_relation_for_candidate(
            "candidate", "existing", "refers_to", "existing_to_candidate"
        )
        self.assertEqual(relation.source_concept_id, "existing")
        self.assertEqual(relation.target_concept_id, "candidate")

    def test_symmetric_relation_is_order_independent(self):
        left = build_relation_for_candidate(
            "candidate", "existing", "related_to", "symmetric"
        )
        right = build_relation_for_candidate(
            "existing", "candidate", "related_to", "symmetric"
        )
        self.assertEqual(left.relation_id, right.relation_id)
        self.assertEqual(
            (left.source_concept_id, left.target_concept_id),
            ("candidate", "existing"),
        )

    def test_similar_to_is_always_symmetric(self):
        relation = build_relation_for_candidate(
            "z", "a", "similar_to", "candidate_to_existing"
        )
        self.assertEqual(relation.source_concept_id, "a")
        self.assertEqual(relation.target_concept_id, "z")

    def test_directional_relation_without_direction_degrades(self):
        relation = build_relation_for_candidate(
            "z", "a", "belongs_to", "none"
        )
        self.assertEqual(relation.relation_type, "related_to")
        self.assertEqual(relation.source_concept_id, "a")
        self.assertEqual(relation.target_concept_id, "z")

    def test_self_relation_is_rejected(self):
        self.assertIsNone(
            build_relation_for_candidate(
                "same", "same", "related_to", "symmetric"
            )
        )


if __name__ == "__main__":
    unittest.main()
