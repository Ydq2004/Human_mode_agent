import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import file_access_policy
from tools.file_access_policy import check_path, grant_authorization
from tools.file_tools import apply_patch, list_directory, read_file, write_file


class FileToolTests(unittest.TestCase):
    def _policy(self, root: Path) -> dict:
        return {
            "read_roots": [str(root)],
            "write_roots": [str(root / "allowed")],
            "sensitive_patterns": [".env", "*.pem", "*.key"],
            "max_read_bytes": 10000,
            "max_write_bytes": 10000,
        }

    def test_path_policy_distinguishes_read_write_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "allowed").mkdir()
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            self.assertEqual(check_path(str(root / "note.txt"), "read", policy_path=policy_path).status, "allow")
            self.assertEqual(check_path(str(root / "note.txt"), "write", policy_path=policy_path).status, "confirmation_required")
            self.assertEqual(check_path(str(root / "allowed" / "note.txt"), "write", policy_path=policy_path).status, "allow")
            self.assertEqual(check_path(str(root / "allowed" / ".." / ".." / "outside.txt"), "read", policy_path=policy_path).status, "denied")

    def test_sensitive_file_is_denied_even_inside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            self.assertEqual(check_path(str(root / ".env"), "read", policy_path=policy_path).status, "denied")

    def test_read_list_write_and_unique_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                self.assertEqual(json.loads(write_file.invoke({"path": str(allowed / "a.txt"), "content": "Hello World"}))["status"], "success")
                read = json.loads(read_file.invoke({"path": str(allowed / "a.txt")}))
                self.assertEqual(read["content"], "Hello World")
                listing = json.loads(list_directory.invoke({"path": str(allowed)}))
                self.assertEqual(listing["entries"][0]["name"], "a.txt")
                patched = json.loads(apply_patch.invoke({"path": str(allowed / "a.txt"), "old_content": "World", "new_content": "Agent"}))
                self.assertEqual(patched["status"], "success")
                self.assertEqual((allowed / "a.txt").read_text(encoding="utf-8"), "Hello Agent")

    def test_patch_rejects_ambiguous_content_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                write_file.invoke({"path": str(allowed / "a.txt"), "content": "x x"})
                result = json.loads(apply_patch.invoke({"path": str(allowed / "a.txt"), "old_content": "x", "new_content": "y"}))
                self.assertEqual(result["status"], "error")
                self.assertIn("多次", result["reason"])
                self.assertEqual((allowed / "a.txt").read_text(encoding="utf-8"), "x x")

    def test_outside_write_requires_and_accepts_external_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            outside = root / "outside.txt"
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                first = json.loads(write_file.invoke({"path": str(outside), "content": "confirmed"}))
                self.assertEqual(first["status"], "confirmation_required")
                token = grant_authorization(first["operation_id"], str(outside), "write")
                second = json.loads(write_file.invoke({"path": str(outside), "content": "confirmed", "authorization_id": token}))
                self.assertEqual(second["status"], "success")
                self.assertEqual(outside.read_text(encoding="utf-8"), "confirmed")
                replay = json.loads(write_file.invoke({"path": str(outside), "content": "replay", "authorization_id": token}))
                self.assertEqual(replay["status"], "confirmation_required")

    def test_file_tool_result_uses_generic_observation_type(self):
        from main import _extract_tool_interactions

        class ToolCallMessage:
            tool_calls = [{"name": "read_file", "args": {"path": "docs/a.md"}, "id": "call-file-1"}]
            type = "ai"

        class ToolResultMessage:
            type = "tool"
            name = "read_file"
            tool_call_id = "call-file-1"
            content = '{"operation":"read","status":"success"}'

        _, observations = _extract_tool_interactions([ToolCallMessage(), ToolResultMessage()])
        self.assertEqual(observations[0].modality, "tool_result")
        self.assertEqual(observations[0].metadata["tool_name"], "read_file")


if __name__ == "__main__":
    unittest.main()
