import unittest
import time
from pathlib import Path
import tempfile
from core.vision_service import VisionService, llm_image_observer
from core.vision_input import inspect_image


class VisionServiceTests(unittest.TestCase):
    def test_observer_output_is_limited_to_observations(self):
        service = VisionService(lambda path, focus: {"observations": ["桌子"], "visible_text": [], "uncertainties": ["身份未知"], "relationship": "friend"})
        result = service.describe("x.png")
        self.assertEqual(result["status"], "success")
        self.assertNotIn("relationship", result)

    def test_missing_service_is_explicit(self):
        self.assertEqual(VisionService().describe("x")["status"], "unavailable")

    def test_llm_observer_accepts_plain_text_from_compatible_endpoint(self):
        class FakeResponse:
            content = "画面中可见一名人物，其他细节无法确认。"

        class FakeLLM:
            def __init__(self):
                self.messages = None

            def invoke(self, messages):
                self.messages = messages
                return FakeResponse()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.jpg"
            path.write_bytes(b"\xff\xd8\xffimage-data")
            metadata = inspect_image(path)
            llm = FakeLLM()
            result = llm_image_observer(llm)(metadata, "")

        self.assertEqual(result["observations"], ["画面中可见一名人物，其他细节无法确认。"])
        self.assertEqual(result["visible_text"], [])
        self.assertEqual(len(llm.messages), 1)
        self.assertEqual(llm.messages[0].content[1]["type"], "image_url")

    def test_service_timeout_is_explicit(self):
        def slow_observer(path, focus):
            time.sleep(0.05)
            return {"observations": ["late"]}

        result = VisionService(slow_observer, timeout_seconds=0.001).describe("x.png")
        self.assertEqual(result["status"], "timeout")


if __name__ == "__main__":
    unittest.main()
