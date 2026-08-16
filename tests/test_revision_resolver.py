import json
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.revision_resolver import resolve_memory_revision
from memory.schema import MemoryEntity


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.invoke_count = 0
        self.last_prompt = ""

    def bind(self, **kwargs):
        return self

    def invoke(self, prompt):
        self.invoke_count += 1
        self.last_prompt = prompt
        return FakeResponse(json.dumps(self.payload, ensure_ascii=False))


def entity(summary="用户表示喜欢无糖咖啡。"):
    return MemoryEntity(
        concept_id="coffee",
        canonical_name="咖啡偏好",
        aliases=[],
        memory_type="preference",
        identity_signature={
            "subject": "用户",
            "relation": "喜欢",
            "object": "咖啡",
        },
        summary=summary,
    )


class RevisionResolverTests(unittest.TestCase):

    def test_exact_duplicate_does_not_call_llm(self):
        llm = FakeLLM({})
        result = resolve_memory_revision(
            current_entity=entity(),
            candidate={"summary": "用户表示喜欢无糖咖啡。"},
            event_evidence={},
            llm=llm,
        )

        self.assertEqual(result["content_relation"], "duplicate")
        self.assertEqual(llm.invoke_count, 0)

    def test_revising_relation_requires_complete_summary(self):
        llm = FakeLLM({
            "content_relation": "extend",
            "new_information": "工作时会喝",
            "superseded_information": "",
            "revised_summary": "",
            "reason": "用户补充场景",
        })
        result = resolve_memory_revision(
            current_entity=entity(),
            candidate={"summary": "用户工作时喝无糖咖啡。"},
            event_evidence={"perception_event": {"content": "工作时会喝"}},
            llm=llm,
        )

        self.assertEqual(result["content_relation"], "uncertain")

    def test_conflict_cannot_return_revised_summary(self):
        llm = FakeLLM({
            "content_relation": "conflict",
            "new_information": "用户又说喜欢甜咖啡",
            "superseded_information": "",
            "revised_summary": "错误覆盖内容",
            "reason": "来源冲突",
        })
        result = resolve_memory_revision(
            current_entity=entity(),
            candidate={"summary": "用户表示喜欢甜咖啡。"},
            event_evidence={},
            llm=llm,
        )

        self.assertEqual(result["content_relation"], "conflict")
        self.assertEqual(result["revised_summary"], "")

    def test_prompt_separates_identity_from_content_revision(self):
        llm = FakeLLM({
            "content_relation": "replace",
            "new_information": "现在偏好无糖",
            "superseded_information": "以前偏好甜咖啡",
            "revised_summary": "用户表示过去偏好甜咖啡，现在偏好无糖咖啡。",
            "reason": "用户明确说明口味变化",
        })
        result = resolve_memory_revision(
            current_entity=entity("用户表示以前偏好甜咖啡。"),
            candidate={"summary": "用户表示现在偏好无糖咖啡。"},
            event_evidence={"perception_event": {"content": "现在口味变了"}},
            llm=llm,
        )

        self.assertEqual(result["content_relation"], "replace")
        self.assertIn("不判断身份", llm.last_prompt)
        self.assertIn("原始事件证据", llm.last_prompt)


if __name__ == "__main__":
    unittest.main()
