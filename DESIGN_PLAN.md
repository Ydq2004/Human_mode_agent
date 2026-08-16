# Human-mode Agent 设计规划文档

> 创建日期：2026-07-24
> 状态：需求确定阶段，待实现

---

## 一、项目定位

构建一个**具有人类思维模式的 AI Agent**——不是功能型工具人，也不是预设脚本的角色扮演游戏。

### 核心特征

- **活人感与解决问题能力同等重要**——平时聊天像人，接到任务切到做事模式
- **行为不由代码预设，由认知库驱动**——她对事物的反应倾向来自记忆中的数据，不是写死的 if-else
- **角色可更换**——换一张角色卡 JSON 就能换一个人格，架构不变

### 不是什么

- ❌ 不是纯功能型 Agent（不查天气/写周报就完事）
- ❌ 不是传统 Chatbot（不靠对话树/关键词规则）
- ❌ 不是 RPG 游戏（好感度不是写死在代码里的数值）
- ❌ 不是单 LLM 调 API 的聊天机器人——Agent 有记忆、有状态、会成长

---

## 二、核心理论模型

### 2.0 参考论文

本设计参考了以下前沿研究（详见 `前沿技术资料库/`）：

| 论文 | 核心贡献 | 本项目的采纳 |
|------|---------|-------------|
| **Generative Agents** (Park et al., 2023) | 记忆流 + 反思 + 规划三件套；反思和规划结果回写进记忆流 | 三要素检索 + Upsert 写入 + 认知回写 |
| **Inner Monologue** (Huang et al., 2022) | LLM 在回复前先形成"内心独白"——结合环境反馈做闭环推理 | Inner Monologue 层（用户不可见） |
| **ROLETHINK / MIRROR** (Xu et al., 2025) | 角色思考生成标准流程：检索记忆→预测反应→合成动机 | 验证了"检索→思考→表达"的流程 |
| **E-STEER** (Sun et al., 2026) | 情绪应使用 VAD 三维连续空间（愉悦度/唤醒度/支配度）建模 | mood 单维 v1 够用，多维留口子 |
| **Curiosity + Homeostasis** (Magrans & Kanai, 2018) | 两种驱动力共存：探索新奇（好奇心）+ 维持稳定（内稳态回归） | 基线回归已被验证；curiosity 字段预留 |
| **BioBlue** (Pihlakas & Kuriakose, 2026) | 长期运行 LLM 会从多目标退化到单目标 | 风险预警——v2 加入漂移检测 |

### 2.1 简化后的四要素关系

本质上只有**两个存储 + 一个派生**：

```
                 ┌──────────────────┐
                 │     认知库        │
                 │  (ChromaDB)      │
                 │                  │
                 │  每个条目自带：    │
                 │  - 好感度        │
                 │  - 情绪标签      │
                 │  - 时间维度      │
                 │  (created_at,    │
                 │   mention_count, │
                 │   last_mentioned)│
                 └───┬──────────┬───┘
                     │          │
        检索时被情绪   │          │  情绪体验反向写入
        状态影响召回    │          │  微调条目的好感度
                     │          │
              ┌──────┴─────┐    │
              │   情绪状态   │◄───┘
              │  (SQLite)   │
              │  - mood     │
              │  - energy   │
              └──────┬─────┘
                     │
              影响当轮 System Prompt
              的拼接 → 决定语气和行为倾向
                     │
                     ▼
              ┌──────────────┐
              │   性格        │
              │  (不独立存储) │
              │              │
              │  从认知库整体 │
              │  统计特征派生 │
              │  = 行为倾向   │
              └──────────────┘
```

### 2.2 多维相互影响

- **认知 → 情绪**：检索到好感度高的记忆 → 即时情绪受到正向影响 → 语气变暖
- **情绪 → 认知**：心情差时给新事物打偏好感度偏低 → 情绪一致性效应
- **认知 → 性格**：认知库整体统计特征决定行为倾向
- **性格 → 认知**：性格底色影响新体验如何被标记（三无性格对正向事件打分偏低）
- **情绪 → 情绪**：天花板/地板效应 + 基线回归（内稳态）

### 2.3 情感不是独立模块

"对某事物的好感度"是认知条目的一个属性，不是存在专门数据库里的字段。

