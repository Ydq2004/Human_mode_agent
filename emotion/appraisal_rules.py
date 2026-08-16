"""ExperienceAppraisal 的纯情绪计算规则，不读写数据库。"""

from math import ceil, floor, log1p
from typing import Any

from config import (
    MOOD_BASELINE,
    MOOD_EVENT_VALENCE_BIAS,
    MOOD_IMPACT_MAX,
    MOOD_IMPACT_MIN,
    MOOD_MAX,
    MOOD_MIN,
    MOOD_REACTIVITY_MAX,
    MOOD_REACTIVITY_MIN,
    MOOD_REGRESSION_RATE,
    MOOD_SALIENCE_FACTOR,
    EMOTION_SCORE_INITIAL_BASE,
    EMOTION_SCORE_INITIAL_MAX,
    EMOTION_SCORE_INITIAL_MIN,
    EVENT_INITIAL_BIAS,
    MEMORY_INITIAL_BIAS_FACTOR,
    MOOD_INITIAL_BIAS_FACTOR,
    PERSONA_INITIAL_BIAS,
    STRENGTH_SCORE_RANGES,
    EMOTION_SCORE_MIN,
    EMOTION_SCORE_MAX,
    EMOTION_AFFECT_DIRECTION_DELTA,
)


def round_half_away_from_zero(value: float) -> int:
    """0.5 远离零舍入，避免 Python round 的银行家舍入。"""
    if value >= 0:
        return floor(value + 0.5)
    return ceil(value - 0.5)


def compute_committed_mood_change(
    current_mood: int,
    mood_impact: int,
) -> dict[str, int]:
    """计算 Step 6 这一次提交真正应应用的 mood 变化。

    ``mood_impact`` 已由 Step 5 完成事件效价、显著性、人格反应幅度、
    边界阻尼和单次上限计算。这里不重新解释事件，也不再次做阻尼：

    - 有非零事件冲击时，只应用该冲击；
    - 没有事件冲击时，才向 ``MOOD_BASELINE`` 回归一步；
    - 已经位于基线时保持不变。

    这是纯规则函数，不访问数据库，便于单独验证所有边界。
    """
    if isinstance(current_mood, bool) or not isinstance(current_mood, int):
        raise ValueError("current_mood 必须是整数")
    if not MOOD_MIN <= current_mood <= MOOD_MAX:
        raise ValueError("current_mood 超出框架范围")
    if isinstance(mood_impact, bool) or not isinstance(mood_impact, int):
        raise ValueError("mood_impact 必须是整数")
    if not MOOD_IMPACT_MIN <= mood_impact <= MOOD_IMPACT_MAX:
        raise ValueError("mood_impact 超出 Step 5 单次范围")

    baseline_regression = 0
    if mood_impact == 0:
        if current_mood > MOOD_BASELINE:
            baseline_regression = -min(
                MOOD_REGRESSION_RATE,
                current_mood - MOOD_BASELINE,
            )
        elif current_mood < MOOD_BASELINE:
            baseline_regression = min(
                MOOD_REGRESSION_RATE,
                MOOD_BASELINE - current_mood,
            )

    applied_change = mood_impact + baseline_regression
    new_mood = max(
        MOOD_MIN,
        min(MOOD_MAX, current_mood + applied_change),
    )

    return {
        "old_mood": current_mood,
        "new_mood": new_mood,
        "event_impact": mood_impact,
        "baseline_regression": baseline_regression,
        "applied_change": new_mood - current_mood,
    }


def _mood_boundary_factor(
    current_mood: float,
    direction: float,
) -> float:
    if direction == 0:
        return 1.0

    if direction > 0:
        available_room = MOOD_MAX - current_mood
        baseline_room = MOOD_MAX - MOOD_BASELINE
    else:
        available_room = current_mood - MOOD_MIN
        baseline_room = MOOD_BASELINE - MOOD_MIN

    if baseline_room <= 0:
        raise ValueError("mood baseline 配置非法")

    available_room = max(0.0, available_room)

    # 只衰减，不允许因为远离边界而额外放大。
    return min(
        1.0,
        log1p(available_room) / log1p(baseline_room),
    )



