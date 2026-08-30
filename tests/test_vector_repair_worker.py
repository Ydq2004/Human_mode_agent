import threading
import unittest

from core.vector_repair_worker import VectorRepairWorker


class VectorRepairWorkerTests(unittest.TestCase):
    def test_worker_runs_initial_check_and_can_stop(self):
        inspected = threading.Event()
        repaired = []

        def inspect():
            inspected.set()
            return {"missing_in_chroma": [], "orphaned_in_chroma": []}

        worker = VectorRepairWorker(
            inspect,
            lambda limit: repaired.append(limit) or [],
            interval_seconds=3600,
            batch_size=4,
        )
        try:
            self.assertTrue(inspected.wait(1.0))
            self.assertEqual(repaired, [4])
            self.assertTrue(worker.snapshot()["running"])
        finally:
            worker.shutdown()

        self.assertFalse(worker.snapshot()["running"])

    def test_worker_contains_failures_in_snapshot(self):
        worker = VectorRepairWorker(
            lambda: (_ for _ in ()).throw(RuntimeError("Chroma 不可用")),
            lambda limit: [],
            interval_seconds=3600,
        )
        try:
            for _ in range(20):
                if worker.snapshot()["last_error"]:
                    break
                threading.Event().wait(0.01)
            self.assertIn("Chroma 不可用", worker.snapshot()["last_error"])
        finally:
            worker.shutdown()


if __name__ == "__main__":
    unittest.main()
