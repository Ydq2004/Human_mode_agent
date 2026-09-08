import json
import tempfile
import unittest
from pathlib import Path

from core.comfyui_client import ComfyUIClient
from core.image_generation_service import ImageGenerationService
from tools.image_generation_tools import generate_visual, image_generation_context


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _FakeComfy:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request.get_method(), request.full_url, request.data))
        if request.full_url.endswith("/prompt"):
            return _Response({"prompt_id": "prompt-1"})
        return _Response({"prompt-1": {"outputs": {"9": {"images": [{"filename": self.output_path.name, "subfolder": "", "type": "output"}]}}}})


class ImageGenerationTests(unittest.TestCase):
    def test_service_submits_and_validates_real_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = root / "workflow.json"
            workflow.write_text(json.dumps({"3": {"inputs": {"text": "old", "seed": 1}}, "5": {"inputs": {"width": 512, "height": 512}}}), encoding="utf-8")
            output = root / "result.png"
            output.write_bytes(b"fake-png")
            fake = _FakeComfy(output)
            client = ComfyUIClient("http://comfy", timeout_seconds=2, poll_interval_seconds=0, opener=fake)
            service = ImageGenerationService(client, workflow_path=workflow, output_root=root, field_map={
                "positive_prompt": {"node_id": "3", "input": "text"}, "seed": {"node_id": "3", "input": "seed"},
                "width": {"node_id": "5", "input": "width"}, "height": {"node_id": "5", "input": "height"},
            })
            result = service.generate(prompt="a quiet room", width=512, height=512, seed=7)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["image"]["size_bytes"], len(b"fake-png"))
            posted = json.loads(fake.requests[0][2].decode("utf-8"))
            self.assertEqual(posted["prompt"]["3"]["inputs"]["text"], "a quiet room")
            self.assertEqual(posted["prompt"]["3"]["inputs"]["seed"], 7)

    def test_missing_workflow_is_configuration_missing(self):
        service = ImageGenerationService(workflow_path=Path("does-not-exist.json"), output_root=Path.cwd())
        result = service.generate(prompt="anything")
        self.assertEqual(result["status"], "configuration_missing")

    def test_invalid_dimensions_are_failed_without_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = root / "workflow.json"
            workflow.write_text("{}", encoding="utf-8")
            service = ImageGenerationService(workflow_path=workflow, output_root=root)
            result = service.generate(prompt="anything", width=65)
            self.assertEqual(result["status"], "failed")
            self.assertIn("width", result["message"])

    def test_tool_is_unavailable_without_bound_service(self):
        result = json.loads(generate_visual.invoke({"prompt": "anything"}))
        self.assertEqual(result["status"], "unavailable")
