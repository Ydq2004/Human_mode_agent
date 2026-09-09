import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402


class MediaArtifactTests(unittest.TestCase):
    def test_only_existing_files_inside_configured_output_root_are_exposed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "generated"
            output.mkdir()
            image = output / "job.png"
            image.write_bytes(b"png")
            outside = root / "secret.png"
            outside.write_bytes(b"secret")
            messages = [
                SimpleNamespace(
                    name="generate_visual",
                    content=json.dumps({
                        "status": "succeeded",
                        "prompt_id": "p1",
                        "image": {
                            "path": str(image),
                            "mime_type": "image/png",
                            "size_bytes": 3,
                        },
                    }),
                ),
                SimpleNamespace(
                    name="generate_visual",
                    content=json.dumps({
                        "status": "succeeded",
                        "image": {"path": str(outside)},
                    }),
                ),
            ]

            with patch.object(main, "IMAGE_GENERATION_OUTPUT_ROOT", output):
                artifacts = main._extract_media_artifacts(messages)

            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["filename"], "job.png")
            self.assertNotIn("path", artifacts[0])


if __name__ == "__main__":
    unittest.main()
