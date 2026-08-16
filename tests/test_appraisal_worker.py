import sys
from pathlib import Path
from threading import Event
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.appraisal_worker import AppraisalWorker
from core.experience import create_agent_action, create_experience_slice
from core.perception import create_perception_event


def create_experience(content: str):
    return create_experience_slice(
        perception_event=create_perception_event("user", "text", content),
        perception_understanding={},
        activated_memory_refs=[],
        response_or_actions=[
            create_agent_action("visible_response", "收到。"),
        ],
        observations=[],
        state_snapshot={"owner": "agent", "mood": 50, "energy": 100},
    )


class AppraisalWorkerTests(unittest.TestCase):

    def test_terminal_callback_receives_result_immediately(self):
        delivered = Event()
        snapshots = []
        worker = AppraisalWorker(
            object(),
            appraise_fn=lambda **kwargs: object(),
            effects_fn=lambda **kwargs: {},
            on_terminal=lambda snapshot: (
                snapshots.append(snapshot),
                delivered.set(),
            ),
        )
        try:
            job_id = worker.submit(
                experience=create_experience("立即转交"),
                persona_context={},
                mood_reactivity=1.0,
                event_sequence=7,
                thread_id="thread-7",
            )

            self.assertTrue(delivered.wait(timeout=2))
            self.assertEqual(snapshots[0]["job_id"], job_id)
            self.assertEqual(snapshots[0]["event_sequence"], 7)
            self.assertEqual(snapshots[0]["thread_id"], "thread-7")
            self.assertIn("perception_event", snapshots[0]["event_evidence"])
        finally:
            worker.shutdown(wait=True)

    def test_jobs_run_in_fifo_order(self):
        order = []

        def appraise_fn(**kwargs):
            order.append(kwargs["experience"].perception_event.content)
            return object()

        worker = AppraisalWorker(
            object(),
            appraise_fn=appraise_fn,
            effects_fn=lambda **kwargs: {},
        )

        try:
            first = worker.submit(
                experience=create_experience("第一条"),
                persona_context={},
                mood_reactivity=1.0,
            )
            second = worker.submit(
                experience=create_experience("第二条"),
                persona_context={},
                mood_reactivity=1.0,
            )

            self.assertEqual(worker.wait(first, timeout=2)["status"], "completed")
            self.assertEqual(worker.wait(second, timeout=2)["status"], "completed")
            self.assertEqual(order, ["第一条", "第二条"])
        finally:
            worker.shutdown(wait=True)

    def test_worker_failure_is_visible(self):
        def appraise_fn(**kwargs):
            raise RuntimeError("测试失败")

        worker = AppraisalWorker(
            object(),
            appraise_fn=appraise_fn,
            effects_fn=lambda **kwargs: {},
        )

        try:
            job_id = worker.submit(
                experience=create_experience("失败事件"),
                persona_context={},
                mood_reactivity=1.0,
            )
            result = worker.wait(job_id, timeout=2)

            self.assertEqual(result["status"], "failed")
            self.assertIn("RuntimeError", result["error"])
            self.assertIsNone(result["appraisal"])
            self.assertIsNone(result["effects"])
        finally:
            worker.shutdown(wait=True)

    def test_consumed_result_releases_job_and_future(self):
        worker = AppraisalWorker(
            object(),
            appraise_fn=lambda **kwargs: object(),
            effects_fn=lambda **kwargs: {},
        )

        try:
            job_id = worker.submit(
                experience=create_experience("释放测试"),
                persona_context={},
                mood_reactivity=1.0,
            )
            worker.wait(job_id, timeout=2)

            delivered = worker.drain_finished()

            self.assertEqual([item["job_id"] for item in delivered], [job_id])
            self.assertEqual(worker.stats()["retained"], 1)
            self.assertEqual(worker.stats()["futures"], 1)

            self.assertTrue(worker.acknowledge(job_id))
            self.assertEqual(worker.stats()["retained"], 0)
            self.assertEqual(worker.stats()["futures"], 0)
            self.assertIsNone(worker.snapshot(job_id))
        finally:
            worker.shutdown(wait=True)

    def test_many_consumed_jobs_do_not_accumulate(self):
        worker = AppraisalWorker(
            object(),
            appraise_fn=lambda **kwargs: object(),
            effects_fn=lambda **kwargs: {},
        )

        try:
            for index in range(100):
                job_id = worker.submit(
                    experience=create_experience(f"长期运行-{index}"),
                    persona_context={},
                    mood_reactivity=1.0,
                )
                worker.wait(job_id, timeout=2)
                delivered = worker.drain_finished()
                self.assertEqual(len(delivered), 1)
                self.assertTrue(worker.acknowledge(job_id))

            self.assertEqual(worker.stats()["retained"], 0)
            self.assertEqual(worker.stats()["futures"], 0)
        finally:
            worker.shutdown(wait=True)

    def test_pending_job_cannot_be_acknowledged(self):
        started = Event()
        release = Event()

        def appraise_fn(**kwargs):
            started.set()
            release.wait(timeout=2)
            return object()

        worker = AppraisalWorker(
            object(),
            appraise_fn=appraise_fn,
            effects_fn=lambda **kwargs: {},
        )

        try:
            job_id = worker.submit(
                experience=create_experience("进行中"),
                persona_context={},
                mood_reactivity=1.0,
            )
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(worker.acknowledge(job_id))
            self.assertEqual(worker.stats()["retained"], 1)

            release.set()
            worker.wait(job_id, timeout=2)
            worker.drain_finished()
            self.assertTrue(worker.acknowledge(job_id))
        finally:
            release.set()
            worker.shutdown(wait=True)

    def test_queue_capacity_produces_explicit_failure(self):
        started = Event()
        release = Event()

        def appraise_fn(**kwargs):
            started.set()
            release.wait(timeout=2)
            return object()

        worker = AppraisalWorker(
            object(),
            appraise_fn=appraise_fn,
            effects_fn=lambda **kwargs: {},
            max_in_flight=1,
        )

        try:
            first = worker.submit(
                experience=create_experience("第一条"),
                persona_context={},
                mood_reactivity=1.0,
            )
            self.assertTrue(started.wait(timeout=1))

            second = worker.submit(
                experience=create_experience("第二条"),
                persona_context={},
                mood_reactivity=1.0,
            )
            rejected = worker.wait(second, timeout=0)

            self.assertEqual(rejected["status"], "failed")
            self.assertIn("AppraisalQueueFull", rejected["error"])
            self.assertEqual(worker.stats()["pending"], 1)

            delivered = worker.drain_finished()
            self.assertEqual([item["job_id"] for item in delivered], [second])
            self.assertTrue(worker.acknowledge(second))

            release.set()
            worker.wait(first, timeout=2)
            worker.drain_finished()
            self.assertTrue(worker.acknowledge(first))
        finally:
            release.set()
            worker.shutdown(wait=True)

    def test_failed_consumer_can_release_for_redelivery(self):
        worker = AppraisalWorker(
            object(),
            appraise_fn=lambda **kwargs: object(),
            effects_fn=lambda **kwargs: {},
        )

        try:
            job_id = worker.submit(
                experience=create_experience("重新交付"),
                persona_context={},
                mood_reactivity=1.0,
            )
            worker.wait(job_id, timeout=2)

            self.assertEqual(len(worker.drain_finished()), 1)
            self.assertEqual(worker.drain_finished(), [])
            self.assertTrue(worker.release_delivery(job_id))
            self.assertEqual(len(worker.drain_finished()), 1)
            self.assertTrue(worker.acknowledge(job_id))
        finally:
            worker.shutdown(wait=True)

    def test_shutdown_is_idempotent_and_rejects_new_jobs(self):
        worker = AppraisalWorker(
            object(),
            appraise_fn=lambda **kwargs: object(),
            effects_fn=lambda **kwargs: {},
        )

        worker.shutdown(wait=True)
        worker.shutdown(wait=True)

        self.assertTrue(worker.stats()["closed"])
        with self.assertRaises(RuntimeError):
            worker.submit(
                experience=create_experience("关闭后提交"),
                persona_context={},
                mood_reactivity=1.0,
            )

    def test_shutdown_can_cancel_queued_jobs_with_visible_failure(self):
        started = Event()
        release = Event()

        def appraise_fn(**kwargs):
            started.set()
            release.wait(timeout=2)
            return object()

        worker = AppraisalWorker(
            object(),
            appraise_fn=appraise_fn,
            effects_fn=lambda **kwargs: {},
            max_in_flight=2,
        )

        first = worker.submit(
            experience=create_experience("正在运行"),
            persona_context={},
            mood_reactivity=1.0,
        )
        self.assertTrue(started.wait(timeout=1))
        second = worker.submit(
            experience=create_experience("尚未开始"),
            persona_context={},
            mood_reactivity=1.0,
        )

        worker.shutdown(wait=False, cancel_futures=True)

        cancelled = worker.wait(second, timeout=0)
        self.assertEqual(cancelled["status"], "failed")
        self.assertIn("AppraisalCancelled", cancelled["error"])

        release.set()
        worker.shutdown(wait=True)
        self.assertEqual(worker.wait(first, timeout=2)["status"], "completed")

        delivered = worker.drain_finished()
        self.assertEqual(
            {item["job_id"] for item in delivered},
            {first, second},
        )
        for item in delivered:
            self.assertTrue(worker.acknowledge(item["job_id"]))

    def test_cancelled_queued_job_is_handed_off_as_failed(self):
        started = Event()
        release = Event()
        handed_off = []

        def appraise_fn(**kwargs):
            started.set()
            release.wait(timeout=2)
            return object()

        worker = AppraisalWorker(
            object(),
            appraise_fn=appraise_fn,
            effects_fn=lambda **kwargs: {},
            on_terminal=handed_off.append,
            max_in_flight=2,
        )
        first = worker.submit(
            experience=create_experience("运行中"),
            persona_context={},
            mood_reactivity=1.0,
            event_sequence=1,
            thread_id="thread",
        )
        self.assertTrue(started.wait(timeout=1))
        second = worker.submit(
            experience=create_experience("待取消"),
            persona_context={},
            mood_reactivity=1.0,
            event_sequence=2,
            thread_id="thread",
        )

        worker.shutdown(wait=False, cancel_futures=True)
        failed = [item for item in handed_off if item["job_id"] == second]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], "failed")
        self.assertEqual(failed[0]["event_sequence"], 2)

        release.set()
        worker.shutdown(wait=True)
        self.assertEqual(worker.wait(first, timeout=2)["status"], "completed")

    def test_invalid_capacity_is_rejected(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    AppraisalWorker(object(), max_in_flight=value)


if __name__ == "__main__":
    unittest.main()
