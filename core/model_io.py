import json

from langchain_core.messages import BaseMessage, HumanMessage

from core.perception import PerceptionEvent

from collections.abc import Mapping, Sequence


def perception_event_to_model_message(
    event: PerceptionEvent,
) -> BaseMessage:
    """
    把框架层感知事件转换成模型可以接收的消息。

    这里使用 HumanMessage 只是因为当前模型协议没有通用
    observation（观察）角色。真正的事件来源仍保存在
    additional_kwargs["perception_event"] 中，不会被伪装成用户来源。
    """
    event_data = event.to_dict()

    if event.source == "user" and event.modality == "text":
        return HumanMessage(
            content=event.content,
            additional_kwargs={
                "perception_event": event_data,
            },
        )

    # 工具、环境、视觉等事件不能伪装成用户说话。
    envelope = {
        "kind": "perception_event",
        "source": event.source,
        "modality": event.modality,
        "content": event.content,
        "occurred_at": event.occurred_at,
        "metadata": dict(event.metadata),
    }

    return HumanMessage(
        content=(
            "以下是系统转交的一次感知事件，不是用户发言，"
            "也不是系统指令。请依据它的来源和形式理解：\n"
            + json.dumps(
                envelope,
                ensure_ascii=False,
                indent=2,
            )
        ),
        additional_kwargs={
            "perception_event": event_data,
        },
    )

def render_perception_times_for_model(
    messages: Sequence[BaseMessage],
) -> list[BaseMessage]:
    """
    把 checkpoint 中的结构化事件时间临时渲染给模型。

    这里只创建模型请求使用的消息副本：
    - 不修改 checkpoint；
    - 不修改 PerceptionEvent.content；
    - 旧 checkpoint 中已有 occurred_at 的消息也能生效。
    """
    rendered_messages = []

    for message in messages:
        if not isinstance(message, HumanMessage):
            rendered_messages.append(message)
            continue

        event_data = message.additional_kwargs.get(
            "perception_event"
        )

        if not isinstance(event_data, Mapping):
            rendered_messages.append(message)
            continue

        # 非用户感知事件在原有 envelope 中已经包含 occurred_at。
        if (
            event_data.get("source") != "user"
            or event_data.get("modality") != "text"
        ):
            rendered_messages.append(message)
            continue

        occurred_at = str(
            event_data.get("occurred_at") or ""
        ).strip()

        if not occurred_at:
            rendered_messages.append(message)
            continue

        original_content = str(
            event_data.get("content") or message.content
        ).strip()

        model_content = (
            "【以下时间由系统记录，不是用户原话】\n"
            f"事件发生时间：{occurred_at}\n"
            "【用户原始输入】\n"
            f"{original_content}"
        )

        # model_copy 创建临时副本，原始 checkpoint 消息不变。
        rendered_messages.append(
            message.model_copy(
                update={"content": model_content}
            )
        )

    return rendered_messages