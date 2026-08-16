import unittest
from project_root_path import setrootpath
setrootpath()
from persona.loader import build_system_prompt, load_persona, validate_persona
from emotion.translator import format_emotion_context


class PersonaConfigTests(unittest.TestCase):

    def test_current_persona_reactivity_is_valid(self):
        persona = load_persona()
        value = persona["emotion_profile"]["mood_reactivity"]

        self.assertGreaterEqual(value, 0.5)
        self.assertLessEqual(value, 1.5)

    def test_invalid_reactivity_is_rejected(self):
        persona = {
            "emotion_profile": {
                "mood_reactivity": 2.0,
            }
        }

        with self.assertRaises(ValueError):
            validate_persona(persona)

    def test_system_prompt_explains_memory_and_state_boundaries(self):
        persona = load_persona()
        prompt = build_system_prompt(persona)

        self.assertIn("不知道本轮候选最终会被写入、合并还是驳回", prompt)
        self.assertIn("不要逐条询问用户是否允许", prompt)
        self.assertIn("Persona 是稳定的性格", prompt)
        self.assertIn("局部主观印记", prompt)
        self.assertIn("只撤回被明确否定或替换的原子事实", prompt)
        self.assertIn("不能仅因主题", prompt)
        self.assertIn("在允许领域外就拒绝处理", prompt)
        self.assertIn("当前感知事件", prompt)
        self.assertIn("带来源的", prompt)
        self.assertIn("不得引入来源中不存在的外部事实", prompt)
        self.assertIn("主题白名单", prompt)
        self.assertIn("最后才是角色卡允许领域内的通用训练知识", prompt)
        self.assertIn("已记录", prompt)
        self.assertIn("禁止使用这些说法", prompt)

    def test_persona_uses_source_aware_outside_domain_rule(self):
        persona = load_persona()
        rule = persona["knowledge_boundary"]["outside_domain_rule"]

        self.assertIn("不得调用训练数据中的领域外知识", rule)
        self.assertIn("当前感知事件", rule)
        self.assertIn("带来源的近期上下文", rule)
        self.assertNotIn("然后停止", rule)

    def test_dynamic_state_text_is_persona_neutral(self):
        text = format_emotion_context(80, 20)

        self.assertIn("高于基线", text)
        self.assertIn("可用水平低", text)
        self.assertNotIn("暖意", text)
        self.assertNotIn("烦躁", text)
        self.assertNotIn("必须按照这些状态进行角色扮演", text)


if __name__ == "__main__":
    unittest.main()
