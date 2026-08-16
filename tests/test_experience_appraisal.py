import sys
from pathlib import Path
import unittest
import json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.experience import create_agent_action, create_experience_slice
from core.experience_appraisal import (
    appraise_experience,
    fallback_experience_appraisal,
)
from core.perception import create_perception_event

class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.bind_count = 0
        self.invoke_count = 0
        self.last_prompt = None

    def bind(self, **kwargs):
        self.bind_count += 1
        return self

    def invoke(self, prompt):
       self.invoke_count += 1
       self.last_prompt = prompt
       return FakeResponse(self.content)

class ExperienceAppraisalTests(unittest.TestCase):

    def _create_experience(self, activated_memory_refs=None):
        event = create_perception_event(
            source="user",
            modality="text",
            content="你好",
        )
        action = create_agent_action(
            action_type="visible_response",
            content="你好。",
        )

        return create_experience_slice(
            perception_event=event,
            perception_understanding={},
            activated_memory_refs=activated_memory_refs or [],
            response_or_actions=[action],
            observations=[],
            state_snapshot={
                "mood": 50,
                "energy": 100,
            },
        )

    def test_complete_failure_is_conservative(self):
        event = create_perception_event("user", "text", "你好")
        action = create_agent_action("visible_response", "你好。")

        experience = self._create_experience()

        result = fallback_experience_appraisal(
            experience,
            "LLM 调用失败",
        ).to_dict()

        self.assertEqual(
            result["emotion_assessment"]["event_valence"],
            "neutral",
        )
        self.assertEqual(
            result["emotion_assessment"]["affected_memories"],
            [],
        )
        self.assertEqual(
            result["memory_assessment"]["memory_candidates"],
            [],
        )
        self.assertEqual(len(result["fallback_regions"]), 3)
    
    def test_one_call_and_independent_region_fallback(self):
      experience = self._create_experience()
     
      llm = FakeLLM(json.dumps({
        "experience_review": {"experience_summary": "正常回看"},
        "emotion_assessment": [],
        "memory_assessment": {"memory_candidates": []},
      }, ensure_ascii=False))

      result = appraise_experience(
        experience,
        persona_context={"agent_name": "测试角色"},
        llm=llm,
      ).to_dict()

      self.assertEqual(llm.bind_count, 1)
      self.assertEqual(llm.invoke_count, 1)
      self.assertEqual(
        result["fallback_regions"],
        ["emotion_assessment"],
      )
      self.assertEqual(
        result["experience_review"]["experience_summary"],
        "正常回看",
      )

    def test_emotion_references_use_retrieved_id_whitelist(self):
      experience = self._create_experience([{
        "concept_id": "cog_valid",
        "canonical_name": "有效认知",
      }])

      llm = FakeLLM(json.dumps({
        "experience_review": {},
        "emotion_assessment": {
            "event_relevance": "high",
            "event_valence": "mild_positive",
            "salience": "medium",
            "evidence": [{
                "source_type": "perception",
                "source_id": experience.perception_event.event_id,
                "meaning": "当前事件直接影响测试目标",
            }],
            "affected_memories": [
                {
                    "concept_id": "cog_valid",
                    "change_direction": "slightly_positive",
                    "strength": "slight",
                   "label_update": {
                      "label": "开始产生兴趣",
                      "polarity": "positive",
                      "strength": "moderate",
                    },
                },
                {
                    "concept_id": "cog_fake",
                    "change_direction": "strengthened",
                    "strength": "strong",
                    "label_update":  {
                      "label": "开始产生兴趣",
                      "polarity": "positive",
                      "strength": "moderate",
                    },
                },
            ],
        },
        "memory_assessment": {},
      }, ensure_ascii=False))

      result = appraise_experience(
        experience,
        persona_context={},
        llm=llm,
      ).to_dict()

      affected = result["emotion_assessment"]["affected_memories"]

      self.assertEqual(len(affected), 1)
      self.assertEqual(affected[0]["concept_id"], "cog_valid")
      self.assertEqual(
          affected[0]["label_update"],
          {
             "label": "开始产生兴趣",
             "polarity": "positive",
             "strength": "moderate",
          },
)

    def test_memory_candidates_validate_keys_and_target_ids(self):
      experience = self._create_experience([{
        "concept_id": "cog_valid",
        "canonical_name": "已有认知",
    }])

      llm = FakeLLM(json.dumps({
        "experience_review": {},
        "emotion_assessment": {
            "event_relevance": "none",
            "event_valence": "neutral",
            "salience": "low",
        },
        "memory_assessment": {
            "memory_candidates": [
                {
                    "candidate_key": "candidate_1",
                    "operation": "create",
                    "concept_name": "主人喜欢咖啡",
                    "memory_type": "preference",
                    "identity_signature": {
                        "subject": "主人",
                        "relation": "喜欢",
                        "object": "咖啡",
                    },
                    "summary": "主人表示喜欢咖啡。",
                },
                {
                    "candidate_key": "candidate_1",
                    "operation": "create",
                    "concept_name": "重复候选",
                    "identity_signature": {"subject": "重复"},
                    "summary": "不应保留。",
                },
                {
                    "candidate_key": "candidate_2",
                    "operation": "reinforce",
                    "target_concept_id": "cog_fake",
                },
                {
                    "candidate_key": "candidate_3",
                    "operation": "reinforce",
                    "target_concept_id": "cog_valid",
                },
            ]
        },
    }, ensure_ascii=False))

      result = appraise_experience(
        experience,
        persona_context={},
        llm=llm,
    ).to_dict()

      candidates = result["memory_assessment"]["memory_candidates"]

      self.assertEqual(
        [item["candidate_key"] for item in candidates],
        ["candidate_1", "candidate_3"],
    )
      

    def test_new_memory_impressions_validate_candidate_keys(self):
      experience = self._create_experience([{
        "concept_id": "cog_valid",
        "canonical_name": "已有认知",
    }])

      raw_memory = {
        "memory_candidates": [
            {
                "candidate_key": "candidate_1",
                "operation": "create",
                "concept_name": "新认知一",
                "identity_signature": {"subject": "主人"},
                "summary": "第一个新认知。",
            },
            {
                "candidate_key": "candidate_2",
                "operation": "create",
                "concept_name": "新认知二",
                "identity_signature": {"subject": "主人"},
                "summary": "第二个新认知。",
            },
        ],
        "new_memory_impressions": [
            {
                "candidate_key": "candidate_1",
                "candidate_valence": "mild_positive",
                "persona_effect": "fitting",
                "direct_related_concept_ids": [
                    "cog_valid",
                    "cog_fake",
                ],
                "label_update":  {
                      "label": "开始产生兴趣",
                      "polarity": "positive",
                      "strength": "moderate",
                    },
            },
            {
                "candidate_key": "candidate_unknown",
                "persona_effect": "fitting",
            },
        ],
    }

      llm = FakeLLM(json.dumps({
        "experience_review": {},
        "emotion_assessment": {
            "event_relevance": "high",
            "event_valence": "mild_positive",
            "salience": "medium",
            "evidence": [{
                "source_type": "perception",
                "source_id": experience.perception_event.event_id,
                "meaning": "当前事件直接影响测试目标",
            }],
        },
        "memory_assessment": raw_memory,
    }, ensure_ascii=False))

      result = appraise_experience(
        experience,
        persona_context={},
        llm=llm,
    ).to_dict()

      impressions = result["memory_assessment"][
        "new_memory_impressions"
    ]

      self.assertEqual(len(impressions), 2)
      self.assertEqual(
        impressions[0]["direct_related_concept_ids"],
        ["cog_valid"],
    )
      self.assertEqual(
    impressions[0]["label_update"],
    {
        "label": "开始产生兴趣",
        "polarity": "positive",
        "strength": "moderate",
    },
)
      self.assertEqual(
    impressions[1]["label_update"],
    {
        "label": "中性",
        "polarity": "neutral",
        "strength": "neutral",
    },
)
      self.assertTrue(impressions[1]["fallback_to_neutral"])

    def test_experience_review_requires_evidence_for_salient_points(self):
      experience = self._create_experience()

      llm = FakeLLM(json.dumps({
        "experience_review": {
            "experience_summary": "用户进行了普通问候。",
            "situated_interpretation": "没有额外含义。",
            "salient_points": [
                {
                    "point": "有效线索",
                    "evidence": "用户说了你好",
                    "possible_downstream_use": ["emotion", "invalid"],
                },
                {
                    "point": "没有证据的推测",
                    "evidence": "",
                    "possible_downstream_use": ["memory"],
                },
            ],
            "do_not_assume": ["不能假设关系发生变化"],
        },
        "emotion_assessment": {
            "event_relevance": "low",
            "event_valence": "mild_positive",
            "salience": "low",
        },
        "memory_assessment": {},
      }, ensure_ascii=False))

      result = appraise_experience(
        experience,
        persona_context={},
        llm=llm,
      ).to_dict()

      review = result["experience_review"]

      self.assertEqual(len(review["salient_points"]), 1)
      self.assertEqual(
        review["salient_points"][0]["possible_downstream_use"],
        ["emotion"],
      )
      self.assertEqual(
        result["emotion_assessment"]["event_valence"],
        "neutral",
      )

    def test_low_salience_cannot_affect_existing_memory(self):
      experience = self._create_experience([{
        "concept_id": "cog_valid",
        "canonical_name": "已有认知",
        "emotion_score": 60.0,
    }])

      llm = FakeLLM(json.dumps({
        "experience_review": {},
        "emotion_assessment": {
            "event_relevance": "high",
            "event_valence": "mild_positive",
            "salience": "low",
            "evidence": [{
                "source_type": "perception",
                "source_id": experience.perception_event.event_id,
                "meaning": "当前事件直接影响测试目标",
            }],
            "affected_memories": [
                {
                    "concept_id": "cog_valid",
                    "change_direction": "strengthened",
                    "strength": "strong",
                    "label_update": {
                        "label": "更加喜欢",
                        "polarity": "positive",
                        "strength": "moderate",
                    },
                },
            ],
        },
        "memory_assessment": {},
    }, ensure_ascii=False))

      result = appraise_experience(
        experience,
        persona_context={},
        llm=llm,
    ).to_dict()

      emotion = result["emotion_assessment"]

      self.assertEqual(emotion["affected_memories"], [])

    # relevance 仍然是 high，因此事件效价不被改成 neutral；
    # 后续 mood 规则可以按 low salience 计算很小的变化。
      self.assertEqual(
        emotion["event_valence"],
        "mild_positive",
    )

    def test_update_uses_retrieved_revision_and_structured_intent(self):
      experience = self._create_experience([{
        "concept_id": "cog_valid",
        "canonical_name": "咖啡偏好",
        "revision": 4,
      }])
      llm = FakeLLM(json.dumps({
        "experience_review": {},
        "emotion_assessment": {
            "event_relevance": "medium",
            "event_valence": "neutral",
            "salience": "medium",
        },
        "memory_assessment": {"memory_candidates": [{
            "candidate_key": "update_1",
            "operation": "update",
            "target_concept_id": "cog_valid",
            "base_revision": 999,
            "summary_update": {
                "update_kind": "contextualize",
                "new_information": "用户工作时通常喝无糖咖啡",
                "superseded_information": "",
                "revised_summary": "用户表示偏好无糖咖啡，工作时通常会喝一杯。",
            },
        }]},
      }, ensure_ascii=False))

      result = appraise_experience(experience, persona_context={}, llm=llm)
      candidate = result.to_dict()["memory_assessment"][
          "memory_candidates"
      ][0]

      self.assertEqual(candidate["base_revision"], 4)
      self.assertEqual(
          candidate["summary_update"]["update_kind"],
          "contextualize",
      )

    def test_update_with_string_summary_is_rejected(self):
      experience = self._create_experience([{
        "concept_id": "cog_valid",
        "canonical_name": "咖啡偏好",
        "revision": 1,
      }])
      llm = FakeLLM(json.dumps({
        "experience_review": {},
        "emotion_assessment": {
            "event_relevance": "medium",
            "event_valence": "neutral",
            "salience": "medium",
        },
        "memory_assessment": {"memory_candidates": [{
            "candidate_key": "update_1",
            "operation": "update",
            "target_concept_id": "cog_valid",
            "summary_update": "直接覆盖旧摘要",
        }]},
      }, ensure_ascii=False))

      result = appraise_experience(experience, persona_context={}, llm=llm)
      self.assertEqual(
          result.to_dict()["memory_assessment"]["memory_candidates"],
          [],
      )

    def test_high_relevance_without_traceable_evidence_falls_back(self):
      experience = self._create_experience()
      llm = FakeLLM(json.dumps({
        "experience_review": {"experience_summary": "发生了一次事件。"},
        "emotion_assessment": {
            "event_relevance": "high",
            "event_valence": "strong_positive",
            "salience": "high",
            "evidence": [],
        },
        "memory_assessment": {},
      }, ensure_ascii=False))

      result = appraise_experience(
        experience,
        persona_context={},
        llm=llm,
      ).to_dict()

      self.assertIn("emotion_assessment", result["fallback_regions"])
      self.assertEqual(
        result["emotion_assessment"]["event_valence"],
        "neutral",
      )

    def test_fact_copy_label_is_rejected_and_persona_defaults_neutral(self):
      experience = self._create_experience()
      llm = FakeLLM(json.dumps({
        "experience_review": {"experience_summary": "用户表达水果偏好。"},
        "emotion_assessment": {
            "event_relevance": "medium",
            "event_valence": "neutral",
            "salience": "medium",
        },
        "memory_assessment": {
            "memory_candidates": [{
                "candidate_key": "candidate_1",
                "operation": "create",
                "concept_name": "主人喜欢苹果",
                "memory_type": "preference",
                "identity_signature": {
                    "subject": "主人",
                    "relation": "喜欢",
                    "object": "苹果",
                },
                "summary": "用户表示喜欢苹果。",
            }],
            "new_memory_impressions": [{
                "candidate_key": "candidate_1",
                "persona_effect": "unknown",
                "direct_related_concept_ids": [],
                "label_update": {
                    "label": "主人喜欢苹果",
                    "polarity": "neutral",
                    "strength": "neutral",
                },
            }],
        },
      }, ensure_ascii=False))

      result = appraise_experience(
        experience,
        persona_context={},
        llm=llm,
      ).to_dict()
      impression = result["memory_assessment"][
        "new_memory_impressions"
      ][0]

      self.assertEqual(impression["persona_effect"], "neutral")
      self.assertIsNone(impression["label_update"])

    def test_prompt_contains_complete_output_contract(self):
      experience = self._create_experience()

      llm = FakeLLM(json.dumps({
        "experience_review": {},
        "emotion_assessment": {
            "event_relevance": "none",
            "event_valence": "neutral",
            "salience": "low",
        },
        "memory_assessment": {},
    }, ensure_ascii=False))

      appraise_experience(
        experience,
        persona_context={},
        llm=llm,
    )

      self.assertIn(
        '"affected_memories"',
        llm.last_prompt,
    )
      self.assertIn(
        '"candidate_key"',
        llm.last_prompt,
    )
      self.assertIn(
        '"label_update"',
        llm.last_prompt,
    )
      self.assertIn(
        '"polarity"',
        llm.last_prompt,
    )
      self.assertIn(
        "salience 为 low 时",
        llm.last_prompt,
    )
      self.assertIn(
        "不得输出 event_effect",
        llm.last_prompt,
    )
      self.assertIn(
        "不要输出 event_type",
        llm.last_prompt,
    )
      self.assertIn(
            "自然浮现不等于受影响",
        llm.last_prompt,
    )
      self.assertIn(
        "单次行为通常不足以建立 interaction_pattern",
        llm.last_prompt,
    )
      self.assertIn(
        "工具结果、视觉或听觉描述",
        llm.last_prompt,
    )
      self.assertIn(
        "不能只因为事件“可能”涉及",
        llm.last_prompt,
    )
      self.assertIn(
        "不是记忆候选白名单",
        llm.last_prompt,
    )
      self.assertIn(
        "领域外事实，只要有明确来源",
        llm.last_prompt,
    )
      self.assertIn(
        "直接针对 Agent 或关系对象的亲密、拒绝、冲突、边界",
        llm.last_prompt,
    )
      self.assertIn(
        "普通个人事实通常是 neutral",
        llm.last_prompt,
    )
      self.assertIn(
        "不能复述 concept_name 或 summary",
        llm.last_prompt,
    )
      self.assertIn(
        "没有被撤回的其他事实保持不变",
        llm.last_prompt,
    )

if __name__ == "__main__":
    unittest.main()
