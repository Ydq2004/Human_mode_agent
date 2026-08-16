import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.commit_worker import CommitTask, CommitWorker


class CommitWorkerTests(unittest.TestCase):

    def test_later_completion_cannot_overtake_earlier_sequence(self):
        order = []
        worker = CommitWorker(
            lambda task: order.append(task.job_id) or {"ok": True}
        )
        try:
            worker.submit(CommitTask("job-2", 2, "thread", {"status": "completed"}))
            worker.submit(CommitTask("job-1", 1, "thread", {"status": "completed"}))
            self.assertEqual(worker.wait("job-1", 2)["status"], "committed")
            self.assertEqual(worker.wait("job-2", 2)["status"], "committed")
            self.assertEqual(order, ["job-1", "job-2"])
        finally:
            worker.shutdown()

    def test_commit_failure_advances_watermark(self):
        order = []

        def commit(task):
            order.append(task.job_id)
            if task.job_id == "job-1":
                raise RuntimeError("写入失败")
            return {"ok": True}

        worker = CommitWorker(commit)
        try:
            worker.submit(CommitTask("job-1", 1, "thread", {}))
            worker.submit(CommitTask("job-2", 2, "thread", {}))
            self.assertEqual(worker.wait("job-1", 2)["status"], "commit_failed")
            self.assertEqual(worker.wait("job-2", 2)["status"], "committed")
            self.assertEqual(order, ["job-1", "job-2"])
        finally:
            worker.shutdown()

    def test_duplicate_job_id_is_idempotent(self):
        calls = []
        worker = CommitWorker(lambda task: calls.append(task.job_id) or {})
        try:
            task = CommitTask("same", 1, "thread", {})
            worker.submit(task)
            worker.submit(task)
            self.assertEqual(worker.wait("same", 2)["status"], "committed")
            self.assertTrue(worker.acknowledge("same"))
            worker.submit(task)
            self.assertEqual(calls, ["same"])
        finally:
            worker.shutdown()

    def test_old_sequence_cannot_be_reused_by_another_job(self):
        worker = CommitWorker(lambda task: {})
        try:
            worker.submit(CommitTask("job-1", 1, "thread", {}))
            self.assertEqual(worker.wait("job-1", 2)["status"], "committed")
            with self.assertRaises(ValueError):
                worker.submit(CommitTask("different", 1, "thread", {}))
        finally:
            worker.shutdown()

    def test_shutdown_marks_unfillable_sequence_explicitly(self):
        worker = CommitWorker(lambda task: {})
        worker.submit(CommitTask("job-2", 2, "thread", {}))
        worker.shutdown()
        self.assertEqual(worker.snapshot("job-2")["status"], "commit_failed")
        self.assertIn("MissingEventSequence", worker.snapshot("job-2")["error"])


if __name__ == "__main__":
    unittest.main()
