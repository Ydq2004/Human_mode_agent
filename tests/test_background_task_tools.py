import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.background_task_tools import (
    background_task_context,
    consult_advanced_model,
    get_background_task_status,
    start_code_review,
)
from core.advanced_model_service import run_code_review
from memory import background_task_store as store


class _FakeWorker:
    def __init__(self):
        self.submitted = []

    def submit(self, task_id):
        self.submitted.append(task_id)
        return True


class _FakeLLM:
    def invoke(self, messages):
        class Response:
            content = '{"answer":"ok","assumptions":[],"recommended_actions":[],"uncertainties":[]}'
        return Response()


class BackgroundTaskToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "tasks.db")
        self.patch_db = patch.object(store, "SQLITE_DB_PATH", self.db)
        self.patch_db.start()

    def tearDown(self):
        self.patch_db.stop()
        self.temp.cleanup()

    def test_consult_returns_unavailable_without_runtime(self):
        result = json.loads(consult_advanced_model.invoke({"question": "x"}))
        self.assertEqual(result["status"], "unavailable")

    def test_consult_uses_fake_model(self):
        with background_task_context(worker=None, event_bus=None, llm=_FakeLLM(), persona_instance_id="p", thread_id="t"):
            result = json.loads(consult_advanced_model.invoke({"question": "x"}))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"], "ok")

    def test_review_creation_is_scoped_and_idempotent(self):
        worker = _FakeWorker()
        # 项目根目录在默认 file_access_policy 的读取白名单内。
        with background_task_context(worker=worker, event_bus=None, llm=_FakeLLM(), persona_instance_id="p", thread_id="t"):
            first = json.loads(start_code_review.invoke({"focus": "检查", "paths": [str(Path.cwd())], "idempotency_key": "k"}))
            again = json.loads(start_code_review.invoke({"focus": "检查", "paths": [str(Path.cwd())], "idempotency_key": "k"}))
            status = json.loads(get_background_task_status.invoke({"task_id": first["task_id"]}))
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["task_id"], again["task_id"])
        self.assertEqual(status["status"], "queued")
        self.assertEqual(len(worker.submitted), 1)

    def test_review_rejects_path_outside_read_whitelist(self):
        worker = _FakeWorker()
        outside = str(Path(self.temp.name) / "outside.py")
        with background_task_context(worker=worker, event_bus=None, llm=_FakeLLM(), persona_instance_id="p", thread_id="t"):
            result = json.loads(start_code_review.invoke({"focus": "检查", "paths": [outside]}))
        self.assertEqual(result["status"], "denied")
        self.assertEqual(worker.submitted, [])

    def test_review_service_returns_a_report_without_write_tools(self):
        path = Path.cwd() / "tests" / "test_background_tasks.py"
        task = {"request": {"focus": "检查"}, "scope": {"paths": [str(path)]}}
        result = run_code_review(task, _FakeLLM())
        self.assertEqual(result["result_format"], "json")
        self.assertEqual(result["audit"]["files_read"], 0)


if __name__ == "__main__":
    unittest.main()
