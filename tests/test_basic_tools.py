import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.calculation_tools import calculate
from tools.environment_tools import get_current_time
from tools.file_access_policy import FILE_ACCESS_POLICY_PATH
from tools.process_tools import execute_command
from tools.search_tools import search_text
from tools.metadata_tools import read_image_metadata


class BasicToolTests(unittest.TestCase):
    def test_calculator_rejects_code(self):
        self.assertNotEqual(json.loads(calculate.invoke({"expression": "__import__('os')"}))["status"], "success")

    def test_time_validates_timezone(self):
        self.assertEqual(json.loads(get_current_time.invoke({"timezone": "bad/zone"}))["status"], "invalid_input")

    def test_search_and_metadata_respect_policy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "note.txt").write_text("hello Agent\n", encoding="utf-8")
            (root / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (2).to_bytes(4, "big") + (3).to_bytes(4, "big"))
            policy = root / "policy.json"
            policy.write_text(json.dumps({"read_roots": [str(root)], "write_roots": [], "sensitive_patterns": [], "max_read_bytes": 10000, "max_write_bytes": 10000}), encoding="utf-8")
            with patch("tools.file_access_policy.FILE_ACCESS_POLICY_PATH", policy):
                self.assertEqual(len(json.loads(search_text.invoke({"query": "Agent", "path": str(root)}))["result"]["matches"]), 1)
                meta = json.loads(read_image_metadata.invoke({"path": str(root / "a.png")}))
                self.assertEqual((meta["result"]["width"], meta["result"]["height"]), (2, 3))

    def test_search_rejects_parent_glob(self):
        result = json.loads(search_text.invoke({"query": "x", "glob": "../*.txt"}))
        self.assertEqual(result["status"], "invalid_input")

    def test_execute_command_is_restricted(self):
        denied = json.loads(execute_command.invoke({"argv": ["powershell", "Get-Date"]}))
        self.assertEqual(denied["status"], "denied")
        output_file = Path(tempfile.gettempdir()) / "agent-tool-diff.txt"
        denied_write = json.loads(execute_command.invoke({"argv": ["git", "diff", f"--output={output_file}"]}))
        self.assertEqual(denied_write["status"], "denied")

    def test_background_command_requires_runtime(self):
        result = json.loads(execute_command.invoke({"argv": ["python", "--version"], "background": True}))
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
