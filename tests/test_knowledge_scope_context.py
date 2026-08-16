import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.knowledge_scope import format_knowledge_scope


class KnowledgeScopeContextTests(unittest.TestCase):

    def test_each_mode_becomes_an_explicit_turn_instruction(self):
        expected_phrases = {
            "allowed": "可以使用下列角色允许领域内的通用训练知识",
            "source_limited": "不要仅因话题在领域外就拒绝处理",
            "mixed": "把任务按来源分开",
            "uncertain": "不要把不完整输入直接判成必须拒绝",
            "not_applicable": "按当前互动自然回应",
        }

        for mode, phrase in expected_phrases.items():
            with self.subTest(mode=mode):
                text = format_knowledge_scope({
                    "knowledge_scope": {
                        "mode": mode,
                        "allowed_domain_matches": ["计算机软件"],
                        "restricted_topics": ["影视事实"],
                        "reason": "测试判断",
                    },
                })

                self.assertIn(f"范围模式：{mode}", text)
                self.assertIn(phrase, text)
                self.assertIn("允许领域匹配：计算机软件", text)
                self.assertIn("受限主题：影视事实", text)
                self.assertIn("不是新增事件事实", text)

    def test_source_limited_instruction_is_perception_source_neutral(self):
        text = format_knowledge_scope({
            "knowledge_scope": {
                "mode": "source_limited",
                "allowed_domain_matches": [],
                "restricted_topics": ["影视事实"],
                "reason": "测试判断",
            },
        })

        self.assertIn("不决定本轮内容是否值得检索或形成记忆", text)

        self.assertIn("当前感知事件", text)
        self.assertIn("带来源的近期上下文", text)
        self.assertIn("不得引入来源中不存在的外部事实", text)
        self.assertNotIn("用户本轮明确提供", text)

    def test_missing_scope_adds_no_fake_classification(self):
        self.assertEqual(format_knowledge_scope(None), "")
        self.assertEqual(format_knowledge_scope({}), "")


if __name__ == "__main__":
    unittest.main()
