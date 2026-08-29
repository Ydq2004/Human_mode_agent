import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory import experience_summary_store


def _summary(
    summary_id="daily-1",
    text="当天发生了两次互动",
    generated_at="2026-08-25T00:00:00+08:00",
):
    return experience_summary_store.ExperienceSummary(
        summary_id=summary_id,
        summary_type="daily",
        start_at="2026-08-24T00:00:00+08:00",
        end_at="2026-08-24T23:59:59+08:00",
        source_slice_ids=("slice-1", "slice-2"),
        summary_text=text,
        uncertainties=("关系变化尚未确认",),
        algorithm_version="summary-v1",
        prompt_version="prompt-v1",
        generated_at=generated_at,
    )


class ExperienceSummaryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "summary.db")
        self.patch = patch.object(
            experience_summary_store,
            "SQLITE_DB_PATH",
            self.db_path,
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_round_trip_preserves_sources_and_versions(self):
        summary = _summary()
        self.assertTrue(experience_summary_store.save_summary(summary))
        stored = experience_summary_store.get_summary(summary.summary_id)

        self.assertEqual(stored, summary)
        self.assertEqual(stored.source_slice_ids, ("slice-1", "slice-2"))
        self.assertEqual(stored.prompt_version, "prompt-v1")

    def test_duplicate_same_content_is_idempotent_and_changed_content_rejected(self):
        summary = _summary()
        self.assertTrue(experience_summary_store.save_summary(summary))
        self.assertFalse(experience_summary_store.save_summary(summary))
        self.assertFalse(
            experience_summary_store.save_summary(
                _summary(generated_at="2026-08-26T00:00:00+08:00")
            )
        )
        with self.assertRaises(ValueError):
            experience_summary_store.save_summary(
                _summary(text="不应覆盖旧摘要")
            )

    def test_summary_requires_source_slice_and_supports_period_filter(self):
        with self.assertRaises(ValueError):
            experience_summary_store.ExperienceSummary(
                summary_id="bad",
                summary_type="daily",
                start_at="2026-08-24",
                end_at="2026-08-24",
                source_slice_ids=(),
                summary_text="没有来源不合法",
                uncertainties=(),
                algorithm_version="v1",
                prompt_version=None,
                generated_at="2026-08-25",
            )

        experience_summary_store.save_summary(_summary())
        self.assertEqual(
            len(
                experience_summary_store.list_summaries(
                    summary_type="daily",
                    start_at="2026-08-24T12:00:00+08:00",
                    end_at="2026-08-24T13:00:00+08:00",
                )
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
