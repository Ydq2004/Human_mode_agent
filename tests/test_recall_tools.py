import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.experience import create_agent_action, create_experience_slice
from core.perception import create_perception_event
from memory import experience_store, recall_tools
from memory import sql_store
from memory.experience_store import StoredExperienceSlice
from memory.retrieval_engine import RetrievedMemory, RetrievalResult
from memory.schema import MemoryEntity


def _stored(event_id: str, content: str) -> StoredExperienceSlice:
    event = create_perception_event(
        source="user",
        modality="text",
        content=content,
        event_id=event_id,
        occurred_at="2026-08-24T10:00:00",
    )
    experience = create_experience_slice(
        perception_event=event,
        perception_understanding={},
        activated_memory_refs=[{"concept_id": "memory-1"}],
        response_or_actions=[create_agent_action("visible_response", "收到")],
        observations=[],
        state_snapshot={"mood": 50},
    )
    return StoredExperienceSlice(
        experience=experience,
        thread_id="thread-a",
        event_sequence=1,
        stored_at="2026-08-24T10:00:01",
        schema_version=1,
    )


class RecallToolTests(unittest.TestCase):
    def test_tool_reports_actual_data_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "coverage.db")
            first = _stored("evt-coverage-1", "较早经历")
            second_event = create_perception_event(
                source="user",
                modality="text",
                content="较晚经历",
                event_id="evt-coverage-2",
                occurred_at="2026-08-26T10:00:00",
            )
            second = StoredExperienceSlice(
                experience=create_experience_slice(
                    perception_event=second_event,
                    perception_understanding={},
                    activated_memory_refs=[],
                    response_or_actions=[],
                    observations=[],
                    state_snapshot={"mood": 50},
                ),
                thread_id="thread-b",
                event_sequence=1,
                stored_at="2026-08-26T10:00:01",
                schema_version=1,
            )
            with patch.object(experience_store, "SQLITE_DB_PATH", db_path), \
                    patch.object(sql_store, "SQLITE_DB_PATH", db_path):
                sql_store.create_tables()
                experience_store.save_experience_slice(
                    first.experience, thread_id="thread-a", event_sequence=1
                )
                experience_store.save_experience_slice(
                    second.experience, thread_id="thread-b", event_sequence=1
                )
                with patch.object(
                    recall_tools,
                    "retrieve_memories",
                    return_value=RetrievalResult(),
                ):
                    output = recall_tools.recall_memory.invoke(
                        {"query": "较晚经历", "thread_id": "thread-b"}
                    )

            data = json.loads(output)
            coverage = data["data_coverage"]
            self.assertEqual(coverage["experience_slices"]["count"], 1)
            self.assertEqual(
                coverage["experience_slices"]["earliest_at"],
                "2026-08-26T10:00:00",
            )
            self.assertEqual(
                coverage["experience_slices"]["latest_at"],
                "2026-08-26T10:00:00",
            )

    def test_recall_budget_blocks_eleventh_call(self):
        with recall_tools.recall_call_budget(max_calls=10):
            outputs = [
                recall_tools.recall_memory.invoke({"query": "不存在"})
                for _ in range(11)
            ]

        first_ten = [json.loads(item) for item in outputs[:10]]
        eleventh = json.loads(outputs[10])
        self.assertTrue(all(item["recall_call"]["status"] == "allowed" for item in first_ten))
        self.assertEqual(first_ten[-1]["recall_call"]["count"], 10)
        self.assertEqual(eleventh["result_status"], "call_limit_reached")
        self.assertEqual(eleventh["recall_call"]["count"], 10)

    def test_recall_budget_is_shared_by_parallel_tool_calls(self):
        def invoke_once(index):
            return json.loads(
                recall_tools.recall_memory.invoke({"query": f"并发查询-{index}"})
            )

        with recall_tools.recall_call_budget(max_calls=10):
            with ThreadPoolExecutor(max_workers=20) as executor:
                outputs = list(executor.map(invoke_once, range(20)))

        allowed = [item for item in outputs if item["recall_call"]["status"] == "allowed"]
        limited = [item for item in outputs if item["result_status"] == "call_limit_reached"]
        self.assertEqual(len(allowed), 10)
        self.assertEqual(len(limited), 10)
        self.assertEqual(sorted(item["recall_call"]["count"] for item in allowed), list(range(1, 11)))

    def test_tool_returns_memory_and_experience_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "experience.db")
            stored = _stored("evt-1", "用户提到了旧蛋糕")
            with patch.object(experience_store, "SQLITE_DB_PATH", db_path):
                experience_store.save_experience_slice(
                    stored.experience,
                    thread_id=stored.thread_id,
                    event_sequence=stored.event_sequence,
                )
                entity = MemoryEntity(
                    concept_id="memory-1",
                    canonical_name="蛋糕",
                    aliases=["蛋糕"],
                    memory_type="preference",
                    identity_signature={"subject": "主人"},
                    summary="用户提到过蛋糕",
                )
                retrieval = RetrievalResult(
                    accepted=[
                        RetrievedMemory(
                            entity=entity,
                            retrieval_sources=["alias"],
                        )
                    ]
                )
                with patch.object(
                    recall_tools,
                    "retrieve_memories",
                    return_value=retrieval,
                ):
                    output = recall_tools.recall_memory.invoke(
                        {
                            "query": "蛋糕",
                            "start_at": "2026-08-24T00:00:00",
                            "end_at": "2026-08-24T23:59:59",
                            "thread_id": "thread-a",
                            "limit": 5,
                        }
                    )

                data = json.loads(output)
                self.assertEqual(data["result_status"], "found")
                self.assertEqual(data["memories"][0]["concept_id"], "memory-1")
                self.assertEqual(data["experiences"][0]["event_id"], "evt-1")
                self.assertIn("只读", data["source_boundary"])
                self.assertEqual(len(experience_store.list_experience_slices()), 1)

    def test_concept_id_can_find_activated_experiences(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "experience.db")
            stored = _stored("evt-2", "没有概念名字的内容")
            with patch.object(experience_store, "SQLITE_DB_PATH", db_path):
                experience_store.save_experience_slice(stored.experience)
                with patch.object(recall_tools, "read_entity", return_value=None):
                    output = recall_tools.recall_memory.invoke(
                        {"concept_id": "memory-1", "limit": 5}
                    )
                data = json.loads(output)
                self.assertEqual(
                    [item["event_id"] for item in data["experiences"]],
                    ["evt-2"],
                )

    def test_slice_id_reads_only_the_requested_experience(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "experience.db")
            first = _stored("evt-5", "第一条")
            second = _stored("evt-6", "第二条")
            with patch.object(experience_store, "SQLITE_DB_PATH", db_path):
                experience_store.save_experience_slice(first.experience)
                experience_store.save_experience_slice(second.experience)
                output = recall_tools.recall_memory.invoke(
                    {"slice_id": second.experience.slice_id, "limit": 5}
                )
            data = json.loads(output)
            self.assertEqual(
                [item["event_id"] for item in data["experiences"]],
                ["evt-6"],
            )

    def test_tool_call_and_result_are_extracted_as_experience_records(self):
        class ToolCallMessage:
            tool_calls = [{"name": "recall_memory", "args": {"query": "蛋糕"}, "id": "call-1"}]
            type = "ai"

        class ToolResultMessage:
            type = "tool"
            name = "recall_memory"
            tool_call_id = "call-1"
            content = '{"result_status":"found"}'

        from main import _extract_tool_interactions

        actions, observations = _extract_tool_interactions(
            [ToolCallMessage(), ToolResultMessage()]
        )
        self.assertEqual(actions[0].action_type, "tool_call")
        self.assertIn("recall_memory", actions[0].content)
        self.assertEqual(observations[0].source, "tool")
        self.assertEqual(observations[0].modality, "memory_recall_result")

    def test_keyword_wildcards_are_treated_as_literal_text(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "experience.db")
            stored = _stored("evt-3", "包含百分号 % 的原始内容")
            other = _stored("evt-4", "另一条内容")
            with patch.object(experience_store, "SQLITE_DB_PATH", db_path):
                experience_store.save_experience_slice(stored.experience)
                experience_store.save_experience_slice(other.experience)
                rows = experience_store.search_experience_slices(keyword="%")
            self.assertEqual(
                [row.experience.perception_event.event_id for row in rows],
                ["evt-3"],
            )


if __name__ == "__main__":
    unittest.main()
