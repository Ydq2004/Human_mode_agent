import json
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.identity_judge import judge_ambiguous_identity
from memory.schema import MemoryEntity


class FakeJsonLLM:
    def __init__(self, result):
        self.result = result

    def bind(self, **kwargs):
        return self

    def invoke(self, prompt):
        return SimpleNamespace(content=json.dumps(self.result))


def _existing():
    return MemoryEntity(
        concept_id="existing",
        canonical_name="已有实体",
        aliases=["已有实体"],
        memory_type="entity",
        identity_signature={"subject": "x", "relation": "is", "object": "y"},
        summary="已有实体摘要",
    )


class IdentityJudgeDirectionTests(unittest.TestCase):

    def test_related_keeps_whitelisted_direction(self):
        result = judge_ambiguous_identity(
            {"concept_name": "候选"},
            [_existing()],
            FakeJsonLLM({
                "decision": "related",
                "target_concept_id": "existing",
                "relation_type": "belongs_to",
                "relation_direction": "candidate_to_existing",
                "reason": "候选属于已有实体",
            }),
        )
        self.assertEqual(result["relation_direction"], "candidate_to_existing")

    def test_invalid_direction_falls_back_to_symmetric(self):
        result = judge_ambiguous_identity(
            {"concept_name": "候选"},
            [_existing()],
            FakeJsonLLM({
                "decision": "related",
                "target_concept_id": "existing",
                "relation_type": "belongs_to",
                "relation_direction": "invented",
                "reason": "测试非法方向",
            }),
        )
        self.assertEqual(result["relation_direction"], "symmetric")

    def test_same_never_carries_relation_direction(self):
        result = judge_ambiguous_identity(
            {"concept_name": "候选", "memory_type": "entity"},
            [_existing()],
            FakeJsonLLM({
                "decision": "same",
                "target_concept_id": "existing",
                "relation_type": "belongs_to",
                "relation_direction": "candidate_to_existing",
                "reason": "同一认知",
            }),
        )
        self.assertEqual(result["relation_type"], "none")
        self.assertEqual(result["relation_direction"], "none")

    def test_cross_type_same_is_downgraded_to_related(self):
        existing = _existing()
        existing.memory_type = "event"

        result = judge_ambiguous_identity(
            {
                "concept_name": "主人不喜欢被欺骗",
                "memory_type": "preference",
            },
            [existing],
            FakeJsonLLM({
                "decision": "same",
                "target_concept_id": "existing",
                "relation_type": "none",
                "relation_direction": "none",
                "reason": "主题相近",
            }),
        )

        self.assertEqual(result["decision"], "related")
        self.assertEqual(result["target_concept_id"], "existing")
        self.assertEqual(result["relation_type"], "related_to")
        self.assertEqual(result["relation_direction"], "symmetric")


if __name__ == "__main__":
    unittest.main()
