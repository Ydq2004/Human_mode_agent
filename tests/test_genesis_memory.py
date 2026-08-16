import unittest

from project_root_path import setrootpath

setrootpath()

from memory.store_manager import build_genesis_entity


class GenesisMemoryTests(unittest.TestCase):

    def test_genesis_memory_is_a_relationship_entity_not_knowledge_scope(self):
        entity = build_genesis_entity({
            "agent_name": "如烟",
            "user_role": "主人",
            "knowledge_boundary": {
                "allowed_domains": ["编程"],
            },
            "genesis_memory": {
                "summary": "用户唤醒并创造了本机。",
                "emotion_score": 50,
                "emotion_label": "中立",
                "tags": "人类,绑定",
            },
        })

        self.assertIsNotNone(entity)
        self.assertEqual(entity.memory_type, "relationship")
        self.assertEqual(entity.source, "genesis")
        self.assertEqual(entity.canonical_name, "主人")
        self.assertEqual(entity.tags, ["人类", "绑定"])
        self.assertNotIn("allowed_domains", entity.summary)

    def test_missing_genesis_configuration_does_not_create_fake_memory(self):
        self.assertIsNone(build_genesis_entity({
            "agent_name": "如烟",
            "user_role": "主人",
        }))


if __name__ == "__main__":
    unittest.main()
