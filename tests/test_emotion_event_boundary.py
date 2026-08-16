import unittest
from unittest.mock import patch
from project_root_path import setrootpath
setrootpath()
from core.perception import create_perception_event
from main import _build_perception_frame, _build_persona_context


class EmotionEventBoundaryTests(unittest.TestCase):

    def test_persona_context_keeps_semantic_boundaries_only(self):
        persona = {
            "agent_name": "测试角色",
            "self_terms": ["本机"],
            "user_role": "用户",
            "relationship": "协作关系",
            "personality": "谨慎",
            "goals": ["完成当前任务"],
            "values": ["诚实"],
            "boundaries": ["不越权操作"],
            "obedience_rule": "只在授权范围内执行",
            "knowledge_boundary": {
                "personal_memory_rule": "个人事实来自长期认知",
            },
            "expression_preferences": {
                "initiative": "信息不足时先询问",
                "tone": "简洁",
            },
            "emotion_profile": {"mood_reactivity": 0.8},
            "persona_bias": -1,
            "genesis_memory": {"summary": "实例记忆"},
            "model": {"api_key_env": "SECRET"},
        }

        context = _build_persona_context(persona)

        self.assertEqual(
            context["initiative"],
            "信息不足时先询问",
        )
        self.assertIn("knowledge_boundary", context)
        self.assertIn("boundaries", context)
        self.assertNotIn("expression_preferences", context)
        self.assertNotIn("emotion_profile", context)
        self.assertNotIn("persona_bias", context)
        self.assertNotIn("genesis_memory", context)
        self.assertNotIn("model", context)

    def test_perception_frame_reads_state_once_at_event_boundary(self):
        event = create_perception_event(
            source="user",
            modality="text",
            content="你好",
        )

        persona = {
            "agent_name": "测试角色",
            "user_role": "用户",
        }

        with patch(
            "main.begin_perception_event",
            return_value={"mood": 63, "energy": 88},
        ) as begin_event:
            frame = _build_perception_frame(
                event=event,
                thread_id="test_thread",
                recent_context=[],
                persona=persona,
            )

        begin_event.assert_called_once_with("test_thread")
        self.assertEqual(frame.state_snapshot["mood"], 63)
        self.assertEqual(frame.state_snapshot["energy"], 88)


if __name__ == "__main__":
    unittest.main()