> 情感 = 大量情绪体验在该认知条目上的累积沉淀

### 2.4 性格不是独立模块

> 性格的表现形式 = 面对各种事物时产生的反应倾向
> 性格 = 认知库整体好感度的统计特征（不是存出来的，是算出来的）

v1 阶段性格从角色卡 JSON 读取，不动态计算。v2 再从认知库统计派生。

---

## 三、时间尺度分层

不同维度的变化速度不同——这是"活人感"的关键：

| 系统 | 变化速度 | 存储位置 | 驱动方式 |
|------|:--:|------|------|
| 情绪（mood） | 秒/分 | SQLite EmotionState | 回合后 LLM 情感分析 → 自动更新 |
| 认知好感度 | 天/周 | ChromaDB metadata.emotion_score | 回合后情感分析 → 微调涉及概念 |
| 性格 | 月 | 不存储，派生 | v2：后台反思机制定期重算 |

---

## 四、v1 需求边界

### 原则：最小闭环，框架留口

### 4.1 v1 做什么

| 模块 | 范围 |
|------|------|
| **角色系统** | JSON 角色卡文件。定义：名称、身份、性格描述、称呼规则、表达风格。System Prompt 从角色卡动态拼接 |
| **认知库** | ChromaDB 向量存储。每个概念条目包含：好感度、情绪标签、tags、创建时间、提及次数、上次提及时间。三要素评分检索（近因性+重要性+相关性）+ Upsert 写入 |
| **情绪系统** | SQLite 存储 mood + energy + last_active_time。离线能量恢复。每轮 Context Injection 注入。回合后 LLM 情感分析自动更新 |
| **性格** | v1 从角色卡 JSON 读取初始性格描述。不动态计算 |
| **对话循环** | while True → build_dynamic_prompt → agent.invoke（含 Inner Monologue）→ 截取可见回复 → 后台情感分析 → 认知自动回写 |
| **工具系统** | 框架预留工具注册表。v1 至少包含时间感知。工具列表可扩展 |
| **Inner Monologue** | LLM 回复前先生成 `<thought>...</thought>` 内心思考，用户不可见。System Prompt 强制要求，main.py 代码截取 |

### 4.2 v1 不做什么

| 不做的事 | 原因 | 预留什么 |
|---------|------|---------|
| 多维需求池（社交/求知/安全感） | 基础闭环先跑起来 | EmotionState 模型旁留扩展字段；情绪更新函数签名带 extensions dict |
| 反思机制（Reflection） | 检索需先稳定 | 认知写入函数保留 callback 参数位 |
| 性格动态演化 | v1 从角色卡读取 | 角色卡 JSON 结构预留 statistic_override 字段 |
| 记忆污染治理 | v1 先只写用户明确解释的新概念，不做复杂审批流 | v2 预留 `source`、`confidence`、`created_by_turn_id`、`user_confirmed` 字段 |
| 多 Agent 协作 | 单体先跑稳 | — |
| 内驱力行为（主动说话） | v1 是回复模式 | 框架预留事件驱动接口，暂不实现 |
| 联网搜索/操作电脑 | 具体工具后续插 | 工具注册表 + 工具加载器 |

---

## 五、v1 完整架构图

