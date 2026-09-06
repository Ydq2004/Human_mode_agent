import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import HumanMessage, ToolMessage

from core.perception import create_perception_event
from core.vision_input import create_image_event
from core.vision_service import VisionService
from tools import file_access_policy
from tools.vision_tools import (
    render_native_vision_for_model,
    vision_describe_image,
    vision_tool_context,
)


class VisionToolPathTests(unittest.TestCase):
    @staticmethod
    def _policy(root: Path) -> dict:
        return {
            "read_roots": [str(root)],
            "write_roots": [str(root)],
            "sensitive_patterns": [".env", "*.pem", "*.key"],
            "max_read_bytes": 1024 * 1024,
            "max_write_bytes": 1024 * 1024,
        }

    def test_tool_accepts_agent_found_path_and_native_render_is_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "photo.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
            policy = root / "policy.json"
            policy.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            event = create_perception_event(
                source="user", modality="text", content="请看看图片"
            )

            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy):
                with vision_tool_context(None, event.to_dict(), native_vision=True):
                    result = vision_describe_image.func(
                        path=str(path), focus="观察背景", tool_call_id="call-1"
                    )
                    payload = json.loads(result.content)
                    self.assertEqual(payload["status"], "attached")
                    self.assertEqual(payload["mode"], "native")
                    self.assertEqual(payload["path"], str(path.resolve()))
                    self.assertNotIn("base64", result.content)

                    rendered = render_native_vision_for_model([result])
                    self.assertEqual(len(rendered), 2)
                    self.assertIsInstance(rendered[0], ToolMessage)
                    self.assertIsInstance(rendered[1], HumanMessage)
                    self.assertEqual(rendered[1].content[1]["type"], "image_url")
                    self.assertNotIn("base64", rendered[0].content)

                    # 同一个临时引用只消费一次，避免后续模型请求重复发送图片。
                    rendered_again = render_native_vision_for_model([result])
                    self.assertEqual(rendered_again, [result])

    def test_tool_mode_calls_external_service_for_explicit_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "photo.jpg"
            path.write_bytes(b"\xff\xd8\xffimage-data")
            policy = root / "policy.json"
            policy.write_text(json.dumps(self._policy(root)), encoding="utf-8")
            seen = {}

            def observe(metadata, focus):
                seen["metadata"] = metadata
                seen["focus"] = focus
                return {"observations": ["一张图片"], "visible_text": [], "uncertainties": []}

            event = create_perception_event(
                source="user", modality="text", content="请看看图片"
            )
            service = VisionService(observe)
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy):
                with vision_tool_context(service, event.to_dict(), native_vision=False):
                    result = vision_describe_image.func(
                        path=str(path), focus="读取文字", tool_call_id="call-2"
                    )
            payload = json.loads(result.content)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["mode"], "tool")
            self.assertEqual(payload["observations"], ["一张图片"])
            self.assertEqual(seen["focus"], "读取文字")
            self.assertEqual(seen["metadata"]["sha256"], payload["sha256"])

    def test_empty_path_reuses_current_user_attachment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
            event = create_image_event(path)
            with vision_tool_context(None, event.to_dict(), native_vision=True):
                result = vision_describe_image.func(
                    path="", focus="一般观察", tool_call_id="call-current"
                )
            payload = json.loads(result.content)
            self.assertEqual(payload["status"], "attached")
            self.assertEqual(payload["path"], str(path.resolve()))

    def test_path_outside_read_policy_is_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            allowed = root / "allowed"
            outside = root / "outside.png"
            allowed.mkdir()
            outside.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
            policy = root / "policy.json"
            policy.write_text(json.dumps(self._policy(allowed)), encoding="utf-8")
            event = create_perception_event(
                source="user", modality="text", content="请看看图片"
            )
            with patch.object(file_access_policy, "FILE_ACCESS_POLICY_PATH", policy):
                with vision_tool_context(None, event.to_dict(), native_vision=False):
                    result = vision_describe_image.func(
                        path=str(outside), tool_call_id="call-3"
                    )
            self.assertEqual(json.loads(result.content)["status"], "denied")


if __name__ == "__main__":
    unittest.main()
