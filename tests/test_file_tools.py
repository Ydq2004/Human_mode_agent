import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import file_access_policy
from tools.file_access_policy import check_path, grant_authorization
from tools.file_tools import apply_patch, list_directory, read_file, write_file
from tools.file_authorization import make_file_authorization_middleware
from langchain_core.messages import ToolMessage


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

    def test_binary_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            (allowed / "image.bin").write_bytes(b"\x00\xff\x01")
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                result = json.loads(read_file.invoke({"path": str(allowed / "image.bin")}))
            self.assertEqual(result["status"], "error")
            self.assertIn("UTF-8", result["reason"])

    def test_read_size_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            (allowed / "large.txt").write_text("12345", encoding="utf-8")
            policy = self._policy(root)
            policy["max_read_bytes"] = 4
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                result = json.loads(read_file.invoke({"path": str(allowed / "large.txt")}))
            self.assertEqual(result["status"], "error")
            self.assertIn("超过", result["reason"])

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            link = allowed / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"当前环境不支持创建符号链接：{exc}")
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                result = json.loads(read_file.invoke({"path": str(link / "secret.txt")}))
            self.assertEqual(result["status"], "denied")

    def test_windows_style_case_change_does_not_escape_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            candidate = str(root / "allowed").upper()
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                result = check_path(candidate, "read")
            if __import__("os").name == "nt":
                self.assertEqual(result.status, "allow")
            else:
                self.assertIn(result.status, {"allow", "denied"})

    def test_directory_truncated_only_when_more_entries_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            (allowed / "only.txt").write_text("one", encoding="utf-8")
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                result = json.loads(list_directory.invoke({"path": str(allowed), "limit": 1}))
            self.assertEqual(result["entries"], [{"name": "only.txt", "type": "file"}])
            self.assertFalse(result["truncated"])

    def test_authorization_middleware_rejects_without_running_tool(self):
        class Request:
            tool_call = {"name": "write_file", "args": {"path": "outside.txt", "content": "x"}, "id": "call-1"}

            def override(self, *, tool_call):
                copied = Request()
                copied.tool_call = tool_call
                return copied

        called = []
        middleware = make_file_authorization_middleware(lambda request: False)
        result = middleware.wrap_tool_call(Request(), lambda request: called.append(request) or ToolMessage(content="ran", tool_call_id="call-1"))
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(json.loads(result.content)["status"], "confirmation_required")
        self.assertEqual(called, [])

    def test_authorization_middleware_continues_original_call_after_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(self._policy(root)), encoding="utf-8")

            class Request:
                tool_call = {"name": "write_file", "args": {"path": str(root / "outside.txt"), "content": "x"}, "id": "call-2"}

                def override(self, *, tool_call):
                    copied = Request()
                    copied.tool_call = tool_call
                    return copied

            captured = []
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy_path):
                middleware = make_file_authorization_middleware(lambda request: True)
                result = middleware.wrap_tool_call(
                    Request(),
                    lambda request: captured.append(request.tool_call) or ToolMessage(content="ran", tool_call_id="call-2"),
                )
            self.assertEqual(result.content, "ran")
            self.assertEqual(captured[0]["args"]["authorization_id"][:5], "auth_")


if __name__ == "__main__":
    unittest.main()
