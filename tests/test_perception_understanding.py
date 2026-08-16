import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.perception import PerceptionFrame, create_perception_event
from core.perception_understanding import understand_perception


class FakeResponse:
    def __init__(
        self,
        content,
        *,
        response_metadata=None,
        additional_kwargs=None,
        usage_metadata=None,
    ):
        self.content = content
        self.response_metadata = response_metadata or {}
        self.additional_kwargs = additional_kwargs or {}
        self.usage_metadata = usage_metadata or {}


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.last_prompt = ""

    def bind(self, **kwargs):
        return self

    def invoke(self, prompt):
        self.last_prompt = prompt
        return FakeResponse(json.dumps(self.payload, ensure_ascii=False))


class FailingLLM(FakeLLM):
    def invoke(self, prompt):
        self.invoke_count = getattr(self, "invoke_count", 0) + 1
        raise RuntimeError("模拟调用失败")


class RawResponseLLM(FakeLLM):
    def __init__(self, response):
        self.response = response
        self.last_prompt = ""
        self.invoke_count = 0

    def invoke(self, prompt):
        self.last_prompt = prompt
        self.invoke_count += 1
        return self.response


class SequenceResponseLLM(FakeLLM):
    """按顺序返回响应，用于模拟第一次空回、第二次恢复。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.last_prompt = ""
        self.invoke_count = 0

    def invoke(self, prompt):
        self.last_prompt = prompt
        response = self.responses[self.invoke_count]
        self.invoke_count += 1
        return response


def create_frame(content="我喜欢苹果", capability_snapshot=None):
    return PerceptionFrame(
        perception_event=create_perception_event("user", "text", content),
        working_context="",
        state_snapshot={"owner": "agent", "mood": 50, "energy": 100},
        capability_snapshot=capability_snapshot or {},
        persona_context={
            "personality": "克制",
            "knowledge_boundary": {
                "allowed_domains": [
                    "计算机硬件",
                    "计算机软件",
                    "编程和项目开发",
                ],
            },
        },
    )


def make_scope(mode="not_applicable", **overrides):
    scope = {
        "mode": mode,
        "allowed_domain_matches": [],
        "restricted_topics": [],
        "reason": "测试判断",
    }
    scope.update(overrides)
    return scope


class PerceptionUnderstandingTests(unittest.TestCase):

    @staticmethod
    def _run_with_output(llm, retry_llm=None):
        output = StringIO()
        with redirect_stdout(output):
            result = understand_perception(
                create_frame("测试诊断输入"),
                llm,
                retry_llm,
            )
        return result, output.getvalue()

    def test_output_contract_contains_only_framework_fields(self):
        llm = FakeLLM({
            "perception_summary": "旧字段",
            "situated_understanding": "用户陈述稳定个人偏好。",
            "knowledge_scope": make_scope(),
            "capability_constraints": [],
            "salient_clues": [{"clue": "旧字段"}],
            "memory_activation_cues": [{
                "query": "用户的苹果偏好",
                "filters": {"memory_type": "preference"},
                "derived_from": "当前用户陈述",
            }],
            "uncertainties": [],
            "suggested_next_focus": "旧字段",
        })

        result = understand_perception(create_frame(), llm)

        self.assertEqual(
            set(result),
            {
                "understanding_status",
                "situated_understanding",
                "knowledge_scope",
                "capability_constraints",
                "memory_activation_cues",
                "uncertainties",
            },
        )
        self.assertEqual(len(result["memory_activation_cues"]), 1)

    def test_capability_constraints_are_cleaned_and_prompt_uses_snapshot(self):
        frame = create_frame(
            "不要修改项目文件",
            capability_snapshot={
                "registered_tools": [],
                "device_control": False,
            },
        )
        llm = FakeLLM({
            "situated_understanding": "用户要求文件保持不变。",
            "knowledge_scope": make_scope(),
            "capability_constraints": [
                "当前没有文件操作工具，不能声称文件已经保持不变。",
                "当前没有文件操作工具，不能声称文件已经保持不变。",
                "",
            ],
            "memory_activation_cues": [],
            "uncertainties": [],
        })

        result = understand_perception(frame, llm)

        self.assertEqual(result["capability_constraints"], [
            "当前没有文件操作工具，不能声称文件已经保持不变。",
        ])
        self.assertIn("用户希望发生什么", llm.last_prompt)
        self.assertIn('"device_control": false', llm.last_prompt)

    def test_explicit_empty_hints_are_respected(self):
        llm = FakeLLM({
            "situated_understanding": "普通问候。",
            "knowledge_scope": make_scope(),
            "memory_activation_cues": [],
            "uncertainties": [],
        })

        result = understand_perception(create_frame("你好"), llm)

        self.assertEqual(result["memory_activation_cues"], [])

    def test_invalid_nonempty_hints_fall_back_to_raw_event(self):
        llm = FakeLLM({
            "situated_understanding": "用户陈述偏好。",
            "knowledge_scope": make_scope(),
            "memory_activation_cues": [{"query": ""}],
            "uncertainties": [],
        })
        frame = create_frame("我喜欢苹果")

        result = understand_perception(frame, llm)

        self.assertEqual(
            result["memory_activation_cues"][0]["query"],
            "我喜欢苹果",
        )

    def test_prompt_uses_material_impact_retrieval_rule(self):
        llm = FakeLLM({
            "situated_understanding": "普通问候。",
            "knowledge_scope": make_scope(),
            "memory_activation_cues": [],
            "uncertainties": [],
        })

        understand_perception(create_frame("你好"), llm)

        self.assertIn("不要求用户主动询问过去", llm.last_prompt)
        self.assertIn("模糊动作", llm.last_prompt)
        self.assertIn("工具结果、视觉或听觉描述", llm.last_prompt)
        self.assertIn("不是“话题名称是否在领域内”", llm.last_prompt)
        self.assertIn("用户给出领域外材料并要求总结", llm.last_prompt)
        self.assertIn("完成当前事件所需处理", llm.last_prompt)
        self.assertIn("带来源的\nworking_context", llm.last_prompt)
        self.assertIn("不能引入来源中不存在的外部事实", llm.last_prompt)
        self.assertNotIn("完成用户当前要求", llm.last_prompt)
        self.assertIn("not_applicable", llm.last_prompt)
        self.assertIn("不是 Agent 的\n长期认知目录", llm.last_prompt)
        self.assertIn("这一步与 knowledge_scope 分开判断", llm.last_prompt)
        self.assertIn("新实体或有复用价值的来源材料", llm.last_prompt)
        self.assertIn("关系状态、许可或边界", llm.last_prompt)
        self.assertNotIn("suggested_next_focus", llm.last_prompt)
        self.assertIn("不能代替自动认知唤起", llm.last_prompt)
        self.assertIn("即使近期上下文已经直接写出了答案", llm.last_prompt)
        self.assertIn("明确要求回忆", llm.last_prompt)

    def test_all_knowledge_scope_modes_are_preserved(self):
        for mode in (
            "allowed",
            "source_limited",
            "mixed",
            "uncertain",
            "not_applicable",
        ):
            with self.subTest(mode=mode):
                scope_overrides = {}
                if mode in {"allowed", "mixed"}:
                    scope_overrides["allowed_domain_matches"] = [
                        "计算机软件"
                    ]
                llm = FakeLLM({
                    "situated_understanding": "测试情境。",
                    "knowledge_scope": make_scope(
                        mode,
                        **scope_overrides,
                    ),
                    "memory_activation_cues": [],
                    "uncertainties": [],
                })

                result = understand_perception(create_frame(), llm)

                self.assertEqual(result["knowledge_scope"]["mode"], mode)

    def test_allowed_modes_without_whitelist_match_are_uncertain(self):
        for mode in ("allowed", "mixed"):
            with self.subTest(mode=mode):
                result = understand_perception(
                    create_frame(),
                    FakeLLM({
                        "situated_understanding": "测试情境。",
                        "knowledge_scope": make_scope(mode),
                        "memory_activation_cues": [],
                        "uncertainties": [],
                    }),
                )

                self.assertEqual(
                    result["knowledge_scope"]["mode"],
                    "uncertain",
                )
                self.assertIn(
                    "没有给出角色卡白名单",
                    result["knowledge_scope"]["reason"],
                )

    def test_allowed_domain_matches_use_persona_whitelist(self):
        llm = FakeLLM({
            "situated_understanding": "用户请求软件帮助。",
            "knowledge_scope": make_scope(
                "mixed",
                allowed_domain_matches=[
                    "计算机软件",
                    "影视娱乐",
                    "计算机软件",
                ],
                restricted_topics=["电影事实", "电影事实"],
            ),
            "memory_activation_cues": [],
            "uncertainties": [],
        })

        result = understand_perception(create_frame(), llm)
        scope = result["knowledge_scope"]

        self.assertEqual(
            scope["allowed_domain_matches"],
            ["计算机软件"],
        )
        self.assertEqual(scope["restricted_topics"], ["电影事实"])

    def test_missing_or_invalid_scope_falls_back_to_uncertain(self):
        for scope in (None, {"mode": "outside"}):
            with self.subTest(scope=scope):
                payload = {
                    "situated_understanding": "测试情境。",
                    "memory_activation_cues": [],
                    "uncertainties": [],
                }
                if scope is not None:
                    payload["knowledge_scope"] = scope

                result = understand_perception(
                    create_frame(),
                    FakeLLM(payload),
                )

                self.assertEqual(
                    result["knowledge_scope"]["mode"],
                    "uncertain",
                )

    def test_llm_failure_uses_conservative_scope(self):
        llm = FailingLLM({})
        result, output = self._run_with_output(llm)

        self.assertEqual(
            result["knowledge_scope"]["mode"],
            "uncertain",
        )
        self.assertIn(
            "不能可靠确定",
            result["knowledge_scope"]["reason"],
        )
        self.assertIn('"failure_stage": "llm_call_failed"', output)
        self.assertNotIn("测试诊断输入", output)
        self.assertEqual(llm.invoke_count, 1)

    def test_empty_response_has_separate_failure_stage(self):
        for content in ("", "   \n"):
            with self.subTest(content=repr(content)):
                response = FakeResponse(
                    content,
                    response_metadata={
                        "finish_reason": "length",
                        "model_name": "test-model",
                    },
                    usage_metadata={"output_tokens": 1024},
                )
                llm = RawResponseLLM(response)
                result, output = self._run_with_output(llm)

                self.assertEqual(
                    result["knowledge_scope"]["mode"],
                    "uncertain",
                )
                self.assertIn('"failure_stage": "empty_response"', output)
                self.assertIn('"finish_reason": "length"', output)
                self.assertIn('"content_length"', output)
                self.assertIn('"retry_result": "empty_again"', output)
                self.assertEqual(llm.invoke_count, 2)

    def test_empty_response_is_retried_once_and_can_recover(self):
        payload = {
            "situated_understanding": "第二次调用恢复。",
            "knowledge_scope": make_scope(),
            "memory_activation_cues": [],
            "uncertainties": [],
        }
        llm = SequenceResponseLLM([
            FakeResponse("", response_metadata={"finish_reason": "stop"}),
            FakeResponse(json.dumps(payload, ensure_ascii=False)),
        ])

        result, output = self._run_with_output(llm)

        self.assertEqual(result["situated_understanding"], "第二次调用恢复。")
        self.assertEqual(llm.invoke_count, 2)
        self.assertIn("PerceptionUnderstanding 空响应，准备重试", output)
        self.assertIn("PerceptionUnderstanding 重试恢复", output)
        self.assertIn('"retry_result": "success"', output)

    def test_empty_response_uses_independent_retry_llm_when_provided(self):
        payload = {
            "situated_understanding": "独立重试客户端恢复。",
            "knowledge_scope": make_scope(),
            "memory_activation_cues": [],
            "uncertainties": [],
        }
        first_llm = RawResponseLLM(
            FakeResponse("", response_metadata={"finish_reason": "stop"})
        )
        retry_llm = FakeLLM(payload)
        retry_llm.invoke_count = 0
        original_invoke = retry_llm.invoke

        def counted_invoke(prompt):
            retry_llm.invoke_count += 1
            return original_invoke(prompt)

        retry_llm.invoke = counted_invoke

        result, _ = self._run_with_output(first_llm, retry_llm)

        self.assertEqual(result["understanding_status"], "normal")
        self.assertEqual(first_llm.invoke_count, 1)
        self.assertEqual(retry_llm.invoke_count, 1)

    def test_non_json_response_reports_safe_preview_and_metadata(self):
        response = FakeResponse(
            "not-json",
            response_metadata={
                "finish_reason": "stop",
                "token_usage": {"completion_tokens": 12},
            },
        )
        llm = RawResponseLLM(response)
        result, output = self._run_with_output(llm)

        self.assertEqual(result["knowledge_scope"]["mode"], "uncertain")
        self.assertIn('"failure_stage": "json_parse_failed"', output)
        self.assertIn("not-json", output)
        self.assertIn('"finish_reason": "stop"', output)
        self.assertEqual(llm.invoke_count, 1)

    def test_refusal_and_reasoning_are_reported_without_reasoning_text(self):
        response = FakeResponse(
            "",
            additional_kwargs={
                "refusal": "request refused",
                "reasoning_content": "hidden reasoning must not be printed",
            },
        )
        _, output = self._run_with_output(RawResponseLLM(response))

        self.assertIn('"refusal_present": true', output)
        self.assertIn("request refused", output)
        self.assertIn('"reasoning_content_present": true', output)
        self.assertIn('"reasoning_content_length": 36', output)
        self.assertNotIn("hidden reasoning must not be printed", output)

    def test_json_array_is_classified_as_output_shape_failure(self):
        result, output = self._run_with_output(
            RawResponseLLM(FakeResponse("[]"))
        )

        self.assertEqual(result["knowledge_scope"]["mode"], "uncertain")
        self.assertIn('"failure_stage": "output_shape_failed"', output)

    def test_schema_clean_exception_has_separate_failure_stage(self):
        response = FakeResponse(json.dumps({
            "situated_understanding": "测试",
            "knowledge_scope": make_scope(),
            "memory_activation_cues": [],
            "uncertainties": [],
        }))
        output = StringIO()
        with patch(
            "core.perception_understanding._clean_understanding",
            side_effect=ValueError("模拟清洗失败"),
        ), redirect_stdout(output):
            result = understand_perception(
                create_frame("测试诊断输入"),
                RawResponseLLM(response),
            )

        self.assertEqual(result["knowledge_scope"]["mode"], "uncertain")
        self.assertIn(
            '"failure_stage": "schema_clean_failed"',
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
