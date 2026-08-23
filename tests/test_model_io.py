import sys
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.model_io import (
    perception_event_to_model_message,
    render_perception_times_for_model,
)
from core.perception import create_perception_event


class ModelIoTimeRenderingTests(unittest.TestCase):
    def test_current_user_event_keeps_original_content_and_carries_time(self):
        event = create_perception_event(
            source="user",
            modality="text",
            content="你还记得昨天吗？",
            event_id="evt-time-1",
            occurred_at="2026-08-23T20:00:00+08:00",
        )
        message = perception_event_to_model_message(event)
        rendered = render_perception_times_for_model([message])

        self.assertEqual(message.content, "你还记得昨天吗？")
        self.assertEqual(rendered[0].additional_kwargs, message.additional_kwargs)
        self.assertIn("2026-08-23T20:00:00+08:00", rendered[0].content)
        self.assertIn("你还记得昨天吗？", rendered[0].content)

    def test_rendering_does_not_mutate_checkpoint_message(self):
        message = HumanMessage(
            content="原始内容",
            additional_kwargs={
                "perception_event": {
                    "source": "user",
                    "modality": "text",
                    "content": "原始内容",
                    "occurred_at": "2026-08-22T09:30:00+08:00",
                }
            },
        )
        original_content = message.content
        rendered = render_perception_times_for_model([message])

        self.assertIsNot(rendered[0], message)
        self.assertEqual(message.content, original_content)
        self.assertEqual(rendered[0].additional_kwargs, message.additional_kwargs)

    def test_old_checkpoint_event_is_rendered_and_non_event_messages_are_unchanged(self):
        old_event = HumanMessage(
            content="旧消息内容",
            additional_kwargs={
                "perception_event": {
                    "source": "user",
                    "modality": "text",
                    "content": "旧消息内容",
                    "occurred_at": "2026-08-21T12:00:00",
                }
            },
        )
        ordinary = HumanMessage(content="没有事件元数据")
        assistant = AIMessage(content="之前的回复")

        rendered = render_perception_times_for_model(
            [old_event, ordinary, assistant]
        )

        self.assertIn("2026-08-21T12:00:00", rendered[0].content)
        self.assertEqual(rendered[1], ordinary)
        self.assertEqual(rendered[2], assistant)

    def test_non_user_event_already_has_time_envelope(self):
        event = create_perception_event(
            source="environment",
            modality="text",
            content="房间变暗了",
            event_id="evt-time-2",
            occurred_at="2026-08-24T08:00:00+08:00",
        )
        message = perception_event_to_model_message(event)
        rendered = render_perception_times_for_model([message])

        self.assertEqual(rendered[0].content, message.content)
        self.assertIn("2026-08-24T08:00:00+08:00", message.content)


if __name__ == "__main__":
    unittest.main()
