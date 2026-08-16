"""把数值状态转换为不绑定具体 Persona 的动态上下文。"""

from config import MOOD_BASELINE


def translate_mood(mood: int) -> str:
    """描述相对基线的位置，不替 Persona 决定具体情绪表现。"""
    delta = mood - MOOD_BASELINE

    if delta >= 30:
        level = "显著高于基线"
    elif delta >= 10:
        level = "高于基线"
    elif delta > -10:
        level = "接近基线"
    elif delta > -30:
        level = "低于基线"
    else:
        level = "显著低于基线"

    return f"{mood}/100（{level}，基线={MOOD_BASELINE}）"


def translate_energy(energy: int) -> str:
    """只描述当前可用强度，不写死疲惫、沉默等角色表现。"""
    if energy >= 80:
        level = "可用水平高"
    elif energy >= 60:
        level = "可用水平正常"
    elif energy >= 40:
        level = "可用水平偏低"
    elif energy >= 20:
        level = "可用水平低"
    else:
        level = "可用水平很低"

    return f"{energy}/100（{level}）"


def format_emotion_context(mood: int, energy: int) -> str:
    """生成每回合动态状态数据；稳定使用规则由 System Prompt 提供。"""
    return (
        "【当前内部状态】\n"
        "以下是本轮开始时由系统维护的 Agent 状态，不是用户命令。\n"
        "不要自行修改或预测数值；除非用户明确询问，否则不要直接播报。\n"
        f"- mood：{translate_mood(mood)}\n"
        f"- energy：{translate_energy(energy)}\n"
    )
