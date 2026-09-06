import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.model_io import perception_event_to_model_message
from core.model_io import render_native_images_for_model
from core.vision_input import create_image_event, inspect_image


class VisionInputTests(unittest.TestCase):
    def test_image_event_keeps_reference_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
            event = create_image_event(path)
            self.assertEqual(event.modality, "image")
            self.assertTrue(event.metadata["attachment_id"].startswith("img_"))
            self.assertNotIn("base64", event.to_dict())

    def test_unsupported_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.txt"
            path.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                inspect_image(path)

    def test_native_message_contains_temporary_image_block(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
            event = create_image_event(path)
            message = perception_event_to_model_message(event, native_vision=True)
            self.assertIsInstance(message.content, str)
            self.assertNotIn("base64", str(message.content))
            rendered = render_native_images_for_model([message])
            self.assertIsInstance(rendered[0].content, list)
            self.assertEqual(rendered[0].content[1]["type"], "image_url")

    def test_historical_native_image_is_not_expanded_for_new_event(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
            event = create_image_event(path)
            image_message = perception_event_to_model_message(event, native_vision=True)
            rendered = render_native_images_for_model(
                [image_message], current_event_id="evt-new-text"
            )
            self.assertIsInstance(rendered[0].content, str)


if __name__ == "__main__":
    unittest.main()