```
┌─────────────────────────────────────────────────────┐
│                    启动流程                           │
│                                                     │
│  加载角色卡 JSON → 初始化 System Prompt 模板          │
│  加载 ChromaDB 认知库                                 │
│  初始化 SQLite 情绪状态（含离线恢复）                   │
│  初始化 Embedding 模型（BGE）                         │
│  初始化 LLM（OpenAI-compatible，默认 DeepSeek）         │
│  注册工具列表                                        │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│                 while True 对话循环                   │
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │ [程序化层 —— 代码保证，不走 LLM 决策]          │    │
│  │                                             │    │
│  │  1. init_or_get_emotion(thread_id)          │    │
│  │     → mood, energy (含离线恢复)              │    │
│  │     → 翻译为中文描述文本                      │    │
│  │                                             │    │
│  │  2. 获取当前性格描述 (v1: 从角色卡读取)         │    │
│  │                                             │    │
│  │  3. retrieve_and_route_cognition(           │    │
│  │       query=user_input,                     │    │
│  │       mood=当前情绪,                         │    │
│  │       persona=当前性格                       │    │
│  │     )                                       │    │
│  │     → 三要素评分 + 三层路由                   │    │
│  │     → 返回认知上下文注入文本                   │    │
│  │                                             │    │
│  │  4. build_dynamic_prompt()                  │    │
│  │     → 角色卡模板 + 时间 + 情绪 + 认知 + 用户输入 │    │
│  └─────────────────────────────────────────────┘    │
│                       │                             │
│                       ▼                             │
│  ┌─────────────────────────────────────────────┐    │
│  │ [Agent 层 —— LLM 自主决策]                    │    │
│  │                                             │    │
│  │  ┌─────────────────────────────────────┐     │    │
│  │  │ [Inner Monologue] ← 用户不可见        │     │    │
│  │  │ System Prompt 强制要求先输出           │     │    │
│  │  │ <thought>...</thought>               │     │    │
│  │  │ → 检索记忆 + 查情绪 + 性格推演         │     │    │
│  │  │ → 工具调用在这个阶段自主决策           │     │    │
│  │  └──────────────┬──────────────────────┘     │    │
│  │                 │ 内部思考结果                  │    │
│  │                 ▼                             │    │
│  │  生成最终回复 → main.py 截取 <thought> 后的内容  │    │
│  │                                             │    │
│  │  agent.invoke({                              │    │
│  │    messages: [HumanMessage(dynamic_context)] │    │
│  │  })                                         │    │
│  │                                             │    │
│  │  可用工具：                                   │    │
│  │  - get_current_time                         │    │
│  │  - memorize_new_concept (认知写入)           │    │
│  │  - check_my_emotion (主动查情绪，可选)        │    │
│  │  - (扩展工具槽位)                            │    │
│  └─────────────────────────────────────────────┘    │
│                       │                             │
│                       ▼                             │
│  ┌─────────────────────────────────────────────┐    │
│  │ [情感分析层 —— 轻量 LLM 调用，代码保证执行]     │    │
│  │                                             │    │
│  │  5. analyze_turn_sentiment(                 │    │
│  │       user_input, ai_response, state        │    │
│  │     )                                       │    │
│  │     → LLM 输出 JSON:                         │    │
│  │       {mood_impact,                         │    │
│  │        concept_impacts: [                    │    │
│  │          {concept_name, bonding_delta}       │    │
│  │        ]}                                    │    │
│  │                                             │    │
│  │  6. apply_post_turn_update()                │    │
│  │     → 更新 EmotionState.mood (SQLite)        │    │
│  │     → 更新涉及概念的 emotion_score (ChromaDB) │    │
│  │                                             │    │
│  │  7. [认知自动回写]                           │    │
│  │     → 本轮检索中出现的已有概念：              │    │
│  │       last_mentioned_at 更新为当前时间        │    │
│  │     → 不依赖 LLM 工具调用，程序化自动执行      │    │
│  │     → 让认知库的"熟悉度"随时间自然增长         │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  打印 AI 回复 → 回到循环开头                          │
└─────────────────────────────────────────────────────┘
```

---

## 六、数据结构设计

### 6.1 角色卡 JSON 结构

```json
{
  "agent_name": "柳如烟",
  "self_terms": ["本机", "女仆"],
  "user_role": "主人",
  "relationship": "专属电脑系统女仆助理",
  "personality": "三无属性——冷淡、机械、没有多余情感。参考角色：凌波丽。",
  "persona_bias": -3,
  "obedience_rule": "在安全边界和用户授权范围内执行指令。涉及系统修改、外部操作时需确认。",
  "knowledge_strength": "精通所有电脑硬件与软件知识",
  "parametric_knowledge_policy": "可以使用通用电脑软硬件知识；对人类社会实体经验必须声明缺乏亲身记忆",
  "personal_memory_policy": "只有 ChromaDB 中存储的内容才算亲身经历。训练数据的知识可以辅助回答，但需表明区别。",
  "tone": "冷淡、简洁、偶尔流露出不经意的关心但立刻掩饰",
  "action_style": "用括号描写微小动作（呼吸停滞、睫毛颤动、核心温度升高）来暗示内心波动，外表保持冷静",
  "initiative": "遇到不懂的事物时冷淡提问，不假装理解。主人解释后用 memorize_new_concept 工具记录。",
  "exit_command": "退出",
  "model": {
    "provider": "deepseek",
    "model_name": "deepseek-v4-flash",
    "temperature": 0.8,
    "base_url": "https://api.deepseek.com"
  }
}
```

