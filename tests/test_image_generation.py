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
        if "/view?" in request.full_url:
            class BinaryResponse:
                def read(self_inner):
                    return b"fake-png"
            return BinaryResponse()
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

    def test_optional_seed_keeps_template_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = root / "workflow.json"
            workflow.write_text(json.dumps({"3": {"inputs": {"text": "old"}}, "4": {"inputs": {"seed": 123}}}), encoding="utf-8")
            service = ImageGenerationService(
                workflow_path=workflow,
                output_root=root,
                field_map={"positive_prompt": {"node_id": "3", "input": "text"}, "seed": {"node_id": "4", "input": "seed"}},
            )
            built = service._build_workflow(prompt="new", negative_prompt="", width=None, height=None, seed=None)
            self.assertEqual(built["4"]["inputs"]["seed"], 123)

    def test_workflow_switch_uses_separate_maps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normal = root / "normal.json"
            upscale = root / "upscale.json"
            normal.write_text(json.dumps({
                "3": {"inputs": {"text": "old-negative"}},
                "6": {"inputs": {"width": 512, "height": 512}},
                "10": {"inputs": {"text": "old-positive", "seed": 1}},
            }), encoding="utf-8")
            upscale.write_text(json.dumps({
                "3": {"inputs": {"text": "old-negative"}},
                "12": {"inputs": {"width": 512, "height": 512}},
                "25": {"inputs": {"seed": 2}},
                "28": {"inputs": {"text": "old-positive"}},
            }), encoding="utf-8")
            service = ImageGenerationService(
                workflow_path=normal,
                upscale_workflow_path=upscale,
                output_root=root,
                field_map={
                    "positive_prompt": {"node_id": "10", "input": "text"},
                    "negative_prompt": {"node_id": "3", "input": "text"},
                    "width": {"node_id": "6", "input": "width"},
                    "height": {"node_id": "6", "input": "height"},
                    "seed": {"node_id": "10", "input": "seed"},
                },
                upscale_field_map={
                    "positive_prompt": {"node_id": "28", "input": "text"},
                    "negative_prompt": {"node_id": "3", "input": "text"},
                    "width": {"node_id": "12", "input": "width"},
                    "height": {"node_id": "12", "input": "height"},
                    "seed": {"node_id": "25", "input": "seed"},
                },
            )
            normal_built = service._build_workflow(prompt="normal", negative_prompt="none", width=768, height=1024, seed=7)
            upscale_built = service._build_workflow(prompt="upscale", negative_prompt="none", width=1024, height=768, seed=8, upscale=True)
            self.assertEqual(normal_built["10"]["inputs"]["text"], "normal")
            self.assertEqual(normal_built["6"]["inputs"]["width"], 768)
            self.assertEqual(upscale_built["28"]["inputs"]["text"], "upscale")
            self.assertEqual(upscale_built["12"]["inputs"]["width"], 1024)

    def test_missing_upscale_workflow_is_configuration_missing(self):
        service = ImageGenerationService(
            workflow_path=Path("does-not-exist.json"),
            upscale_workflow_path=Path("also-does-not-exist.json"),
            output_root=Path.cwd(),
        )
        result = service.generate(prompt="anything", upscale=True)
        self.assertEqual(result["status"], "configuration_missing")

    def test_tool_is_unavailable_without_bound_service(self):
        result = json.loads(generate_visual.invoke({"prompt": "anything"}))
        self.assertEqual(result["status"], "unavailable")
