import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.context_builder import build_agent_context
from core.perception import PerceptionFrame, create_perception_event
from memory import sql_store
from memory.schema import MemoryEntity


class FakeResponse:
    def __init__(self, content):
        self.content = content


class RecallUnderstandingLLM:
    def bind(self, **kwargs):
        return self

    def invoke(self, prompt):
        return FakeResponse(json.dumps({
            "situated_understanding": "用户明确询问过去形成的咖啡偏好认知。",
            "knowledge_scope": {
                "mode": "not_applicable",
                "allowed_domain_matches": [],
                "restricted_topics": [],
                "reason": "这是个人长期认知回忆，不需要参数知识。",
            },
            "capability_constraints": [
                "当前没有主动回忆工具，只能使用本轮自动自然浮现的认知。"
            ],
            "memory_activation_cues": [{
                "query": "咖啡偏好",
                "filters": {"memory_type": "preference"},
                "derived_from": "当前事件的明确回忆请求",
            }],
            "uncertainties": [],
        }, ensure_ascii=False))


class CrossThreadMemoryRecallTests(unittest.TestCase):

    def test_new_thread_without_recent_context_recalls_long_term_entity(self):
        with TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite")
            with patch.object(sql_store, "SQLITE_DB_PATH", db_path):
                sql_store.create_tables()
                sql_store.upsert_entity(MemoryEntity(
                    concept_id="coffee",
                    canonical_name="咖啡偏好",
                    aliases=["无糖咖啡偏好"],
                    memory_type="preference",
                    identity_signature={
                        "subject": "用户",
                        "relation": "prefers",
                        "object": "无糖咖啡",
                    },
                    summary=(
                        "用户表示过去偏好甜咖啡，"
                        "现在偏好无糖咖啡，工作时通常会喝一杯。"
                    ),
                    revision=3,
                ))

                # 新线程没有近期对话可借用；能得到答案只能来自长期库召回。
                frame = PerceptionFrame(
                    perception_event=create_perception_event(
                        "user",
                        "text",
                        "你还记得我喜欢喝什么吗？",
                    ),
                    working_context="",
                    state_snapshot={
                        "owner": "agent",
                        "thread_id": "brand-new-thread",
                        "mood": 50,
                        "energy": 100,
                    },
                    capability_snapshot={},
                    persona_context={},
                )
                with patch(
                    "memory.retrieval_engine._retrieve_vector_candidates",
                    return_value=[],
                ):
                    result = build_agent_context(
                        frame,
                        understanding_llm=RecallUnderstandingLLM(),
                    )

        self.assertEqual(frame.working_context, "")
        self.assertEqual(
            result["perception_understanding"]["memory_activation_cues"][0][
                "query"
            ],
            "咖啡偏好",
        )
        self.assertEqual(len(result["activated_memory_refs"]), 1)
        self.assertFalse(result["memory_activation_state"]["exhaustive"])
        self.assertFalse(
            result["memory_activation_state"]["absence_means_not_exists"]
        )
        recalled = result["activated_memory_refs"][0]
        self.assertEqual(recalled["concept_id"], "coffee")
        self.assertEqual(recalled["revision"], 3)
        self.assertIn("过去偏好甜咖啡", recalled["summary"])
        self.assertIn("现在偏好无糖咖啡", result["injection_text"])
        self.assertIn("【本轮能力约束】", result["injection_text"])
        self.assertIn("当前没有主动回忆工具", result["injection_text"])


if __name__ == "__main__":
    unittest.main()
