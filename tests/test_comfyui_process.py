import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.comfyui_process import ComfyUIProcessManager, ComfyUIStartupError


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"system": "ok"}).encode("utf-8")


class _Process:
    pid = 42

    def __init__(self, running=True):
        self.running = running
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated = True
        self.running = False

    def kill(self):
        self.killed = True
        self.running = False

    def wait(self, timeout=None):
        return 0


class ComfyUIProcessTests(unittest.TestCase):
    def test_existing_service_is_reused_and_not_owned(self):
        manager = ComfyUIProcessManager(
            base_url="http://127.0.0.1:8188",
            executable="python",
            opener=lambda *args, **kwargs: _Response(),
            popen=Mock(),
        )

        status = manager.start_if_needed()

        self.assertEqual(status.state, "ready")
        self.assertFalse(status.owned)
        manager._popen.assert_not_called()

    def test_missing_executable_does_not_start_unknown_process(self):
        manager = ComfyUIProcessManager(
            base_url="http://comfy",
            opener=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
        )

        status = manager.start_if_needed()

        self.assertEqual(status.state, "unavailable")
        self.assertFalse(status.owned)

    def test_owned_process_is_terminated_only_when_configured(self):
        process = _Process()
        health_calls = {"count": 0}

        def opener(*args, **kwargs):
            health_calls["count"] += 1
            if health_calls["count"] == 1:
                raise OSError("not started yet")
            return _Response()

        with tempfile.TemporaryDirectory() as temp:
            manager = ComfyUIProcessManager(
                base_url="http://comfy",
                executable="python",
                args=("comfy.py",),
                log_root=Path(temp),
                opener=opener,
                popen=lambda *args, **kwargs: process,
                stop_on_exit=True,
            )
            status = manager.start_if_needed()
            self.assertTrue(status.owned)
            manager.stop()

        self.assertTrue(process.terminated)

    def test_start_failure_records_and_raises(self):
        process = _Process(running=False)
        with tempfile.TemporaryDirectory() as temp:
            manager = ComfyUIProcessManager(
                base_url="http://comfy",
                executable="python",
                log_root=Path(temp),
                opener=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
                popen=lambda *args, **kwargs: process,
                startup_timeout_seconds=0.1,
                poll_interval_seconds=0,
            )
            with self.assertRaises(ComfyUIStartupError):
                manager.start_if_needed()
            self.assertEqual(manager.status().state, "failed")


if __name__ == "__main__":
    unittest.main()