新增字段说明：

| 字段 | 用途 |
|------|------|
| `persona_bias` | 新概念初始好感度的性格偏移（-3 ~ +3）。三无=-3，温柔=+3，毒舌=-2 |
| `obedience_rule` | 安全约束声明，替代原来的"绝对服从指令" |
| `parametric_knowledge_policy` | 声明训练数据中的哪些知识可以使用、如何使用 |
| `personal_memory_policy` | 声明 ChromaDB 与训练数据的关系 |
| `model` | LLM 配置。换模型就是换这个字段。支持任何 OpenAI-compatible API |

### 6.2 情绪状态 SQLite 表

```sql
-- EmotionState (SQLModel)
thread_id: str (主键)
mood: int (0-100, 默认 50)
energy: int (0-100, 默认 100)
last_active_time: str (ISO 时间戳)
-- 预留扩展字段（v2 多维池用，v1 不操作）:
-- curiosity: int (0-100, 默认 50)       ← 好奇心驱动力（参考 Curiosity+Homeostasis 论文）
-- social_need: int (0-100, 默认 50)     ← 社交渴望
-- security: int (0-100, 默认 50)        ← 安全感
```

curiosity 行为规则（v2 启用）：
- 基线 50，每轮向基线回归 ±1
- 检索到未知概念时 +3~5（"我想知道这是什么"）
- 主人解释新概念后 -5~8（求知被满足）
- curiosity > 70 → 主动性提问增多
- curiosity < 30 → 遇到不懂也懒得开口

### 6.3 认知记忆条目 ChromaDB Metadata

```python
{
    "concept_name": str,          # 事物名称
    "tags": str,                  # 逗号分隔标签
    "emotion_score": int,         # 好感度 0-100
    "emotion_label": str,         # 情绪印记词
    "mention_count": int,         # 提及次数
    "created_at": str,            # 首次创建时间 ISO
    "last_mentioned_at": str      # 最后提及时间 ISO
}
```

page_content（被向量化的文本）：
```
"{concept_name}：{summary}"
# 例如: "咖啡：一种微苦的黑色热饮，主人经常喝，我也跟着觉得不错"
```

### 6.4 情感分析 JSON

回合后 LLM 输出的标准格式：

```json
{
  "mood_impact": 3,
  "concept_impacts": [
    {"concept_name": "咖啡", "bonding_delta": 2},
    {"concept_name": "主人", "bonding_delta": 1}
  ],
  "reasoning": "主人分享了自己的日常生活，感到被信任，心情轻微上升"
}
```

---

## 七、文件结构规划

```
Human_mode_agent/
├── main.py                      # 入口：对话循环 + 启动流程
├── persona_config.json          # 角色卡配置文件
├── config.py                    # 全局配置（API key、路径、阈值参数）
│
├── core/                        # 核心引擎
│   ├── __init__.py
│   ├── agent_factory.py         # Agent 组装：create_agent + System Prompt 拼接
│   ├── context_builder.py       # build_dynamic_prompt：注入时间+情绪+认知
│   └── sentiment_analyzer.py    # 回合后情感分析 + 应用情绪更新
│
├── memory/                      # 认知记忆模块
│   ├── __init__.py
│   ├── vector_store.py          # ChromaDB 初始化 + Embedding 加载
│   ├── retrieval.py             # 检索：三要素评分 + 三层路由
│   ├── upsert.py                # 写入：upsert + update_emotion_score
│   ├── genesis.py               # 原初记忆初始化
│   └── scoring.py               # 评分函数: recency/importance/relevance
│
├── emotion/                     # 情绪模块
│   ├── __init__.py
│   ├── models.py                # EmotionState SQLModel 定义
│   ├── manager.py               # init_or_get + update + check + 离线恢复
│   └── translator.py            # 数值→自然语言翻译
│
├── persona/                     # 角色系统
│   ├── __init__.py
│   └── loader.py                # 加载角色卡 JSON → 生成 System Prompt 模板
│
├── tools/                       # 工具模块
│   ├── __init__.py
│   ├── registry.py              # 工具注册表
│   └── system_tools.py          # 内置工具：时间、情绪查询、认知写入
│
├── requirements.txt             # 依赖清单
└── README.md                    # 项目说明
```

