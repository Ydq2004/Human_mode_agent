"""Human-mode Agent 的框架级运行配置。

角色性格、语气和知识边界放在 ``Card_slot/persona_config.json``；
这里只保存所有角色共用的路径、模型调用、检索、情绪和后台队列参数。
"""

import os
from pathlib import Path


# ========== 项目路径 ==========
# 角色卡是可替换的；当前实例数据统一放在 Card_slot/memory_db 中。
PROJECT_ROOT = Path(__file__).parent
PERSONA_CONFIG_PATH = PROJECT_ROOT / "Card_slot" / "persona_config.json"
CHROMA_PERSIST_DIR = str(PROJECT_ROOT / "Card_slot" / "memory_db" / "chroma_db")
SQLITE_DB_PATH = str(PROJECT_ROOT / "Card_slot" / "memory_db" / "sql_db.db")
CHECKPOINT_DB_PATH = str(PROJECT_ROOT / "Card_slot" / "memory_db" / "checkpoint.db")


# ========== LLM 默认值与网络容错 ==========
# 角色卡可以覆盖模型名称、地址和主 Agent 温度；这些值只负责兜底。
DEFAULT_LLM_CONFIG = {
    "provider": "deepseek",
    "model_name": "deepseek-v4-flash",
    "temperature": 0.8,
    "base_url": "https://api.deepseek.com",
}

# 每个 LLM 请求必须有截止时间，避免后台线程退出时无限等待。
MAIN_LLM_TIMEOUT_SECONDS = 90.0
UNDERSTANDING_LLM_TIMEOUT_SECONDS = 50.0
APPRAISAL_LLM_TIMEOUT_SECONDS = 90.0
LLM_MAX_RETRIES = 1

# API key 只从环境变量读取，不写入代码或角色卡。
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")


# ========== Embedding 与记忆检索 ==========
EMBEDDING_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
RETRIEVAL_K = 5


# ========== 情绪运行状态 ==========
MOOD_MIN = 0
MOOD_MAX = 100
MOOD_BASELINE = 50

# 无事件冲击时，每次提交向基线回归的步长。
MOOD_REGRESSION_RATE = 1
# 离线期间每分钟向基线回归的步长。
OFFLINE_MOOD_REGRESSION_PER_MINUTE = 0.1
# 离线期间每分钟恢复的体力。
ENERGY_RECOVERY_PER_MINUTE = 0.3
# 单轮事件造成的 mood 变化上下限。
# 这是最后一道裁剪，不参与前面的乘法。即使公式算出 +12，最终也只应用 +10。
# 调大：允许单次事件造成更剧烈的心情变化；调小：心情更稳定，但强事件差异会被压平。
MOOD_IMPACT_MIN = -10
MOOD_IMPACT_MAX = 10

# 本轮 mood 的“事件效价基础值”。它只改变短期全局 mood，不直接决定新认知的情绪分。
# 实际计算：
#   raw_impact = MOOD_EVENT_VALENCE_BIAS[event_valence]
#                * MOOD_SALIENCE_FACTOR[salience]
#                * persona_config.json 中的 mood_reactivity
# 之后还会经过接近 0/100 时的边界衰减、四舍五入和 MOOD_IMPACT_MIN/MAX 裁剪。
# 例：mild_positive(4) * medium(0.6) * reactivity(1.0) = +2.4，四舍五入为 +2。
# 调大某一项：该类事件更容易推动 mood；正负值必须保持方向一致。
MOOD_EVENT_VALENCE_BIAS = {
    "strong_positive": 8,
    "mild_positive": 4,
    "neutral": 0,
    "mild_negative": -4,
    "strong_negative": -8,
}

# 事件“显著性”对 mood 的倍率。它只调节幅度，不创造正负方向。
# high=1.0 表示保留全部事件基础值；medium=0.6 表示保留 60%；low=0.3 表示保留 30%。
# 注意：event_relevance 为 none 或 low 时，代码会直接令 mood_impact=0，届时本表不会参与计算。
# 调大 medium/low：普通事件也更容易积累 mood 波动；调小：只有突出事件才明显影响 mood。
MOOD_SALIENCE_FACTOR = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
}


# ========== 认知对象的情绪印记 ==========
# 新认知从 50 分开始，正常初始化结果限制在 30~70；已有认知后续可在 0~100 变化。
# 初始分完整公式：
#   initial_score = 50
#                   + EVENT_INITIAL_BIAS[事件效价]
#                   + PERSONA_INITIAL_BIAS[与 Persona 的契合程度]
#                   + clamp((相关旧认知平均分 - 50) * MEMORY_INITIAL_BIAS_FACTOR, -10, +10)
#                   + clamp((事件开始时 mood - 50) * MOOD_INITIAL_BIAS_FACTOR, -5, +5)
# 最后将结果裁剪到 EMOTION_SCORE_INITIAL_MIN/MAX。
EMOTION_SCORE_INITIAL_BASE = 50.0
EMOTION_SCORE_INITIAL_MIN = 30.0
EMOTION_SCORE_INITIAL_MAX = 70.0
EMOTION_SCORE_MIN = 0.0
EMOTION_SCORE_MAX = 100.0