def compute_mood_impact(
    emotion_assessment: dict[str, Any],
    current_mood: float,
    mood_reactivity: float,
) -> dict[str, Any]:
    current_mood = float(current_mood)
    mood_reactivity = float(mood_reactivity)

    if not MOOD_MIN <= current_mood <= MOOD_MAX:
        raise ValueError("current_mood 超出框架范围")

    if not (
        MOOD_REACTIVITY_MIN
        <= mood_reactivity
        <= MOOD_REACTIVITY_MAX
    ):
        raise ValueError("mood_reactivity 超出角色配置范围")

    relevance = emotion_assessment.get("event_relevance")
    valence = emotion_assessment.get("event_valence")
    salience = emotion_assessment.get("salience")

    # 无关或低相关事件不能改变 mood。
    if relevance in {"none", "low"}:
        return {
            "mood_impact": 0,
            "raw_impact": 0.0,
            "boundary_factor": 1.0,
            "was_clipped": False,
        }

    if valence not in MOOD_EVENT_VALENCE_BIAS:
        raise ValueError("event_valence 非法")
    if salience not in MOOD_SALIENCE_FACTOR:
        raise ValueError("salience 非法")

    base_delta = MOOD_EVENT_VALENCE_BIAS[valence]
    salience_factor = MOOD_SALIENCE_FACTOR[salience]

    raw_impact = (
        base_delta
        * salience_factor
        * mood_reactivity
    )

    boundary_factor = _mood_boundary_factor(
        current_mood,
        raw_impact,
    ) #计算将要被影响的边界效应因子
    damped_impact = raw_impact * boundary_factor #加入边界效应的影响
    rounded_impact = round_half_away_from_zero(damped_impact) #使用四舍五入(python原生使用银行家算法,强行保留为偶数)

    mood_impact = max(
        MOOD_IMPACT_MIN,
        min(MOOD_IMPACT_MAX, rounded_impact),
    )

    return {
        "mood_impact": mood_impact,
        "raw_impact": raw_impact,
        "boundary_factor": boundary_factor,
        "was_clipped": mood_impact != rounded_impact,
    }


def _clamp(
    value: float,
    minimum: float,
    maximum: float,
) -> float:
    return max(minimum, min(maximum, value))


CONSERVATIVE_EMOTION_LABELS = {
    ("neutral", "neutral"): "中性",
    ("positive", "slight"): "轻微正向",
    ("positive", "moderate"): "中度正向",
    ("positive", "strong"): "强烈正向",
    ("negative", "slight"): "轻微负向",
    ("negative", "moderate"): "中度负向",
    ("negative", "strong"): "强烈负向",
}


def _derive_label_shape(
    emotion_score: float,
) -> tuple[str, str]:
    """根据权威分数推导标签应有的极性和强度。"""
    delta = float(emotion_score) - EMOTION_SCORE_INITIAL_BASE

    if delta == 0:
        return "neutral", "neutral"

    polarity = "positive" if delta > 0 else "negative"
    distance = abs(delta)

    if distance <= STRENGTH_SCORE_RANGES["slight"][1]:
        strength = "slight"
    elif distance <= STRENGTH_SCORE_RANGES["moderate"][1]:
        strength = "moderate"
    else:
        strength = "strong"

    return polarity, strength


def resolve_emotion_label(
    emotion_score: float,
    label_update: Any,
) -> str:
    """
    校验 LLM 的标签建议。

    分数决定极性和强度。建议与分数一致时采用其文字，
    否则保留分数并生成框架层的保守标签。
    """
    polarity, strength = _derive_label_shape(emotion_score)

    if isinstance(label_update, dict):
        label = label_update.get("label")

        if (
            isinstance(label, str)
            and label.strip()
            and label_update.get("polarity") == polarity
            and label_update.get("strength") == strength
        ):
            return label.strip()

    return CONSERVATIVE_EMOTION_LABELS[
        (polarity, strength)
    ]

