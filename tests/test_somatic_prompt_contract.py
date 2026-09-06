import unittest

from core.somatic_appraisal import _build_prompt
from core.somatic_service import format_somatic_context, SomaticOutcome
from core.somatic_simulation import SomaticEventDecision, SomaticState
from persona.loader import build_system_prompt
from tools.somatic_tools import somatic_action


class SomaticPromptContractTests(unittest.TestCase):
    def _decision(self):
        return SomaticEventDecision(
            status="occurred",
            contact_object="agent",
            contact_area="hand",
            intensity="gentle",
            duration="brief",
            uncertainty="low",
        )

    def test_appraisal_prompt_states_scope_without_case_specific_rules(self):
        prompt = _build_prompt(
            decision=self._decision(),
            state=SomaticState({"arousal": 0.5, "sensitivity": 0.5}),
            event_context={"actor": "agent", "target": "user"},
            persona_context={"embodiment": {"reaction_style": "自然"}},
        )

        self.assertIn("结构化事件", prompt)
        self.assertIn("只返回协议中的字段", prompt)
        self.assertIn("不替其他模块", prompt)
        self.assertNotIn("身体反应不代表同意、好感或关系升级", prompt)
        self.assertNotIn("现实设备", prompt)

    def test_agent_prompt_describes_general_tool_contract(self):
        persona = {
            "agent_name": "测试角色",
            "emotion_profile": {"mood_reactivity": 1.0},
        }
        prompt = build_system_prompt(persona)

        self.assertIn("执行当前能力快照允许的具身动作", prompt)
        self.assertIn("同一事件已经有身体模块结果时", prompt)
        self.assertIn("身体反馈只提供身体层信息", prompt)
        self.assertNotIn("用户对你的动作已经由身体模块处理时", prompt)
        self.assertNotIn("不需要反复向用户解释现实与角色世界的区别", prompt)

    def test_tool_description_is_channel_neutral(self):
        description = somatic_action.description

        self.assertIn("执行一次新的具身动作", description)
        self.assertIn("当前执行通道", description)
        self.assertNotIn("现实设备", description)

    def test_dynamic_feedback_describes_only_somatic_scope(self):
        outcome = SomaticOutcome(
            status="completed",
            operation_id="op-1",
            source_kind="agent_action",
            decision=self._decision(),
            appraisal=None,
            state_before=SomaticState({"arousal": 0.5}),
            state_after=SomaticState({"arousal": 0.55}),
            revision=1,
            visible_reaction="肩膀微微放松",
            interoception_description="胸前感到温热",
        )
        text = format_somatic_context([outcome])

        self.assertIn("只描述身体层信息", text)
        self.assertNotIn("身体反应不代表同意、好感或关系变化", text)


if __name__ == "__main__":
    unittest.main()