# 新认知初始印象中的“当前事件”加减分。
# 它和 MOOD_EVENT_VALENCE_BIAS 名字相近，但职责不同：
# - MOOD_EVENT_VALENCE_BIAS 改本轮短期全局 mood；
# - EVENT_INITIAL_BIAS 改本轮创建的新认知对该对象的初始情绪分。
# 如果多条候选的方向不同，候选自己的 candidate_valence 优先；否则使用整轮 event_valence。
# 调大：新认知更容易因第一次事件形成鲜明印象，并更容易撞到 30/70 边界。
EVENT_INITIAL_BIAS = {
    "strong_positive": 10.0,
    "mild_positive": 5.0,
    "neutral": 0.0,
    "mild_negative": -5.0,
    "strong_negative": -10.0,
}

# 新认知与角色当前 Persona 是否契合所产生的初始加减分。
# fitting 表示这条新认知符合角色价值倾向；conflicting 表示冲突；neutral 表示证据不足或无关。
# 这是对“角色如何解释事件”的修正，不是事件本身的好坏，也不修改 Persona。
# 调大绝对值：初始人格对新认知印象的影响更强；调小：事件、旧认知和 mood 占比相对提高。
PERSONA_INITIAL_BIAS = {
    "fitting": 5.0,
    "neutral": 0.0,
    "conflicting": -5.0,
}

# 新认知初始印象中的已有认知偏置系数。
# 只读取 direct_related_concept_ids 指向且本轮确实检索到的旧认知，先求 emotion_score 平均值。
# 例：相关旧认知平均 70 分，(70 - 50) * 0.2 = +4；没有有效相关认知时按 50 分，贡献 0。
# 结果另行限制在 -10~+10。调大后，新认知更容易继承相关旧认知的情绪倾向。
MEMORY_INITIAL_BIAS_FACTOR = 0.2

# 新认知初始印象中的短期 mood 偏置系数。
# 使用事件开始时的 mood，而不是评价完成后的 mood，避免后台执行时序改变结果。
# 例：mood=60 时，(60 - 50) * 0.1 = +1；结果另行限制在 -5~+5。
# 调大后，新认知更容易被当时心情“染色”；过大可能让同一事实随短期心情产生过强差异。
MOOD_INITIAL_BIAS_FACTOR = 0.1

# LLM 提议的 strength 必须和最终分数偏离基线的幅度大致一致。
STRENGTH_SCORE_RANGES = {
    "slight": (0.0, 10.0),
    "moderate": (10.0, 25.0),
    "strong": (25.0, float("inf")),
}

# 已有认知情绪印记的单轮加减分，不用于新认知初始化。
# 计算方式：new_score = clamp(current_score + 对应 delta, 0, 100)。
# 最终分数根据提交时读到的当前实体重算，避免后台评价携带过期的绝对分数。
# 调大绝对值会加快旧认知态度变化，也会提高短期事件反复冲刷长期印象的风险。
EMOTION_AFFECT_DIRECTION_DELTA = {
    "strengthened": 2.0,
    "slightly_positive": 1.0,
    "unchanged": 0.0,
    "slightly_negative": -1.0,
    "weakened": -2.0,
}


# ========== Persona 的情绪反应范围 ==========
# 这是框架允许的范围；具体角色当前值在 persona_config.json 中提供。
# mood_reactivity 是上面 mood 公式的乘数，只能放大或缩小已有方向，不能把正向变负向。
# 例：基础影响 +4、显著性 0.6、reactivity 1.5，raw_impact = +3.6。
MOOD_REACTIVITY_MIN = 0.5
MOOD_REACTIVITY_MAX = 1.7


# ========== ExperienceAppraisal 后台评价 ==========
APPRAISAL_LLM_TEMPERATURE = 0.1
# 结构化候选可能包含多条认知；4096 是输出上限，不是每轮固定消耗。
APPRAISAL_LLM_MAX_TOKENS = 4096
APPRAISAL_MAX_IN_FLIGHT = 8


# ========== PerceptionUnderstanding 感知理解 ==========
PERCEPTION_UNDERSTANDING_ENABLED = True
UNDERSTANDING_LLM_TEMPERATURE = 0.2
UNDERSTANDING_LLM_MAX_TOKENS = 4096


# ========== 对话线程 ==========
# 当前默认值用于本地测试；部署多用户入口时应由调用方传入独立 thread_id。
DEFAULT_THREAD_ID = "project_test_001"
MAX_RECENT_TURN = 5