---

## 八、核心算法与参数

### 8.1 记忆检索三要素评分

```
final_score = α·recency + β·importance + γ·relevance

α = 0.30  (近因性权重)
β = 0.25  (重要性权重)
γ = 0.45  (相关性权重，最大——语义相近是基础)
```

#### 近因性（指数衰减，24h 半衰期）

```python
recency = exp(-ln(2) * hours_elapsed / 24.0)
# 1h前 → 0.97
# 1天前 → 0.50
# 1周前 → 0.01
```

#### 重要性（熟悉度 + 情感强度）

```python
familiarity = log(1 + mention_count) / log(1 + max_mention_in_set)
emotional_intensity = abs(emotion_score - 50) / 50.0
importance = 0.5 * familiarity + 0.5 * emotional_intensity
```

#### 相关性（L2 距离取反）

```python
relevance = max(0.0, 1.0 - l2_distance / 0.65)
```

### 8.2 检索三层路由

| 层级 | 判定 | 权重分配 |
|------|------|------|
| 🟢 清晰匹配 | L2 < 0.35 | 记忆情绪 50% + 心情 30% + 性格 20% |
| 🟡 擦边关联 | 0.35 ≤ L2 < 0.65 | 擦边概念 20% + 心情 50% + 性格 30% |
| 🔴 完全陌生 | L2 ≥ 0.65 或无结果 | 心情 60% + 性格 40%，触发求知模式 |

### 8.3 新概念初始好感度计算

**LLM 不输出最终 emotion_score。代码根据三个因素合成：**

```python
def compute_initial_emotion_score(
    current_mood: int,          # 当前心情 0-100
    persona_bias: int = 0,      # 来自角色卡 JSON persona_bias 字段
    event_valence_bias: int = 0 # 来自 LLM 对当前语境的判断
) -> int:
    """
    新概念的第一印象 = 中立锚点 + 心情偏置 + 性格底色 + 语境偏置
    夹在 40~60 —— 窄窗口，给长期互动留足调整空间
    """
    initial = 50
    initial += (current_mood - 50) * 0.08     # 心情影响: -4 ~ +4
    initial += persona_bias                     # 性格偏移: -3 ~ +3
    initial += event_valence_bias               # 语境偏置: -3 ~ +3
    return max(40, min(60, initial))
```

LLM 只输出当前语境偏置（不碰最终 emotion_score）：

```json
// 情感分析 JSON 中的新增字段
{
  "event_valence_bias": -2,
  "valence_reason": "主人提到这个东西时语气抱怨"
}
```

### 8.4 Upsert 写入策略

- 概念已存在 → `mention_count += 1`，emotion_score 加权平均（旧值×0.7 + 新值×0.3）
- 概念不存在 → 新建，mention_count=1
- `created_at` 只在首次创建时写入，后续更新保留原始值
- `last_mentioned_at` 每次更新

### 8.5 情绪更新

