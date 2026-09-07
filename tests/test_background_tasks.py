import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.background_task_events import BackgroundTaskEventBus
from core.background_task_worker import BackgroundTaskWorker
from memory import background_task_store as store


class BackgroundTaskFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "tasks.db")
        self.patcher = patch.object(store, "SQLITE_DB_PATH", self.db)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def test_idempotent_create_and_legal_transitions(self):
        first = store.create_task(
            task_type="test", execution_mode="async", request={"x": 1},
            idempotency_key="same-request", max_attempts=1,
        )
        again = store.create_task(
            task_type="test", execution_mode="async", request={"x": 1},
            idempotency_key="same-request", max_attempts=1,
        )
        self.assertEqual(first["task_id"], again["task_id"])
        self.assertEqual(store.get_task(first["task_id"])["status"], "queued")
        store.transition_task(first["task_id"], "running", expected_status="queued")
        store.transition_task(first["task_id"], "succeeded", expected_status="running", result={"ok": True})
        self.assertEqual(store.get_task(first["task_id"])["result"], {"ok": True})
        with self.assertRaises(ValueError):
            store.transition_task(first["task_id"], "running")

    def test_worker_persists_completion_and_notification(self):
        bus = BackgroundTaskEventBus()
        task = store.create_task(
            task_type="echo", execution_mode="async", request={"value": "done"},
        )
        worker = BackgroundTaskWorker({"echo": lambda item: {"summary": item["request"]["value"]}}, event_bus=bus)
        try:
            worker.submit(task["task_id"])
            deadline = time.time() + 3
            while time.time() < deadline and store.get_task(task["task_id"])["status"] not in {"succeeded", "failed", "exhausted"}:
                time.sleep(0.02)
            saved = store.get_task(task["task_id"])
            self.assertEqual(saved["status"], "succeeded")
            self.assertEqual(saved["result"]["summary"], "done")
            events = bus.drain()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["status"], "succeeded")
            self.assertTrue(bus.acknowledge(events[0]["event_id"]))
            self.assertEqual(store.list_pending_events(), [])
        finally:
            worker.shutdown()

    def test_failed_task_retries_then_exhausts(self):
        attempts = []
        bus = BackgroundTaskEventBus()
        task = store.create_task(
            task_type="fail", execution_mode="async",
            request={"retry_delay_seconds": 0}, max_attempts=2,
        )
        def fail(_):
            attempts.append(True)
            raise RuntimeError("broken")
        worker = BackgroundTaskWorker({"fail": fail}, event_bus=bus)
        try:
            worker.submit(task["task_id"])
            deadline = time.time() + 4
            while time.time() < deadline and store.get_task(task["task_id"])["status"] != "exhausted":
                time.sleep(0.05)
            self.assertEqual(store.get_task(task["task_id"])["status"], "exhausted")
            self.assertEqual(len(attempts), 2)
        finally:
            worker.shutdown()


if __name__ == "__main__":
    unittest.main()
