import sys
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.appraisal_worker import AppraisalWorker
from core.commit_worker import CommitTask, CommitWorker
from core.experience import create_experience_slice
from core.perception import create_perception_event
from main import (
    _build_capability_snapshot,
    _consume_finished_appraisal_jobs,
    _consume_finished_commit_jobs,
    process_perception_event,
)


class FakeMessage:
    content = "收到。"


class FakeAgent:
    def invoke(self, payload, config):
        return {"messages": [FakeMessage()]}


class FakeAppraisal:
    def to_dict(self):
        return {"status": "fake"}


class MainAppraisalIntegrationTests(unittest.TestCase):

    def test_completed_appraisal_reaches_commit_without_main_thread_drain(self):
        committed = Event()
        commit_worker = CommitWorker(
            lambda task: committed.set() or {"written": True}
        )
        appraisal_worker = AppraisalWorker(
            object(),
            appraise_fn=lambda **kwargs: FakeAppraisal(),
            effects_fn=lambda **kwargs: {"mood": {"mood_impact": 0}},
            on_terminal=lambda job: commit_worker.submit(CommitTask(
                job_id=job["job_id"],
                event_sequence=job["event_sequence"],
                thread_id=job["thread_id"],
                appraisal_job=job,
            )),
        )
        try:
            job_id = appraisal_worker.submit(
                experience=create_experience_slice(
                    perception_event=create_perception_event(
                        "user", "text", "即时转交测试"
                    ),
                    perception_understanding={},
                    activated_memory_refs=[],
                    response_or_actions=[],
                    observations=[],
                    state_snapshot={"owner": "agent", "mood": 50},
                ),
                persona_context={},
                mood_reactivity=1.0,
                event_sequence=1,
                thread_id="new-thread",
            )

            self.assertTrue(committed.wait(timeout=2))
            self.assertEqual(
                commit_worker.wait(job_id, timeout=2)["status"],
                "committed",
            )
            # 整个过程中没有调用 appraisal_worker.drain_finished()。
            self.assertEqual(appraisal_worker.snapshot(job_id)["status"], "completed")
        finally:
            appraisal_worker.shutdown(wait=True)
            commit_worker.shutdown(wait=True)

    def test_appraisal_is_retained_until_commit_reaches_terminal_state(self):
        appraisal_worker = AppraisalWorker(
            object(),
            appraise_fn=lambda **kwargs: FakeAppraisal(),
            effects_fn=lambda **kwargs: {"mood": {"mood_impact": 0}},
        )
        commit_worker = CommitWorker(lambda task: {"written": True})
        try:
            job_id = appraisal_worker.submit(
                experience=create_experience_slice(
                    perception_event=create_perception_event("user", "text", "测试"),
                    perception_understanding={},
                    activated_memory_refs=[],
                    response_or_actions=[],
                    observations=[],
                    state_snapshot={"owner": "agent", "mood": 50},
                ),
                persona_context={},
                mood_reactivity=1.0,
            )
            appraisal_worker.wait(job_id, timeout=2)
            contexts = {
                job_id: {"event_sequence": 1, "thread_id": "thread"}
            }

            _consume_finished_appraisal_jobs(
                appraisal_worker,
                commit_worker,
                contexts,
            )
            self.assertEqual(appraisal_worker.stats()["retained"], 1)
            self.assertEqual(commit_worker.wait(job_id, 2)["status"], "committed")

            _consume_finished_commit_jobs(appraisal_worker, commit_worker)
            self.assertEqual(appraisal_worker.stats()["retained"], 0)
            self.assertEqual(commit_worker.stats()["retained"], 0)
        finally:
            appraisal_worker.shutdown(wait=True)
            commit_worker.shutdown(wait=True)

    def test_capability_snapshot_keeps_memory_under_framework_control(self):
        snapshot = _build_capability_snapshot([])
        memory_control = snapshot["long_term_memory_control"]

        self.assertEqual(
            memory_control["managed_by"],
            "background_framework",
        )
        self.assertFalse(memory_control["agent_can_write"])
        self.assertFalse(memory_control["agent_can_delete"])
        self.assertFalse(
            memory_control["result_available_during_response"]
        )

    @patch("main.build_agent_context")
    @patch("main.begin_perception_event")
    def test_foreground_returns_before_background_appraisal_finishes(
        self,
        begin_event,
        build_context,
    ):
        begin_event.return_value = {"mood": 50, "energy": 100}
        build_context.return_value = {
            "injection_text": "",
            "perception_understanding": {
                "situated_understanding": "普通问候",
                "knowledge_scope": {
                    "mode": "not_applicable",
                    "allowed_domain_matches": [],
                    "restricted_topics": [],
                    "reason": "普通问候不需要外部知识",
                },
                "understanding_status": "normal",
                "memory_activation_cues": [],
                "uncertainties": [],
            },
            "activated_memory_refs": [],
            "memory_activation_state": {
                "status": "normal",
                "exhaustive": False,
                "absence_means_not_exists": False,
            },
            "retrieval_debug": [],
            "timings": {
                "understanding_seconds": 0.01,
                "retrieval_seconds": 0.02,
            },
        }

        appraisal_started = Event()
        appraisal_release = Event()
        fake_appraisal = FakeAppraisal()
        fake_effects = {"mood": {"mood_impact": 0}}

        def appraise_fn(**kwargs):
            appraisal_started.set()
            appraisal_release.wait(timeout=2)
            return fake_appraisal

        def effects_fn(**kwargs):
            return fake_effects

        worker = AppraisalWorker(
            object(),
            appraise_fn=appraise_fn,
            effects_fn=effects_fn,
        )

        try:
            result = process_perception_event(
                create_perception_event("user", "text", "你好"),
                agent=FakeAgent(),
                agent_config={
                    "configurable": {"thread_id": "test_thread"},
                },
                understanding_llm=object(),
                retry_understanding_llm=object(),
                appraisal_worker=worker,
                thread_id="test_thread",
                persona={
                    "agent_name": "测试角色",
                    "emotion_profile": {"mood_reactivity": 0.8},
                },
                recent_context=[],
                capability_snapshot={},
            )

            self.assertTrue(appraisal_started.wait(timeout=1))
            self.assertEqual(result["visible_reply"], "收到。")
            self.assertEqual(result["appraisal_job"]["status"], "pending")
            self.assertNotIn("experience_appraisal", result)
            self.assertNotIn("appraisal_effects", result)
            self.assertGreaterEqual(result["timings"]["main_agent_seconds"], 0)

            appraisal_release.set()
            completed = worker.wait(result["appraisal_job_id"], timeout=2)

            self.assertEqual(completed["status"], "completed")
            self.assertIs(completed["appraisal"], fake_appraisal)
            self.assertEqual(completed["effects"], fake_effects)
            self.assertIn("appraisal_seconds", completed["timings"])
            self.assertIn("rules_seconds", completed["timings"])

            _consume_finished_appraisal_jobs(worker)
            self.assertEqual(worker.stats()["retained"], 0)
            self.assertEqual(worker.stats()["futures"], 0)
        finally:
            appraisal_release.set()
            worker.shutdown(wait=True)

        begin_event.assert_called_once_with("test_thread")


if __name__ == "__main__":
    unittest.main()
