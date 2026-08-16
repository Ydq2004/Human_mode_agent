import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from emotion.appraisal_rules import compute_committed_mood_change
from emotion.manager import commit_mood_effect
from emotion.models import EmotionState


class MoodCommitRuleTests(unittest.TestCase):

    def test_zero_impact_regresses_high_mood_one_step(self):
        result = compute_committed_mood_change(70, 0)
        self.assertEqual(result["new_mood"], 69)
        self.assertEqual(result["baseline_regression"], -1)
        self.assertEqual(result["applied_change"], -1)

    def test_zero_impact_regresses_low_mood_one_step(self):
        result = compute_committed_mood_change(30, 0)
        self.assertEqual(result["new_mood"], 31)
        self.assertEqual(result["baseline_regression"], 1)
        self.assertEqual(result["applied_change"], 1)

    def test_zero_impact_at_baseline_does_not_change_mood(self):
        result = compute_committed_mood_change(50, 0)
        self.assertEqual(result["new_mood"], 50)
        self.assertEqual(result["baseline_regression"], 0)
        self.assertEqual(result["applied_change"], 0)

    def test_nonzero_impact_does_not_also_regress(self):
        result = compute_committed_mood_change(70, 2)
        self.assertEqual(result["new_mood"], 72)
        self.assertEqual(result["event_impact"], 2)
        self.assertEqual(result["baseline_regression"], 0)
        self.assertEqual(result["applied_change"], 2)

    def test_invalid_impact_is_rejected_instead_of_recomputed(self):
        for value in (11, -11, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    compute_committed_mood_change(50, value)

    def test_commit_writes_regressed_mood_in_one_database_transaction(self):
        """用临时 SQLite 验证真实写入，不接触 Card_slot 实例数据库。"""
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "emotion_test.db"
            test_engine = create_engine(
                f"sqlite:///{database_path.as_posix()}"
            )
            SQLModel.metadata.create_all(test_engine)

            with Session(test_engine) as session:
                session.add(EmotionState(
                    thread_id="test-thread",
                    mood=70,
                    energy=100,
                ))
                session.commit()

            with patch("emotion.manager.engine", test_engine):
                result = commit_mood_effect("test-thread", 0)

            with Session(test_engine) as session:
                saved = session.exec(
                    select(EmotionState).where(
                        EmotionState.thread_id == "test-thread"
                    )
                ).first()

            self.assertEqual(result["old_mood"], 70)
            self.assertEqual(result["new_mood"], 69)
            self.assertEqual(result["baseline_regression"], -1)
            self.assertEqual(saved.mood, 69)
            test_engine.dispose()


if __name__ == "__main__":
    unittest.main()
