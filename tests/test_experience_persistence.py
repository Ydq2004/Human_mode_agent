import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.experience import create_agent_action, create_experience_slice
from core.perception import create_perception_event
from memory import experience_store


def _make_slice(event_id: str, occurred_at: str):
    event = create_perception_event(
        source="user",
        modality="text",
        content=f"事件 {event_id}",
        event_id=event_id,
        occurred_at=occurred_at,
    )
    return create_experience_slice(
        perception_event=event,
        perception_understanding={"situated_understanding": "已理解"},
        activated_memory_refs=[{"concept_id": "memory-1"}],
        response_or_actions=[
            create_agent_action("visible_response", "收到")
        ],
        observations=[],
        state_snapshot={"mood": 50},
        capability_snapshot={"tools": []},
        memory_activation_state={"status": "normal"},
    )


class ExperiencePersistenceTests(unittest.TestCase):
    def test_round_trip_preserves_complete_slice(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "experience.db")
            experience = _make_slice("evt-1", "2026-08-17T10:00:00")

            with patch.object(experience_store, "SQLITE_DB_PATH", db_path):
                self.assertTrue(
                    experience_store.save_experience_slice(
                        experience,
                        thread_id="thread-a",
                        event_sequence=1,
                    )
                )
                stored = experience_store.get_experience_slice(
                    experience.slice_id
                )

            self.assertIsNotNone(stored)
            self.assertEqual(stored.thread_id, "thread-a")
            self.assertEqual(stored.event_sequence, 1)
            self.assertEqual(
                stored.experience.to_dict(),
                experience.to_dict(),
            )

    def test_duplicate_same_content_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "experience.db")
            experience = _make_slice("evt-2", "2026-08-17T10:01:00")

            with patch.object(experience_store, "SQLITE_DB_PATH", db_path):
                self.assertTrue(
                    experience_store.save_experience_slice(experience)
                )
                self.assertFalse(
                    experience_store.save_experience_slice(experience)
                )

                rows = experience_store.list_experience_slices()

            self.assertEqual(len(rows), 1)

    def test_time_range_returns_ordered_multiple_slices(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "experience.db")
            first = _make_slice("evt-3", "2026-08-17T10:02:00")
            second = _make_slice("evt-4", "2026-08-17T10:03:00")

            with patch.object(experience_store, "SQLITE_DB_PATH", db_path):
                experience_store.save_experience_slice(
                    second,
                    thread_id="thread-a",
                    event_sequence=2,
                )
                experience_store.save_experience_slice(
                    first,
                    thread_id="thread-a",
                    event_sequence=1,
                )
                rows = experience_store.list_experience_slices(
                    start_at="2026-08-17T10:02:00",
                    end_at="2026-08-17T10:03:00",
                    thread_id="thread-a",
                )

            self.assertEqual(
                [row.experience.perception_event.event_id for row in rows],
                ["evt-3", "evt-4"],
            )

    def test_same_slice_id_with_changed_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "experience.db")
            experience = _make_slice("evt-5", "2026-08-17T10:04:00")
            changed_event = create_perception_event(
                source="user",
                modality="text",
                content="被篡改的事件",
                event_id="evt-5-changed",
                occurred_at="2026-08-17T10:04:00",
            )
            changed = create_experience_slice(
                perception_event=changed_event,
                perception_understanding={},
                activated_memory_refs=[],
                response_or_actions=[],
                observations=[],
                state_snapshot={},
                capability_snapshot={},
                memory_activation_state={},
            )
            changed = type(changed)(
                slice_id=experience.slice_id,
                perception_event=changed.perception_event,
                perception_understanding=changed.perception_understanding,
                activated_memory_refs=changed.activated_memory_refs,
                response_or_actions=changed.response_or_actions,
                observations=changed.observations,
                state_snapshot=changed.state_snapshot,
                capability_snapshot=changed.capability_snapshot,
                memory_activation_state=changed.memory_activation_state,
                completed_at=changed.completed_at,
                preceding_context=changed.preceding_context,
            )

            with patch.object(experience_store, "SQLITE_DB_PATH", db_path):
                experience_store.save_experience_slice(experience)
                with self.assertRaises(ValueError):
                    experience_store.save_experience_slice(changed)


if __name__ == "__main__":
    unittest.main()