def compute_existing_memory_emotion_update(
    current_score: float,
    affected_memory: dict[str, Any],
) -> dict[str, Any]:
    """
    根据经过清洗的 affected_memory 计算已有认知的新情绪分。

    事实候选不会进入这里，只有显式情绪评估才能修改 emotion_score。
    """
    try:
        current_score = float(current_score)
    except (TypeError, ValueError):
        raise ValueError("current_score 非法")

    if not EMOTION_SCORE_MIN <= current_score <= EMOTION_SCORE_MAX:
        raise ValueError("current_score 超出范围")

    direction = affected_memory.get("change_direction")
    if direction not in EMOTION_AFFECT_DIRECTION_DELTA:
        raise ValueError("change_direction 非法")

    score_delta = EMOTION_AFFECT_DIRECTION_DELTA[direction]
    new_score = round(
        _clamp(
            current_score + score_delta,
            EMOTION_SCORE_MIN,
            EMOTION_SCORE_MAX,
        ),
        2,
    )

    return {
        "emotion_score": new_score,
        "emotion_label": resolve_emotion_label(
            new_score,
            affected_memory.get("label_update"),
        ),
        "score_delta": round(new_score - current_score, 2),
    }



def compute_initial_memory_impression(
    impression: dict[str, Any],
    event_valence: str,
    activated_memory_refs: list[dict[str, Any]],
    mood_at_event_start: float,
) -> dict[str, Any]:
    """计算新认知的初始情绪分，并校验标签建议。"""
    if impression.get("fallback_to_neutral"):
        return {
            "emotion_score": EMOTION_SCORE_INITIAL_BASE,
            "emotion_label": "中性",
            "used_fallback": True,
            "components": {
                "event_bias": 0.0,
                "persona_bias": 0.0,
                "memory_bias": 0.0,
                "mood_bias": 0.0,
            },
        }

    candidate_valence = impression.get("candidate_valence")
    effective_valence = (
        candidate_valence
        if candidate_valence in EVENT_INITIAL_BIAS
        else event_valence
    )

    if effective_valence not in EVENT_INITIAL_BIAS:
        raise ValueError("新认知 event_valence 非法")

    persona_effect = impression.get(
        "persona_effect",
        "neutral",
    )
    if persona_effect not in PERSONA_INITIAL_BIAS:
        raise ValueError("persona_effect 非法")

    score_by_id = {}

    for ref in activated_memory_refs:
        concept_id = str(
            ref.get("concept_id", "")
        ).strip()

        try:
            score = float(ref.get("emotion_score"))
        except (TypeError, ValueError):
            continue

        if concept_id and 0.0 <= score <= 100.0:
            score_by_id[concept_id] = score

    related_scores = []

    for concept_id in impression.get(
        "direct_related_concept_ids",
        [],
    ):
        if concept_id in score_by_id:
            related_scores.append(
                score_by_id[concept_id]
            )

    if related_scores:
        direct_related_score = (
            sum(related_scores) / len(related_scores)
        )
    else:
        direct_related_score = 50.0

    event_bias = EVENT_INITIAL_BIAS[effective_valence]
    persona_bias = PERSONA_INITIAL_BIAS[persona_effect]

    memory_bias = _clamp(
        (
            direct_related_score
            - EMOTION_SCORE_INITIAL_BASE
        ) * MEMORY_INITIAL_BIAS_FACTOR,
        -10.0,
        10.0,
    )

    mood_bias = _clamp(
        (
            float(mood_at_event_start)
            - MOOD_BASELINE
        ) * MOOD_INITIAL_BIAS_FACTOR,
        -5.0,
        5.0,
    )

    raw_score = (
        EMOTION_SCORE_INITIAL_BASE
        + event_bias
        + persona_bias
        + memory_bias
        + mood_bias
    )

    emotion_score = round(
        _clamp(
            raw_score,
            EMOTION_SCORE_INITIAL_MIN,
            EMOTION_SCORE_INITIAL_MAX,
        ),
        2,
    )

    return {
        "emotion_score": emotion_score,
        "emotion_label": resolve_emotion_label(
            emotion_score,
            impression.get("label_update")
        ),
        "used_fallback": False,
        "components": {
            "event_bias": event_bias,
            "persona_bias": persona_bias,
            "memory_bias": memory_bias,
            "mood_bias": mood_bias,
        },
    }