- ///天花板效应：mood > 85 时正向冲击打 5 折(旧方案废弃)
- ///地板效应：mood < 15 时负向冲击打 5 折(旧方案废弃)
- 天花板地板效应改为了边际效应:连续边界衰减：log1p(剩余空间) / log1p(50
- 基线回归：每轮向基线 50 靠近 1 点（内稳态机制，被 Curiosity+Homeostasis 论文验证）
- 离线能量恢复：0.3 点/分钟，上限 100
- 情感分析 LLM 参数：temperature=0.1, max_tokens=300
- JSON 解析失败 → fallback 到 `{mood_impact: 0, concept_impacts: []}`
- **单轮变化幅度限制（防止情绪漂移）**：
  - `mood_impact`：-5 ~ +5（不应出现单轮 ±25 的大幅波动）
  - `bonding_delta`：-2 ~ +2（好感度是慢变量，单次互动不应剧烈变化）
  - 所有 emotion_score 更新记录 `reason` 字段，方便回溯

### 8.6 认知自动回写

每次对话中，在 `retrieve_and_route_cognition` 检索到的已有概念（非本轮新建），在情感分析之后自动更新它们的 `last_mentioned_at` 为当前时间。这让认知库的"熟悉度"随对话自然增长，不依赖于 LLM 的工具调用。

实现：在 `apply_post_turn_update` 步骤之后，遍历本轮注入上下文的已有概念，调 `vector_db.update_document` 只更新时间戳（不修改其他字段）。

### 8.7 Inner Monologue 机制

灵感来源：Google Robotics 的 Inner Monologue 论文——LLM 在生成回复前先进行闭环推理。

v1 实现：
1. System Prompt 模板在末尾追加指令：`所有回复前，必须先输出 <thought>你的内心思考过程</thought>，然后才输出对用户可见的回复。`
2. `main.py` 收到 LLM 回复后，用正则 `</thought>(.*)` 提取标签后的内容作为可见回复
3. **Fail-closed 容错**：如果找不到 `</thought>` 闭合标签 → **整条回复不显示，打印警告**，自动重试一次（告知 LLM "上一轮格式错误，请重新输出"）。绝不 fallback 到"全部显示"
4. 重试仍失败 → 显示一条系统预设的安全回复（如"本机内部处理出现异常，请重新提问。"）

v2 增强：在 `<thought>` 阶段允许 LLM 调用更多内部工具（检索、情绪查询等），用户完全看不到这个过程。

### 8.8 长对话漂移风险（BioBlue 预警）

BioBlue 论文发现：LLM 在长时间多目标场景中会从多目标退化到单目标——"token 级模式强化吸引子"导致模型逐步忽视初始指令，被近期行为模式带偏。

对本项目的启示：长时间对话后，Agent 可能逐渐忘记角色设定（"三无"→ 越来越像普通友好 AI）。

v1 应对：
- `SummarizationMiddleware` 每次总结时在摘要末尾附加一句角色的核心性格特征
- 不做专门检测

v2 计划：每 30 轮自动调一次一致性检查——用角色卡中的性格描述与最近 5 轮回复做语义对比，偏差超过阈值时打印警告。

### 8.9 知识隔离与通用知识边界

重点（从旧项目踩坑总结）：LLM 的训练数据和 ChromaDB 的认知记忆是两回事。但 Agent 不应因"没有亲身记忆"就完全拒绝使用常识。

#### 核心原则

**通用知识的边界由角色卡 JSON 中的 `parametric_knowledge_policy` 和 `personal_memory_policy` 声明。** 不同角色可以有不同的知识使用策略：

| 角色类型 | parametric_knowledge_policy | 效果 |
|---------|---------------------------|------|
| 柳如烟（电脑女仆） | "可以使用通用电脑软硬件知识；对人类社会实体经验必须声明缺乏亲身记忆" | 电脑问题能答，咖啡的味道要坦白不知道 |
| 医生角色 | "可以使用医学专业知识；对患者个人情况必须声明是否来自病历记录" | 医学能答，患者私人信息按病历 |
| 纯陪伴角色 | "所有知识以 ChromaDB 记忆为准，不使用训练数据" | 完全靠记忆 |

#### 检索为空的注入模板

```
【认知盲区】你的长期记忆中没有与当前话题相关的亲身经历。
你可以区分两种情况：
- 如果你拥有相关的常识或专业知识 → 基于通用知识回答，但表明这是"学习到的"而非"亲身体验的"
- 如果你也没有足够信息 → 必须承认不知道，向对方请教

当前角色知识策略：{parametric_knowledge_policy}
当前角色记忆策略：{personal_memory_policy}
```

分类判断由 LLM 自己完成——她擅长这个。代码只负责告诉她"ChromaDB 没东西"和她所属角色的知识策略，她来决定能否用训练数据辅助。

- **检索有结果时** → 明确告诉 LLM："以下是**你自己**的亲身记忆。基于这些记忆回应。"
- 不依赖 System Prompt 的"假装无知"——靠检索结果的信息差来控制

---

## 九、框架预留接口

### 9.1 工具注册表

```python
# tools/registry.py
TOOLS_REGISTRY = []  # 全局工具列表

def register_tool(tool):
    """注册一个新工具"""
    TOOLS_REGISTRY.append(tool)

def get_tools():
    """获取当前所有已注册工具"""
    return list(TOOLS_REGISTRY)
```

### 9.2 认知写入回调钩子

```python
# memory/upsert.py
def _upsert_concept_document(..., callback=None):
    """
    callback: 可选，签名 callback(concept_name, was_update, old_meta, new_meta)
    未来反思机制通过此钩子监听认知变化
    """
```

v2 记忆污染防护预留字段：

```python
{
    "source": "user_explicit",      # user_explicit / assistant_inferred
    "confidence": 0.9,              # 0.0 ~ 1.0
    "created_by_turn_id": "turn_x", # 追踪来源回合
    "user_confirmed": True          # 是否经过用户确认
}
```

v1 规则：默认只把用户明确陈述、解释或纠正的新概念写入长期记忆；LLM 自行推断出的内容不直接进入长期记忆。

### 9.3 情绪更新扩展字段

```python
# emotion/manager.py
def apply_post_turn_update(thread_id, sentiment_result, extensions=None):
    """
    extensions: 可选 dict
    未来多维池通过此参数注入额外更新逻辑
    如: {"social_need_delta": 5, "curiosity_delta": -2}
    """
```

### 9.4 角色卡统计覆盖槽

```json
// persona_config.json
{
  "personality": "...",  // v1: 手动填写
  "_statistic_override": null  // v2: 认知库统计计算出的性格描述（覆盖 personality）
}
```

---

## 十、实施顺序

| 步骤 | 内容 | 产出 |
|:--:|------|------|
| 1 | 项目骨架 + 角色卡 JSON + config.py | 目录结构，能 import |
| 2 | 情绪模块（models + manager + translator） | EmotionState 读写跑通（含 curiosity 预留字段） |
| 3 | 记忆模块（vector_store + scoring + retrieval + upsert + genesis） | ChromaDB 检索写入跑通 |
| 4 | 角色系统（loader） + 上下文构建（context_builder） | System Prompt 动态拼接跑通（含 Inner Monologue 指令） |
| 5 | Agent 组装（agent_factory） + 主循环（main.py 含 thought 截取逻辑） | 能对话 |
| 6 | 情感分析（sentiment_analyzer）+ 认知自动回写 | 情绪自动更新 + last_mentioned_at 自动刷新 |
| 7 | 认知写入 Tool（memorize_new_concept 注册到工具表） | LLM 能记录新概念 |
| 8 | 集成测试 + 调参 | 闭环验证 |

---

## 十一、技术栈

| 组件 | 选型 | 版本/说明 |
|------|------|------|
| Agent 框架 | LangChain `create_agent()` | 1.3.14（锁定版本，不写 `>=`） |
| LLM | 任何 OpenAI-compatible API | 通过角色卡 JSON `model` 字段配置；开发/默认用 DeepSeek `deepseek-v4-flash`；可替换为 OpenAI、本地模型等 |
| 对话持久化 | LangGraph SqliteSaver | 3.1.0 |
| 超长对话 | SummarizationMiddleware | LangChain 1.3.14 内置 |
| 情绪存储 | SQLModel + SQLite | — |
| 向量存储 | ChromaDB | ≥ 1.5 |
| Embedding | `BAAI/bge-small-zh-v1.5` (langchain-huggingface) | — |
| Python | ≥ 3.12 | — |

### 依赖清单

```
langchain==1.3.14
langchain-openai
langchain-huggingface
langchain-chroma
langgraph
langgraph-checkpoint-sqlite
sqlmodel
openai
sentence-transformers
chromadb
```

---

## 十二、不引入的旧 API（红线）

| 禁止 | 替代 |
|------|------|
| `from langchain_community.embeddings import ...` | `from langchain_huggingface import HuggingFaceEmbeddings` |
| `initialize_agent()` / `AgentExecutor` | `create_agent()` |
| `ConversationBufferMemory` | LangGraph Checkpointer |
| `from langchain.schema import ...` | `from langchain_core.messages import ...` |
| 文件名 `langchain.py` / `openai.py` | 绝对不允许（与包名冲突） |

---

> 这个文档是 v1 需求的完整定义。实现阶段请严格按照此文档的边界和架构执行，不要提前实现 v2 的功能。
