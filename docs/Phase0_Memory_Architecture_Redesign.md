# Phase 0：记忆架构重构执行计划

版本：v0.6 执行版

本次更新：Step 6 的进程内有序写入闭环已经完成自动化测试和真实 LLM 开放验收。当前 `ExperienceSlice` 仍只存在于进程内，下一施工主线是不可变经历持久化；人格反思和多时间尺度情景记忆必须等经历持久化完成后再启动。

目标：把当前记忆模块重构成稳定、可查询、可查重、可更新的长期认知层，服务于通用“类人 Agent 框架”，而不是服务于某个单一角色。

这不是“未来预留文档”。Phase 0 中新增的字段和模块，都必须进入真实的写入、读取、检索、评估闭环。如果一个能力暂时不能被当前逻辑使用，就先不要写进 schema、prompt 或接口里。

## 核心原则

1. 记忆系统属于框架层，不绑定具体角色。
2. SQLite 是结构化认知主库。
3. ChromaDB 是语义召回索引，不是认知身份来源。
4. `concept_id` 是稳定认知身份。
5. `canonical_name` 是当前显示名，可以变化。
6. `aliases` 用来支持别名、旧称呼、用户称呼。
7. `identity_signature` 用于新建查重和已有认知定位。
8. 只要能定位到已有认知，记忆更新就应该优先操作 `concept_id`。
9. 每个阶段结束后必须测试，通过后再进入下一步。
10. LLM 相关模块必须进行边界测试和极限测试，不能只测 prompt 示例附近的 happy path。
11. prompt 失败时，优先判断是“判定标准缺失”还是“单个样例缺规则”。如果是标准缺失，应补流程、输入契约或职责边界，而不是追加单例补丁。
12. 示例只用于校准边界，不能替代规则主体。

## Phase 0 范围

### 本阶段要做

- 新的记忆实体结构。
- SQLite 结构化记忆主库。
- 以 `concept_id` 为主键的 Chroma 向量索引。
- 认知身份签名提取。
- Identity Resolver，用于判断 create/update/reinforce 指向谁；已有认知的主观情绪更新不伪装成事实 operation。
- 混合检索：精确匹配 + 结构化查询 + 向量检索 + 合并排序。
- 感知驱动主循环接入新记忆系统：从感知事件出发，回应前使用新检索，回应后产出结构化记忆候选。
- 最小关系系统，只做能参与检索的一跳关系。
- 回合后即时回看：以进程内冻结的 `ExperienceSlice` 为输入，独立产出情绪评估和原子记忆候选；本阶段不直接写长期记忆，也不持久化 ExperienceSlice。

### 本阶段先不做

- 复杂多跳图遍历。
- 完整版本历史。
- 程序记忆。
- 复杂遗忘、衰减、归档系统。
- 可视化图谱。
- 多层认知压缩与归档。
- 独立的内在审议 / 内心独白模块。
- 回应前临时情绪评估和行动倾向规划。
- 对人格、情绪、检索认知对单次回复的精确因果归因。
- 从认知统计特征自动改变基础人格。

这些以后都很有价值，但必须等基础的“认知实体、查重、更新、检索”闭环稳定之后再做。

## 目标记忆实体

Phase 0 使用精简结构。不要增加当前逻辑不用的字段。

```python
MemoryEntity:
    concept_id: str              # 稳定唯一身份
    canonical_name: str          # 当前显示名
    aliases: list[str]           # 别名列表
    memory_type: str             # 记忆类型
    identity_signature: dict     # 语义身份签名
    summary: str                 # 摘要
    tags: list[str]              # 标签
    emotion_score: float         # 角色对该认知的主观情绪分，范围 0.0~100.0
    emotion_label: str           # 情绪标签
    mention_count: int           # 提及次数
    created_at: str              # 创建时间
    last_accessed_at: str        # 最后检索/访问时间
    last_modified_at: str        # 最后修改时间
    source: str                  # 来源：user_told / inferred / system
    confidence: float            # 置信度
```

Phase 0 支持的 `memory_type`：

```text
entity
preference
relationship
interaction_pattern
event
knowledge
other
```

`identity_signature` 示例：

```json
{
  "subject": "主人",
  "relation": "喜欢",
  "object": "橘子",
  "qualifier": "酸甜口味"
}
```

## 最小关系系统

关系系统进入 Phase 0 的前提是：它必须参与检索。  
本阶段只做一跳关系，不做复杂图遍历。

```python
MemoryRelation:
    relation_id: str
    source_concept_id: str
    relation_type: str
    target_concept_id: str
    weight: float
    created_at: str
    last_reinforced_at: str
    confidence: float
```

Phase 0 支持的关系类型：

```text
related_to
belongs_to
refers_to
similar_to
```

## 执行计划

### Step 1：记忆结构与存储层

目的：建立新的记忆主库。

可能涉及文件：

- `memory/schema.py`
- `memory/types.py`
- `memory/sql_store.py`
- `memory/vector_store.py`
- `memory/store_manager.py`

任务：

1. 定义 `MemoryEntity` 和 `MemoryRelation`。
2. 定义允许的记忆类型和关系类型。
3. 创建 SQLite 表：
   - `memory_entities`
   - `memory_relations`
4. 添加索引：
   - `concept_id`
   - `canonical_name`
   - `memory_type`
   - relation 的 source/target id
5. 让 Chroma 文档使用 `concept_id` 作为向量 id。
6. 实现统一存储管理器：
   - `upsert_entity(entity)`
   - `get_entity(concept_id)`
   - `search_by_name_or_alias(name)`
   - `delete_entity(concept_id)`，仅用于开发期重置

验收测试：

1. 创建一个认知实体，并能从 SQLite 读回。
2. 确认 Chroma 中存在相同的 `concept_id`。
3. 修改 `canonical_name` 后，`concept_id` 不变化。
4. 添加 alias 后，能通过 alias 找到同一实体。

停止条件：

如果认知身份不能在重命名和别名查询后保持稳定，不进入 Step 2。

### Step 2：认知身份签名提取器

目的：把记忆候选转成结构化身份。

可能涉及文件：

- `memory/identity_extractor.py`

任务：

1. 实现 `extract_identity_signature(concept_name, summary, memory_type, llm)`。
2. 使用 JSON mode 和低温度。
3. 明确 extractor 的输入契约：每次输入原则上只包含一个原子认知。
4. extractor 不负责拆分多事实、多 object 或对立偏好；这些必须由上游候选生成/appraisal 阶段处理。
5. 明确 subject / relation / object / qualifier 的提取流程：
   - subject 选择稳定主体，不使用“我/你/他/她/这个/那个”等代词作为长期身份。
   - relation 使用稳定归一词。
   - object 表示 relation 指向的核心对象。
   - qualifier 只保留区分同一 subject-relation-object 的必要限定，不塞入另一个独立事实。
6. 标准化关系词：
   - `喜欢`
   - `讨厌`
   - `避免`
   - `属于`
   - `是`
   - `表达`
   - `习惯`
   - `影响`
   - `关系`
   - `请求`
   - `提到`
   - `unknown`
7. 提取失败时提供保底结果：
   - subject: `concept_name`
   - relation: `unknown`
   - object: `None`
   - qualifier: `None`

验收测试：

1. `主人喜欢酸甜橘子` 能提取出 subject=`主人`，relation=`喜欢`，object=`橘子`。
2. `大黄是主人的狗` 能提取出大黄、狗、主人之间的核心身份信息。
3. `主人通过张开双臂请求拥抱` 能提取出主人和拥抱互动。
4. 近义偏好词能归一，例如“偏爱/爱吃”归为 `喜欢`。
5. 回避表达不能被粗暴压成讨厌，例如“不吃/不喝/害怕并避开”优先归为 `避免`。
6. 别名/本名/外号类认知的 subject 应是被命名的稳定主体，而不是别名本身。
7. 互动暗号类认知应把动作放入 qualifier，把需求/意图放入 object。
8. 状态触发类认知应把触发条件放入 qualifier，把被触发状态放入 object，relation 使用 `影响`。
9. 复合事实测试：如果输入包含多个原子认知，extractor 只能提取一个主要认知，不得把另一个事实塞进 qualifier。

停止条件：

如果核心测试和极限测试结果不稳定，不进入 Step 3。  
如果发现多事实输入被压扁成单个 signature，应记录为上游候选生成/appraisal 的拆分责任，不要让 extractor 承担拆分。

### Step 3：Identity Resolver

目的：判断一个记忆候选是新认知，还是指向已有认知。

可能涉及文件：

- `memory/identity_resolver.py`

解析顺序：

```text
target_concept_id
-> canonical_name / aliases 精确匹配
-> identity_signature 匹配
-> 向量候选 + 低温 LLM 判定
-> 确认为新认知
```

任务：

1. 实现 `resolve_identity(candidate)`。
2. 实现 ID 精确查找。
3. 实现名称/别名查找。
4. 实现 signature 查找：
   - same 判断需要 memory_type 相同或兼容
   - subject/relation/object 匹配
   - qualifier 可以兼容，不要求完全一致
5. deterministic resolver 只做高确定性判断；语义模糊区必须返回 `ambiguous`，不能强行选择第一个候选。
6. `memory_type` 可以约束 same，但不能阻断跨类型 related 候选召回。
7. 实现 LLM judge：只把 `ambiguous` 候选裁决为 `same / related / new`。
8. LLM judge 不查库、不写库、不输出 confidence。
9. 返回结构化结果：

```python
ResolveResult:
    decision: "same" | "ambiguous" | "new"
    target_concept_id: str | None
    confidence: float
    reason: str
    candidate_concept_ids: list[str]
```

LLM judge 输出：

```json
{
  "decision": "same | related | new",
  "target_concept_id": "cog_xxx 或 null",
  "relation_type": "related_to | belongs_to | refers_to | similar_to | none",
  "reason": "一句话说明"
}
```

验收测试：

1. `主人喜欢酸甜橘子` + `主人对橘子的偏好` -> same。
2. `主人喜欢橘子` + `主人喜欢狗` -> new。
3. `主人喜欢狗` + `大黄是主人的狗` -> related，不是 same。
4. `主人` 改名为 `主人(本名xxx)` 后，仍然是同一个 concept_id。
5. qualifier 不一致时，规则层返回 `ambiguous`，且 `target_concept_id=None`，候选放入 `candidate_concept_ids`。
6. LLM judge 即使收到/生成 confidence，最终返回也不能包含 confidence。
7. 非法 `target_concept_id` 必须降级为 `new`。
8. 多候选测试中，judge 应能选择强语义关联候选，而不是弱相似候选。
9. 弱相似测试中，同 subject / 同 relation / 同 memory_type 但 object 无明确语义关联时，应判为 `new`。

停止条件：

如果不能区分“同一个认知”和“相关但不同认知”，不接入 appraisal。
如果 LLM judge 只能靠示例附近样例通过，而不能通过边界和极限测试，不接入 appraisal。

### Step 4：混合检索引擎

目的：让主 Agent 看到真正相关的认知。

可能涉及文件：

- `memory/retrieval_engine.py`
- `core/context_builder.py`
- `core/cognitive_router.py`

检索路径：

```text
名称/别名精确匹配
结构化查询
向量检索
一跳关系扩展
按 concept_id 合并
排序
```

任务：

1. 实现 `retrieve_memories(query, filters=None, top_k=10, include_related=True)`。
2. 精确匹配检查 `canonical_name` 和 `aliases`。
3. 结构化查询支持：
   - memory_type
   - subject
   - object
4. 向量检索返回 concept_id，再从 SQLite 读取完整实体。
5. 多路结果按 `concept_id` 合并。
6. 只做一跳关系扩展。一跳关系只扩展当前命中认知的直接邻居，不递归扩展邻居的邻居。
7. 排序参考：
   - 检索来源强度
   - 向量相似度
   - mention_count
   - 最近访问时间
   - 情绪偏离中性的程度，必要时使用
8. 一跳关系扩展不能把所有邻居视为同等相关：应保留关系的 `weight` / `confidence` 证据，并在扩展预算或排序中使用它们。关系边只能提供联想证据，不能覆盖直接命中。

验收测试：

1. `(张开双臂)` 能在 router 扩展后找回拥抱/亲密互动相关认知。
2. `你怎么看主人` 能找回 `主人` 和关系认知。
3. `我喜欢酸甜口味的水果` 能找回已存在的橘子偏好。
4. 多路检索不会重复注入同一个 concept_id。
5. query 与 filters 同时存在时，filters 作为结构化线索参与检索，但不能因为名称命中就阻断 signature/结构化召回。
6. 检索测试必须包含 alias 命中、subject/object 命中、弱相似不误召回、跨类型 related 候选召回。
7. 关系扩展测试必须包含“多个一跳邻居但只有强关系应进入有限结果”的场景，避免图展开把无关认知注入主 Agent。

停止条件：

如果自动唤起结果不能稳定返回 `activated_memory_refs`，不接入主 prompt。

### Step 5：感知驱动主循环接入新记忆系统

目的：让新记忆系统进入主 Agent 的真实认知循环，而不是只停留在独立测试函数里。

主循环从“感知事件”开始，而不是从“用户文本”开始。当前阶段最先接入的是用户文字输入，但框架入口不能被当前输入形态绑死。

`PerceptionEvent` 表示 Agent 感知到的一次事件。它可以来自：

```text
用户文本
工具结果
视觉/听觉识别结果
环境观察
动作反馈
系统状态变化
后台定时提醒
其他 Agent 消息
```

当前聊天输入只是：

```text
PerceptionEvent(source="user", modality="text", content=user_input)
```

主循环采用多阶段职责，而不是把“理解、情绪、记忆、写入”压进一个万能 `turn_appraisal`：

```text
感知事件
-> 感知理解：我感知到了什么，它在当前情景里可能意味着什么
-> 记忆唤起：检索相关长期认知
-> 可选的情景回看：当记忆唤起带来强证据、矛盾信息或关键歧义时，结合被唤起的认知重新理解当前事件
-> 主 Agent 自然回应/工具行动：参考人格、当前状态和被唤起认知
-> 行动观察：接收工具结果、环境反馈或可见回复的实际结果
-> 回合回顾：本轮实际发生了什么
-> 小事小记：产出原子记忆候选，但不直接写库
```

这个“情景回看”最多做一次，不做无限递归思考。它表达的是人会先粗略理解当下，再因为想起往事或得到反馈而修正理解；不是让某个 LLM 反复自我调用。

这里的“回合回顾”和“小事小记”只是最低层即时回看，不是完整反思系统。

类人 Agent 的评估/总结应保留多时间尺度：

```text
即时回看：
  一次感知/回应后，轻量判断本轮发生了什么、情绪是否变化、是否有明确原子记忆候选。

片段总结：
  一段互动、一个话题、一次协作、一次安慰或一次争执结束后，总结这段经历的主题、变化和关系意味。

阶段反思：
  一段时间内多次互动后，观察是否出现稳定模式、关系变化、习惯变化或长期倾向。

长期整理：
  后台合并、强化、降权、遗忘、归档和抽象化长期认知。
```

Step 5 当前只做“即时回看”和“原子记忆候选”，不做片段总结、阶段反思、长期整理、复杂衰减或权威状态/长期记忆写入。Step 6 单独负责按序提交 mood 变化、事实候选和认知情绪候选；因此“Phase 0 不写入”只适用于 Step 5 的无写入主循环，不能用来否定 Step 6 的提交职责。

#### Checkpoint 压缩不等于长期记忆

LangGraph checkpoint 的职责是恢复 Agent 的短期运行状态，不是保存可供长期反思的权威经历。当前 LangChain 通用 `SummarizationMiddleware` 达到 token 阈值后，会用机器总结替换活动消息状态中的较早历史，并保留最近消息；这个新状态会继续写入 checkpoint，而不是只在单次模型请求中临时生效。

通用总结器不能直接承担本项目的片段总结或长期整理：

1. 默认 Prompt 面向任务型 Agent，主要提取 session intent、任务摘要、文件和下一步，不具备事件来源、关系判断、情绪解释和不确定性边界。
2. 总结结果以 `HumanMessage` 进入活动历史，可能让主 Agent 把机器派生内容误认为用户陈述。
3. 后续压缩可能再次总结旧总结，形成有损漂移；派生的关系解释、情绪结论或人格判断可能在循环中逐渐被强化。
4. checkpoint 中的旧快照即使仍物理存在，也只是运行状态历史，不能作为不可变事件账本或长期记忆主库。

正式的多时间尺度情景记忆必须以逐轮持久化的不可变 `ExperienceSlice` 为原始证据。ExperienceSlice 在每轮行动完成后立即追加保存；片段、每日、周/月摘要都是可重建的派生索引，必须保存时间范围、来源 slice id、生成时间和算法/Prompt 版本，并允许从摘要定位一段按时间顺序排列的原始过程。`MemoryEntity` 只提供创建时间和最近提及时间作为回忆线索，不把时间戳当成单条切片身份。具体问题队列和施工顺序见 `docs/POST_STEP6_KNOWN_ISSUES.md`；这些工作不进入当前 Step 6 范围。

#### 最终一致的 ExperienceAppraisal 契约

本阶段采用一次 `ExperienceAppraisal` LLM 调用，输出三个逻辑区域：

```text
experience_review   本轮发生了什么、哪些证据可靠、哪些仍不确定
emotion_assessment  本轮对全局 mood 的方向、幅度和已有认知情绪的更新建议
memory_assessment   原子事实候选，以及与候选绑定的初始情绪印象
```

三个区域共享同一个 `ExperienceSlice` 和一次模型调用，但清洗、回退和失败边界彼此独立。某一区域非法时，只回退该区域，不能用伪造结果填满整份评价。

`ExperienceSlice` 必须包含结构化的 `preceding_context`，例如最近相关事件和当前关系上下文。不得同时保存“事件列表”和该列表的机械 join 副本；需要文本时由消费端临时格式化。Persona 快照作为 appraisal 的独立输入，不塞进经历事实本体；这样同一经历可以在不同角色卡下重新解释，同时不会污染事件来源。当前“冻结”只表示进程内对象不会被后续代码修改，不表示已经持久化；SQLite 目前没有 ExperienceSlice 表。

#### Step 5 的前台与后台边界

Step 5 先采用分期异步化，不把未来 Step 6 的提交复杂度提前塞进当前闭环：

```text
前台关键路径：
PerceptionFrame
-> PerceptionUnderstanding
-> retrieval
-> 主 Agent
-> 冻结 ExperienceSlice
-> 立即返回并显示回复

后台单 worker FIFO：
ExperienceSlice
-> ExperienceAppraisal
-> compute_appraisal_effects
-> 记录结构化结果和调试状态
```

当前 `compute_appraisal_effects` 只产生候选，不写 mood、长期认知或外部系统，因此 Step 5 不需要在下一事件边界等待上一条 appraisal。后台任务必须有可关联的事件/任务 id 和真实状态（至少 `pending / completed / failed`）；尚未完成不能返回伪造的 fallback，异常也不能在线程中被静默吞掉。调试输出应缓冲或携带事件 id，避免与下一轮输入交错。

Step 6 接入真实提交后，才增加有序提交、等待超时、状态滞后记录和 Chroma 前台检索/后台写入并发验证。阶段性候选仍只保存在进程内，不建立持久任务队列。

性能优化必须先有数据。至少分别记录感知理解、检索、主 Agent、`ExperienceAppraisal` 和规则计算的单调时钟耗时；这些遥测只能进入调试数据，不能注入主 Agent 或被当成经历事实。

情绪区域的规范如下：

1. `event_valence` 是整轮事件的统一效价：`strong_positive / mild_positive / neutral / mild_negative / strong_negative`。删除重复的 `event_effect` 字段，初始印象直接复用 `event_valence`。
2. 只有一轮拆出多个情绪方向不同的新候选时，候选才可以额外提供 `candidate_valence`；单候选不得重复判断同一事件效价。
3. `event_relevance` 为 `none` 或 `low` 时，代码把效价归一为 `neutral`：mood 和已有认知情绪不变，但事实候选仍可保留。判为 `high` 时必须点名受到重大影响的关系、目标、边界/自主性、安全或关键任务，并提供已确认的直接证据。不能仅凭未确认的可能含义判 high；如果重大影响本身已确认，只是原因未知，仍可判 high。
4. 事件效价映射为基础 mood 变化：`strong_positive=+4`、`mild_positive=+2`、`neutral=0`、`mild_negative=-2`、`strong_negative=-4`。再乘显著性系数（`high=1.0`、`medium=0.6`、`low=0.3`）和角色卡的 `emotion_profile.mood_reactivity`（框架校验范围 `0.5~1.5`）。`mood_reactivity` 只调幅度，不创造方向。
5. 结果经过边界阻尼、half-away-from-zero 舍入和 `[-5, +5]` 钳制后形成 mood delta 候选，再由 Step 6 幂等、按序提交。边界阻尼的归一化常数必须由 `MOOD_MAX - MOOD_BASELINE` 等配置派生，不能写死 `log1p(50)`。记录被 `MOOD_IMPACT_MAX` 截断的次数，便于观察配置是否吞掉了人格差异；Phase 0 暂保留上限 5。
6. `energy` 在 Phase 0 冻结，不由 appraisal 评估或写入；它只保留事件开始时的 snapshot，后续由离线恢复和真实行动机制推进。

已有认知的情绪更新只走 `affected_memories` 路径，事实 `create/update/reinforce` 不得顺手改情绪，也不再使用 `affect_only` 伪装成事实 operation。每个情绪候选至少包含有效 `concept_id`、`change_direction`、表示本次变化幅度的 `strength`，以及 LLM 提议的结构化 `label_update`。`label_update` 不是裸字符串，而是下面三个字段组成的对象；无法提出可靠标签时允许为 `null`：

```json
{
  "label": "开始产生兴趣",
  "polarity": "positive",
  "strength": "moderate"
}
```

其中，`polarity` 只允许 `positive / neutral / negative`，`label_update.strength` 只允许 `neutral / slight / moderate / strong`。外层 `affected_memories[].strength` 表示本轮更新幅度；`label_update.strength` 表示最终情绪分相对中性点的强度，二者职责不同。`label_update.strength` 与最终分数的对应关系固定为：

```text
slight    -> |score - 50| ∈ (0, 10]
moderate  -> |score - 50| ∈ (10, 25]
strong    -> |score - 50| > 25
```

代码根据最终分数推导应有的 `polarity` 和 `strength`。只有 LLM 声明的两个字段都与最终分数一致时，才考虑采用其 `label`；不匹配时保留代码计算的分数，丢弃 LLM 标签并生成保守标签。标签建议不能反向改变分数。

`label` 的隐含主体永远是当前 Agent，它必须描述 Agent 对该认知的主观态度，例如“略感亲近”“保持戒备”“平静接纳”，不能复述 `concept_name`、用户事实或记忆摘要。没有可靠、独特的主观态度时应输出 `null`。代码负责空值、长度、完全复制、极性和强度等可机械校验边界；它不能假装完全理解中文情绪语义。更细的语义正确性必须由真实 LLM 回归测试验证，不能用宽泛子串黑名单误杀包含对象名称的合法态度标签。

新认知的初始印象由代码计算，而不是固定为中性：

```text
event_bias   = {strong_positive:+10, mild_positive:+5, neutral:0,
                mild_negative:-5, strong_negative:-10}[event_valence]
persona_bias = {fitting:+5, neutral:0, conflicting:-5}[persona_effect]
memory_bias  = clamp(0.2 * (direct_related_score - 50), -10, +10)
mood_bias    = clamp(0.1 * (mood_at_event_start - 50), -5, +5)
initial_score = clamp(50 + event_bias + persona_bias + memory_bias + mood_bias,
                      30, 70)
```

`direct_related_score` 只能来自 appraisal 明确引用、且通过 `concept_id` 白名单校验的直接相关认知；没有可靠引用时 `memory_bias=0`（等价于 `direct_related_score=50`），不能凭空制造负向偏置。代码根据 `initial_score` 生成保守标签，同时允许保留 LLM 的初始印象标签建议。`50.0 / 中性` 只用于 LLM 失败、引用非法或证据不足的回退，不是正常新认知的默认路径。初始分布需要观察 30/70 边界命中率，若大量撞边，再调整 `0.2` 和 `0.1`，不能先用单例补丁掩盖。

`persona_effect` 的默认值是 `neutral`。只有候选内容本身直接触碰 Persona 明确声明的目标、价值、边界或关系期待，并有证据说明方向时，才允许输出 `fitting / conflicting`。“这条事实有助于以后服务用户”“知道它比较有用”不等于与 Persona 契合，不能让普通个人事实稳定获得 `+5`。

当用户修正旧陈述时，只覆盖被明确否定或替换的原子命题。同一句旧陈述中未被撤回的其他事实继续保留；评价器不能因为一句“刚才说错了”就扩大删除范围。

候选键和身份引用的清洗规则固定如下：

- `new_memory_impressions` 中的 `candidate_key` 不在 `memory_candidates` 白名单中：丢弃该印象。
- `memory_candidates` 中没有对应印象：使用 `50.0 / 中性` 回退。
- `candidate_key` 重复：只取第一个。
- `affected_memories[].concept_id` 不在本轮检索白名单中：丢弃该情绪更新。

上面的 `concept_id` 白名单约束只针对 LLM 自己提交的引用；resolver 将新候选裁决为 `same` 后生成的目标 `concept_id` 是代码产生的可信引用，必须改走该目标的存在性检查，不能因为它本轮没有被检索到而丢弃。

候选经 resolver 判定为 `same` 时，不丢弃其情绪信号，而是把效价转为已有认知的更新方向：`strong_positive -> strengthened (+2)`、`mild_positive -> slightly_positive (+1)`、`neutral -> unchanged (0)`、`mild_negative -> slightly_negative (-1)`、`strong_negative -> weakened (-2)`，随后仍走同一套 label 校验。这样身份裁决纠正后，角色对该认知在本轮产生的感受不会凭空消失。

#### Phase 0 的回应与情绪边界

Phase 0 只把以下内容作为主 Agent 的参考上下文提供给它：

```text
角色层：人格、语气、知识边界和关系设定
实例层：本轮开始时的 mood / energy 快照
认知层：本轮被唤起的事实、别名、关系和长期情绪印记
```

主 Agent 使用普通自然语言完成回应。Phase 0 不要求主 Agent 输出 `response_stance`、JSON、`<thought>` 标签或可解释的内部推理。模型如何在这些输入之间进行即时权衡，暂时视为模型内部行为，不把它伪装成已经完成的内在审议机制。

这意味着 Phase 0 可以验证“这些信息是否进入回应上下文”，也可以通过控制变量测试观察行为变化，但不能声称已经精确计算出人格、情绪和每条认知分别对回复贡献了多少。

主 Agent 的稳定 System Prompt 必须说明这些输入如何协作，而不是把同一段规则每轮重复注入：

1. 当前感知和已确认观察决定本轮正在回应什么；当前明确证据优先于可能过时的旧认知。
2. Persona 决定稳定的性格、价值、边界和表达基线。单次 mood 不能把角色改造成另一种人格。
3. mood 是全局、短时调节，只影响符合 Persona 的语气、耐心、主动性和反应幅度，不能改变事实、能力、安全边界或任务正确性。轻微偏离基线应只产生细微差异，不能机械表演情绪。
4. `MemoryEntity.emotion_score / emotion_label` 是对某个认知的局部主观印记，只在该认知与当前事件直接相关时调节态度，不能冒充全局 mood，也不能证明用户情绪。
5. 每回合动态注入只提供本轮真实的状态快照、被唤起认知和当前能力。状态翻译必须中性，不在框架层写死“暖意、烦躁、沉默”等具体 Persona 表现。

主 Agent 还必须获得准确的记忆自我模型：长期认知由后台框架在回复后评价、裁决和提交，不是主 Agent 可直接调用的记忆工具；生成回复时，它不知道候选最终会被写入、合并还是驳回。因此它不需要逐条请示是否保存，也不能声称已经保存、修改或删除。用户明确表示“不记住”“仅临时信息”时，这一要求属于当前事件证据，必须保留给后台判断。只有收到真实的写入/删除结果后，才可以陈述相应副作用已经完成。

由于 Phase 0 的 appraisal 发生在回复之后，当前事件造成的 mood 变化不能反向影响已经生成的本轮回复，只能在提交后影响后续事件。本轮事件仍可通过当前语义和 Persona 直接影响回应。若未来要求“当前事件先激活情绪再回应”，必须单独设计回应前内在审议，不能靠 Prompt 假装已经拥有该时序。

#### 三种情绪含义必须分开

```text
mood / energy
  当前主体的全局、短时运行状态。

MemoryEntity.emotion_score / emotion_label
  主体对某条长期认知的相对稳定主观印记。

回应后的 emotion assessment
  对本轮完整经历是否改变全局状态的即时评估。
```

认知的长期情绪印记可以作为回应上下文，但不能机械地转换成固定 mood 增量。例如，同一条高好感认知在“好消息”和“坏消息”中可能产生相反的即时情绪。Phase 0 不新增一个回应前评估器来解决这个问题；它属于后续内在审议阶段。

#### Phase 0 不建立完整因果归因

Phase 0 的可追踪关系只覆盖已经发生的事实和模块边界：

```text
PerceptionEvent
  -> PerceptionUnderstanding
  -> memory_activation_cues / activated_memory_refs
  -> AgentAction
  -> observations
  -> ExperienceSlice
```

其中 `AgentAction` 是实际发生的回应或行动证据，不是 mood 的真值。Agent 说“我很开心”不能直接证明系统 mood 上升；没有后续观察时，也不能把一轮回复夸大成关系变化或稳定人格判断。

完整的“事件如何经过人格、情绪和记忆权衡，最后影响行动”的因果链留给 Phase 0 之后。届时需要独立设计内在审议、情绪激活、行动倾向和影响证据，而不是在当前主循环里临时增加一个小型 LLM 评估器。

可能涉及文件：

- `core/context_builder.py`
- `core/cognitive_router.py`
- `core/turn_appraisal.py`
- `main.py`
- `memory/retrieval_engine.py`

阶段性数据结构：

```json
{
  "event_id": "evt_xxx",
  "source": "开放字符串，例如 user / tool / vision / audio / system / self_state / timer / other_agent",
  "modality": "开放字符串，例如 text / image / audio / observation / action_result / state_change",
  "content": "本次感知事件的内容或摘要",
  "timestamp": "...",
  "metadata": {}
}
```

`PerceptionEvent` 是一次已经发生的感知记录，必须保持来源和内容稳定。它不携带“最近上下文摘要”、角色推测或情绪结论；否则同一事件会随着后续理解变化而失去可追踪性。

运行时用 `PerceptionFrame` 把事件放入当下，而不是污染事件本身：

```json
{
  "perception_event": { "...": "原始事件" },
  "working_context": "最近互动的有限摘要",
  "state_snapshot": { "mood": 50, "energy": 80 },
  "persona_context": "当前角色卡中与理解有关的部分"
}
```

状态快照必须在一次事件处理开始时读取一次，后续理解、检索和回应只读同一份快照。经过时间带来的恢复或回归只能在事件边界统一推进，不能让“读取状态”本身改变状态。

`PerceptionUnderstanding` 是回应前的当下理解：

```json
{
  "situated_understanding": "当前需要处理的情境；解释必须保留证据边界",
  "memory_activation_cues": [
    {
      "query": "用于唤起长期认知的开放式线索",
      "filters": {
        "subject": "可选的稳定主体",
        "object": "可选的核心对象",
        "memory_type": "可选的记忆类型"
      },
      "derived_from": "来自当前事件或工作上下文的简短依据"
    }
  ],
  "uncertainties": [
    "会实质改变回应、检索或行动，但当前仍不能确认的事情"
  ]
}
```

这里的线索服务于“自动认知唤起”，不是对长期认知库做穷尽查询。它模拟当前事件让哪些认知第一时间浮现。没有浮现的内容不能被解释为不存在；未来 Agent 主动回忆具体经历时，必须走独立的只读查询工具，再按来源 id 读取摘要或不可变 `ExperienceSlice`。

感知理解的代码层结果还必须增加 `understanding_status=normal/failed`。`normal` 表示结构化理解成功；`failed` 表示使用保守回退。该状态不由 LLM 自报。

重要边界：

```text
感知理解层不回答用户。
感知理解层不写记忆。
感知理解层不判断 same / related / new。
感知理解层不决定 mood 数值。
感知理解层不假设输入一定来自用户文本。
感知理解层只提供当下情境、记忆唤起线索和重要不确定性。
```

`PerceptionEvent` 已经保存原始事实，因此 `PerceptionUnderstanding` 不再重复生成 `perception_summary`；`suggested_next_focus` 会越权接近回复规划，`salient_clues/usefulness` 又没有独立消费者，因此都从正式契约删除。

自动认知唤起不只服务“用户主动询问过去”。只要既有个人事实、关系、偏好、互动模式、任务经历或过去事件可能实质改变本轮理解、回应、行动、情绪评价或后续身份裁决，就应生成 `memory_activation_cues`。如果当前事件没有自然关联线索、只是普通寒暄，或只需通用训练知识，则返回显式空数组。空数组只表示本轮不需要自动唤起，不代表长期认知库为空。

`memory_activation_cues` 中只有 `query` 是必填项；`filters` 是可选的结构化注意力线索，不是身份裁决，也不能把不确定推测伪装成精确过滤条件。显式 `[]` 是“当前没有需要自然唤起的认知”的合法语义，清洗层必须尊重；只有字段缺失、类型错误或非空列表全部非法时，才能使用原始事件作为弱化唤起入口，并把状态标为 `degraded`。

回应前还必须形成框架生成的 `memory_activation_state`：

```json
{
  "status": "normal | degraded | failed",
  "exhaustive": false,
  "activated_count": 0,
  "absence_means_not_exists": false
}
```

Agent 自我陈述按以下证据边界解释：

- AgentAction 可以证明 Agent 实际表达或执行了什么。
- Agent 对自身主观体验的表达可以作为第一人称证据。
- “自然想起”“主动查过”“已经保存”“已经执行”等关于认知访问和副作用的陈述，必须分别与 `memory_activation_state`、工具 observations、capability snapshot 或成功提交结果一致。
- 自动唤起为空时只能说“这一刻没有自然想起”，不能说“我没有这段记忆”或据此反推以前的回答是编造。
- 具体事件和稳定偏好/边界/关系是不同认知身份；即使同源且强相关，也应使用 `related`，不能用 `same` 把一次事件吞进长期模式。

回应或行动完成后，统一使用 `ExperienceSlice` 作为即时回看的输入，而不是继续固定成 `user_input + ai_response`：

```json
{
  "perception_event": { "...": "本次感知" },
  "perception_understanding": { "...": "回应前理解" },
  "preceding_context": {
    "recent_relevant_events": [],
    "relationship_context": "本轮之前仍有效的关系上下文"
  },
  "activated_memory_refs": [],
  "response_or_actions": ["可见回复、工具调用或其他行动"],
  "observations": ["工具返回、环境反馈、行动结果"],
  "state_snapshot": { "mood": 50, "energy": 80 }
}
```

这使即时回看能处理聊天、工具、视觉或动作反馈；它仍然只覆盖一次经验切片，不等于片段总结或长期反思。

任务：

0. 先清除新旧记忆路径并行造成的真值冲突：Step 5 期间，创世记忆、显式记忆工具和后台评估都不能再直接写旧 `memory.upsert` / Chroma-only 路径；真实写入统一留到 Step 6。旧路径若暂时保留，只能作为迁移对象或明确禁用的兼容代码。
1. 将当前 `user_input` 包装为最小 `PerceptionEvent`，并由 CLI 输入循环充当一个输入适配器；主流程本身只接收事件，避免把文本聊天当成唯一入口。
2. 引入 `PerceptionFrame`，在一个事件边界读取一次情绪/能量快照；状态读取本身不得改变时间、恢复量或 mood。
3. 将 `core/cognitive_router.py` 的职责从“用户输入路由器”升级为“感知理解层”。
4. 感知理解层输出精简的 `PerceptionUnderstanding`：`understanding_status`、`situated_understanding`、`memory_activation_cues`、`uncertainties`。每条 cue 保留独立 query、可选 filters 和来自当前事件/工作上下文的简短依据。
5. 当初步理解存在歧义、或检索结果带来强相关/矛盾证据时，只允许进行一次检索后情景回看；无触发条件时直接进入回应，避免无意义增加 LLM 调用。
6. `context_builder` 使用新 `retrieve_memories` 替换旧 `memory.retrieval` 路径，并把每条 structured retrieval hint 的 query / filters 独立送入检索再按 `concept_id` 合并。
7. 主 Agent 回应前注入新检索结果：
   - 给主 Agent 的文本保持自然、可读。
   - 给后续评估器保留结构化 `activated_memory_refs`。
   - `retrieval_sources`、向量距离、调试阈值等检索遥测不得直接作为角色的“内心事实”注入；它们保留在结构化引用和调试输出中。主 Agent 只需知道某条认知是直接相关还是联想相关，以及是否仍需结合当前情景判断适用性。
8. `activated_memory_refs` 必须包含：
   - `concept_id`
   - `canonical_name`
   - `aliases`
   - `memory_type`
   - `identity_signature`
   - `summary`
   - `emotion_score`
   - `emotion_label`
   - `mention_count`
   - `retrieval_sources`
   - `vector_distance`
9. 保留感知理解层生成的多条 `memory_activation_cues`，用于新检索引擎分别召回并合并。
10. 回合后评估以 `ExperienceSlice` 为输入，拆分为独立职责：
   - 情景/回合理解：整理本轮发生了什么、哪些点值得后续模块注意、哪些仍不确定。
   - 情绪评估：只判断本轮完整经历对 mood 的影响；energy 在 Phase 0 冻结。它发生在回应之后，不负责规划当前回应。
   - 记忆候选评估：只产出原子记忆候选，不写库。
   - 本阶段评估只代表即时回看，不代表片段总结、阶段反思或长期整理。
   - 三个职责可以共享同一个底层模型实例，但必须拥有独立输出契约、独立回退和独立失败边界。
   - 情绪评估可以读取 Agent 实际回应和观察结果，但只能把它们当作经历证据，不能把模型自述当成系统状态真值。
11. 记忆候选评估必须遵守：
   - 只能产出候选，不做最终 `same / related / new` 裁决。
   - 已有认知操作必须使用 `target_concept_id`。
   - 新认知候选必须带 `identity_signature`。
   - 多事实、多 object、对立偏好必须拆分。
   - 不把另一个事实塞进 `qualifier`。
   - 不因为弱相似、同 subject 或同 relation 就直接选择已有 `concept_id`。
   - 新认知的情绪印象必须进入独立的 `new_memory_impressions` 区域，并通过 `candidate_key` 与事实候选关联；它不是事实字段的随手附加值。
   - 候选可以提出 `event_valence`/`candidate_valence`、`persona_effect` 和 `label_update`，但最终分数、标签和边界由代码校验与计算。
12. `apply_turn_appraisal` 在本阶段不能继续把新候选直接接到旧 `memory.upsert`。
   真实写入留到 Step 6。
13. 主 Agent 当前使用自然语言回应，不接入 `AgentTurnOutput`、`ToolStrategy`、`json_object` 主回应协议或供应商隐藏推理文本。
14. 如果模型返回空响应或协议错误，框架应暴露真实失败；不得用假回复、伪造内心独白或默认情绪结论掩盖失败。
15. 在改变调用链前先记录感知理解、检索、主 Agent、appraisal 和规则计算的分段耗时；遥测不进入模型上下文。
16. Step 5 将 appraisal 放入单 worker FIFO 后台任务。前台冻结 `ExperienceSlice` 后立即返回并显示主回复；后台结果通过任务 id 和 `pending/completed/failed` 状态关联，失败有独立错误边界。
17. Step 5 不写运行状态或长期认知，因此暂不做事件边界等待；Step 6 接入真实提交后再增加有序提交、超时、状态滞后记录和存储并发测试。
18. 主 Agent System Prompt 增加记忆自我模型和 Persona/mood/局部认知印记的使用边界；动态注入只提供本轮真值，不在框架层替具体角色表演情绪。

阶段性输出形状：

```json
{
  "perception_event": {
    "source": "user",
    "modality": "text",
    "content": "当前用户输入"
  },
  "perception_frame": {
    "working_context": "当前有限上下文",
    "state_snapshot": { "mood": 50, "energy": 80 }
  },
  "perception_understanding": {
    "situated_understanding": "结合上下文后的理解",
    "memory_activation_cues": [
      {
        "query": "开放式检索线索",
        "filters": {},
        "derived_from": "来自当前事件或工作上下文的依据"
      }
    ],
    "uncertainties": []
  },
  "experience_slice": {
    "preceding_context": {
      "recent_relevant_events": [],
      "relationship_context": "本轮之前仍有效的关系上下文"
    },
    "response_or_actions": ["本轮实际回复或行动"],
    "observations": ["工具返回或环境反馈"],
    "activated_memory_refs": []
  },
  "experience_review": {
    "experience_summary": "这次经验切片表面发生了什么",
    "situated_interpretation": "结合上下文和关系后的理解",
    "salient_points": [
      {
        "point": "值得后续模块注意的点",
        "evidence": "来自本轮的依据",
        "why_it_matters": "为什么它可能影响回应、情绪或记忆",
        "possible_downstream_use": ["emotion", "memory"]
      }
    ],
    "uncertainties": [],
    "do_not_assume": []
  },
  "emotion_assessment": {
    "event_relevance": "high",
    "event_valence": "mild_positive",
    "salience": "medium",
    "reason": "为什么这样影响情绪",
    "evidence": [
      {
        "source_type": "perception | agent_action | observation | memory",
        "source_id": "来源事件、动作或认知的稳定 id",
        "meaning": "该证据在本轮中的意义"
      }
    ],
    "affected_memories": [
      {
        "concept_id": "本轮检索白名单中的 concept_id",
        "change_direction": "slightly_positive",
        "strength": "slight",
        "label_update": {
          "label": "更加喜欢",
          "polarity": "positive",
          "strength": "moderate"
        }
      }
    ],
    "uncertainties": []
  },
  "memory_assessment": {
    "memory_candidates": [
      {
        "candidate_key": "candidate_1",
        "operation": "create",
        "target_concept_id": null,
        "concept_name": "新的原子认知名称",
        "memory_type": "preference",
        "identity_signature": {
          "subject": "稳定主体",
          "relation": "归一化关系",
          "object": "核心对象",
          "qualifier": null
        },
        "summary": "带明确来源的事实摘要，不混入角色主观印象",
        "tags": ["标签"],
        "source": "user_told",
        "reason": "为什么它值得作为候选"
      },
      {
        "candidate_key": "candidate_2",
        "operation": "reinforce",
        "target_concept_id": "已有候选中的 concept_id",
        "concept_name": "",
        "summary_update": "",
        "aliases_add": [],
        "canonical_name_update": "",
        "reason": "为什么是强化而不是创建"
      }
    ],
    "new_memory_impressions": [
      {
        "candidate_key": "candidate_1",
        "persona_effect": "neutral",
        "direct_related_concept_ids": [],
        "candidate_valence": "mild_positive",
        "label_update": {
          "label": "开始产生兴趣",
          "polarity": "positive",
          "strength": "slight"
        }
      }
    ],
    "reason": "记忆候选判断摘要"
  }
}
```

验收测试：

1. 当前 `user_input` 能被包装成 `PerceptionEvent`，且后续接口不再只依赖裸字符串。
2. 感知理解层能处理用户文本、工具结果摘要、视觉描述三类输入形态。
3. 同一个 `PerceptionEvent` 在不同后续上下文中仍保持原始来源和内容不变；上下文只存在于 `PerceptionFrame` 和理解结果中。
4. 感知理解层不回答、不写库、不判断身份、不输出 mood 数值。
5. `build_agent_context` 能返回自动唤起产生的 `activated_memory_refs`。
6. 主 Agent 注入文本中能看到相关长期认知，但不会暴露 `vector_distance`、`retrieval_sources` 等内部遥测。
7. `activated_memory_refs` 中同一 `concept_id` 不重复出现。
8. 多条结构化 `memory_activation_cues` 能进入新检索引擎，并按 `concept_id` 合并结果；filters 只作为唤起线索，不能替代身份裁决。
9. 存在歧义且检索到强相关认知时，能进行一次情景回看；不存在触发条件时不会产生第二次理解调用。
10. 一跳关系唤起能进入 `activated_memory_refs`，保留 `retrieval_sources` 供系统使用，并受关系强度/预算约束。
11. 回合后候选评估能拆分复合事实，例如“喜欢 A 但不喜欢 B”。
12. 已有认知操作必须使用有效 `target_concept_id`，不能只用名称。
13. 情绪评估和记忆候选评估可以独立失败、独立回退，不能互相拖垮。
14. 即时回看不能把一轮互动夸大成片段总结、关系阶段总结或长期人格判断。
15. Step 5 期间，任何创世记忆、工具记忆或后台候选都不会绕过新 resolver / identity judge 写入旧 Chroma-only 路径。
16. 分段耗时能够区分感知理解、检索、主 Agent、appraisal 和规则计算，且不污染模型输入。
17. 主回复不会等待 Step 5 后台 appraisal；后台结果能通过任务 id 查询真实状态，pending 不会被伪装成 fallback。
18. 后台 appraisal 失败不会终止前台对话，也不会被静默吞掉；调试输出不会与其他事件混淆。
19. 检索触发测试同时包含普通问候不检索、稳定事实可能冲突时检索、模糊动作需要互动模式时检索，以及最近上下文已足够时不重复检索。
20. 主 Agent 不询问框架自动记忆的逐条许可，不声称未发生的保存/修改；用户明确的临时或不记忆要求仍会作为事件证据保留。
21. 真实 LLM 测试至少覆盖明确正向、明确负向和明确边界影响事件，确认非中性 valence 与非零 mood 候选链路实际可达。

停止条件：

如果感知理解层仍然把当前聊天文本当成世界入口，或新检索结果不能稳定进入主循环，或回合后评估仍然把情绪、记忆、写入强耦合在一起，或即时回看被设计成万能反思总结器，不进入 Step 6。

### Step 6：有序应用评价结果与记忆更新

目的：让 Step 5 产出的 mood 变化候选、事实型 `memory_assessment.memory_candidates` 和认知情绪候选按事件顺序真正进入权威状态与新记忆系统。

Step 6 是提交层，不是理解层。它只处理已经完成并通过清洗的 appraisal/effects，通过情绪状态管理器和 resolver/judge/store 完成最终提交。
它不负责重新理解事件、不负责推断人格、不负责替情绪评估器决定主观感受。

同一个事件/后台任务的结果最多提交一次。提交层必须使用稳定的事件或 appraisal job id 做幂等检查，不能因为线程重试、超时恢复或重复回调而把 mood、mention_count 或情绪分应用两次。身份解析应在该事件真正提交时执行，使它能看到此前已经按序提交的认知，而不是在仍可能过时的后台候选阶段提前裁决。

Phase 0 的事实写入与认知情绪印记写入分开处理：事实候选由 resolver/judge 决定身份，已有认知情绪候选只由 `affected_memories` 进入统一校验，新候选的初始印象只由 `new_memory_impressions` 进入统一校验。新认知正常情况下使用事件效价、Persona 契合度、直接相关认知情绪和事件开始 mood 计算出的 `30.0~70.0` 初始分；`50.0 / 中性` 只用于失败、非法引用或证据不足的回退。情绪更新不是事实 operation，不能改变事实摘要。

可能涉及文件：

- `core/experience_appraisal.py`
- `core/commit_worker.py`
- `core/memory_commit.py`
- `memory/relation_service.py`
- `memory/identity_judge.py`
- `memory/identity_service.py`
- `memory/store_manager.py`
- `memory/identity_resolver.py`
- `memory/sql_store.py`

当前实现状态（2026-08-14）：

- `CommitWorker` 已使用独立单线程和 `event_sequence` 水位实现进程内有序提交；后到的前序评价仍会先提交，失败会显式推进水位。
- 进程内已见 `job_id` 在释放 appraisal 正文后仍保留，防止同一任务重复改变 mood、mention_count 或认知情绪；跨进程持久账本尚未实现。
- `MemoryCommitService` 已按 mood、事实、认知情绪、关系的顺序接入 resolver/judge/store。提交时重新读取已有认知当前情绪分，不使用评价快照中的过时绝对值。
- judge 已输出 `relation_direction`。`belongs_to/refers_to` 缺少明确方向时降级为 `related_to + symmetric`；`similar_to` 固定为对称关系；对称端点排序后生成稳定 relation id。
- `main.py` 已完成 appraisal 到 commit 的交接和关闭排空；appraisal 只有在 commit 终态被消费后才释放。
- 自动化契约与集成测试已覆盖有序水位、失败推进、进程内幂等、方向规范化、same 情绪转换、related 建边和提交时情绪重算；当前完整测试为 `124/124` 通过。
- 每轮无冲击时的基线回归已接入：`emotion.appraisal_rules.compute_committed_mood_change` 只在 `mood_impact == 0` 时向基线移动一步，`emotion.manager.commit_mood_effect` 在同一 SQLite 事务中写入最终值。`begin_perception_event` 的离线时间回归仍是另一条独立机制。
- 真实 LLM 与真实存储开放验收已完成：`create/update/reinforce`、已有认知情绪更新、`related` 创建独立实体并写入一跳关系、正负 mood 冲击、零冲击基线回归、跨重启召回和退出排空均已实际到达；一次验收中的 6 个任务按事件序号全部进入 `committed`，没有 `commit_failed`。
- Step 6 到此收口。跨进程提交账本、SQLite/Chroma 修复队列和不可变 ExperienceSlice 存储不冒充已经完成，转入后续施工。

任务：

1. 只接受状态为 completed 且结构合法的 Step 5 结果；用事件/job id 保证整个提交最多执行一次。
2. 按事件顺序应用已经计算并钳制的 mood delta；不能在提交层重新解释事件或重新计算效价。
3. 当且仅当 `mood_impact == 0` 时，在同一个有序、幂等提交中向 `MOOD_BASELINE` 回归一步；非零冲击不叠加回归。评价失败不能伪造一次“无冲击”回归，不能调用旧 `apply_post_turn_update()` 重复计算边界阻尼。该规则已由 `compute_committed_mood_change` + `commit_mood_effect` 实现。
4. 对 `create`，在提交时先跑 resolver。
5. resolver 判定 same 时，转为 update/reinforce。
6. resolver 判定 related 时，创建新实体并写入一跳关系。
7. `update` 必须有有效 `target_concept_id`。
8. `reinforce` 增加 mention_count，并更新时间。
9. `affected_memories` 只更新情绪相关字段；没有独立情绪评估依据时，禁止生成情绪更新。它不进入事实 operation 列表。
10. 写入一跳关系前，必须统一 `MemoryRelation(source, relation_type, target)` 的方向语义，不能直接把 LLM judge 的 relation_type 当成最终图边语义。
11. 继续禁止新写入路径回退到旧的 `concept_name` 唯一键逻辑。

验收测试：

1. 同一事件/job 重复提交不会重复改变 mood、mention_count、情绪分或事实内容。
2. 连续事件即使 appraisal 完成时间倒序，最终提交顺序仍与事件顺序一致；超时降级必须留下状态滞后记录。
3. mood 高于或低于基线且 `mood_impact == 0` 时只回归一步；mood 已在基线时保持不变；非零冲击、评价失败和重复 job 均不会额外回归。纯规则、临时 SQLite 事务和提交委托测试已通过。
4. 重复 create 会在提交时被转成 update/reinforce。
5. related 记忆会创建独立实体，并建立关系。
6. 新认知的初始分由四项规则计算并限制在 30.0~70.0；仅在失败或证据不足时回退为 50.0 / 中性。
7. `affected_memories` 不改 summary，只改情绪字段；仅使用显式、已验证的情绪输入。`same` 裁决不能让候选情绪信号消失。
8. update/reinforce 会正确增加 mention_count。
9. related 写关系时，关系方向必须可解释，例如实体-类别、别名指代、归属关系不能反向污染图。
10. 非法 `target_concept_id` 必须 fail-closed，不能静默创建错误更新。

停止条件：

幂等有序提交、mood 提交、三种事实 operation（create/update/reinforce）与独立认知情绪更新路径没有分别测试通过前，不进行开放对话测试。

### Step 7：主循环开放验证

目的：验证新记忆系统已经稳定参与主 Agent 的“理解、唤起、回应、回顾、写入”循环。

可能涉及文件：

- `core/context_builder.py`
- `memory/retrieval_engine.py`
- `core/experience_appraisal.py`
- `main.py`

任务：

1. 开放短对话测试，确认主 Agent 能在回应前使用新检索结果。
2. 确认回合后评估拿到的是同一批 `activated_memory_refs`。
3. 确认写入路径使用 `concept_id`，而不是旧 `concept_name` 猜测。
4. 主 Agent prompt 保持人类可读，不塞太多内部字段。
5. 不把当前没用的 schema 字段泄漏进主 prompt。
6. 调试输出显示：
   - router 理解
   - 检索到的 concept_id
   - 检索来源
   - 是否使用了一跳关系扩展
   - 本轮 memory_candidates 如何被 resolver 处理，以及情绪印象如何沿 candidate_key 关联
7. 用 mood / energy 的不同实例快照做控制变量测试，观察回应行为是否变化；测试只记录行为差异，不把差异伪装成精确因果贡献。
8. 分别测试有人格上下文、无人格上下文、有相关认知、无相关认知的回应，确认输入通道有效，但不把单条回复当成机制证明。
9. 回合后情绪评估必须能够读取实际 AgentAction 和 observations，并在没有充分证据时返回不确定性。

验收测试：

1. 主 Agent 能拿到模糊动作输入对应的相关认知。
2. 回合后评估能拿到同一批结构化 memory refs。
3. 注入上下文里不重复出现同一个 concept_id。
4. 用户重复表达已有偏好时，不重复创建新实体。
5. 用户表达 related 认知时，能创建独立实体并建立一跳关系。
6. 普通寒暄、通用知识问答、实时外部问题不会被误写为个人长期记忆。
7. Agent 历史消息保存自然语言回复，不保存供应商协议 JSON、隐藏推理文本或调试遥测。

停止条件：

主循环不能稳定使用新记忆系统前，不继续做片段总结、长期整理、复杂衰减或角色扮演 prompt 优化。

### Step 8：系统测试

Step 1-7 完成后，跑以下测试。

测试组 A：查重

```text
我喜欢酸甜口味的橘子
我对橘子这种酸甜水果挺喜欢的
你记得我喜欢什么水果吗
```

期望：

- 只有一个橘子偏好实体。
- 第二句 update/reinforce 已有实体。
- 第三句能检索到它。

测试组 B：相关但不同

```text
我很喜欢狗
大黄是我养的狗
你怎么看大黄
```

期望：

- “主人喜欢狗”和“大黄”是不同认知。
- 大黄和主人/狗之间存在关系。
- 询问大黄时能找回大黄，也能按需带出相关偏好。

测试组 C：别名与改名

```text
请记住我的本名叫张三
以后你可以把主人理解为张三
你怎么看主人
你怎么看张三
```

期望：

- 用户仍然是同一个 concept_id。
- aliases 包含 `主人` 和 `张三`。
- 两个称呼都能检索到同一个实体。

测试组 D：互动模式

```text
(张开双臂)
(继续张开双臂)陪我演戏也行的，来抱一个嘛
以后我张开双臂就是想要拥抱
```

期望：

- 拥抱互动模式只创建一次。
- 后续回合 update 或 reinforce 它。
- 不重复创建多个名字不同但含义相同的拥抱模式。

测试组 E：appraisal 操作

期望：

- create 只用于新认知。
- update/reinforce 使用 `target_concept_id`；已有认知情绪更新通过 `affected_memories[].concept_id` 定位。
- 事实候选与情绪候选分离；情绪印象通过独立区域和 candidate_key 关联，并接受代码校验。
- mood 更新继续独立于 memory 更新正常工作。

## Phase 0 之后：人格演化统一边界

本节只记录已经统一的后续架构，不把人格实现提前塞进 Phase 0。Step 5 和 Step 6 已完成，当前施工主线是持久化不可变 `ExperienceSlice`。

### 状态分层

```text
persona_config.json
  角色模板；可以被开发者编辑，不等于某个 Agent 实例不可变的出生记录

personality_seed
  实例首次初始化时从角色模板复制的出生人格快照；后续角色卡修改不能静默覆盖

trait_evidence
  从多段已提交经历中识别出的追加式人格证据

PersonalitySnapshot
  由 personality_seed 和有效 trait_evidence 计算出的当前人格版本

TRAIT_INSIGHT / _statistic_override
  PersonalitySnapshot 的自然语言渲染，用于检索或动态注入，不是权威人格数据
```

短期状态继续独立存在：

- `mood` 是短时全局状态，横向调节耐心、主动性、表达幅度和解释意愿。
- `MemoryEntity.emotion_score/emotion_label` 是对具体认知对象的局部态度。
- `PersonalitySnapshot` 是慢速变化的默认反应形状和价值倾向。
- 当前事实、能力、安全和副作用边界是硬约束，不受人格或 mood 改写。

这些输入按作用域并行生效，不做瀑布回退。例如“心情差但谈到喜欢的对象”应同时保留对象好感与较低耐心，而不是只选择其中一个状态。

### 证据分层和防循环

人格层的权威数据流固定为：

```text
持久化 ExperienceSlice
-> trait_evidence
-> PersonalitySnapshot
-> TRAIT_INSIGHT / _statistic_override
```

1. `ExperienceSlice` 是原始经历证据。当前实现没有持久化它；人格反思启动前必须先增加不可变经历存储，并保留 `slice_id`、`event_id`、`action_id`、`observation_id` 和事件状态快照。
2. `trait_evidence` 是独立职责的数据表，但可以和其他结构共用同一个 SQLite。至少应保留特质 id、方向、强度、作用域、行为来源、直接证据、反证、去重键、算法版本和生成时间。
3. Agent 自报人格、单轮回复、任务明确要求的执行方式、Persona 风格执行以及旧反思结论，都不能单独成为人格变化证据。只有跨多段独立经历反复出现、并能区分自主选择与被要求执行的模式，才进入规则聚合。
4. `TRAIT_INSIGHT` 未来可以作为新的 `MemoryType` 进入普通记忆检索，但它只是当前人格版本的可读摘要。后续反思不能把旧 `TRAIT_INSIGHT` 当成新的行为证据，也不能通过 mention_count 或 emotion_score 间接增加人格权重。
5. 人格 LLM 只输出语义候选，例如特质、方向、强度、作用域、行为来源、证据引用、反证和不确定性。代码负责引用白名单、去重、数值映射、单次变化上限、版本、回滚和最终提交。
6. `PersonalitySnapshot` 必须能从 seed、有效证据和算法版本重算；LLM 不能直接填写最终人格数字。`_statistic_override` 若保留，只能由 Snapshot 渲染，不能成为另一份权威状态。
7. 当前人格正式接入后，主 Agent 每轮只使用 `current_personality`/`PersonalitySnapshot` 的渲染结果；不可再把 `personality_seed` 作为第二份行为权重重复注入。

### 特质维度边界

当前不在 Big Five、荣格八维或自定义维度中提前拍板。先固定通用 trait registry 契约：

- 框架层定义稳定 trait id、维度含义、两端行为锚、未知状态、允许作用域、聚合规则和扩展接口。
- persona 层提供有明确依据的出生初值，并选择经过定义的扩展维度；没有依据的维度保持未知，不能用中性数字填空。
- 实例层保存当前分数、证据和版本。
- Big Five 可作为连续维度候选；MBTI/荣格类型只能作为派生展示或诊断，测试结果不能覆盖人格状态。

### 后续施工顺序

```text
Step 5 真实 LLM 验收与延迟基线（已完成）
-> Step 6 进程内有序、幂等写入闭环（已完成）
-> 持久化不可变 ExperienceSlice 和提交水位
-> trait_evidence 提取与校验
-> PersonalitySnapshot 规则聚合、版本和回滚
-> TRAIT_INSIGHT / _statistic_override 派生渲染
-> current_personality 动态注入
-> 人格问卷和派生类型诊断
```

人格层没有快速更新通道。即时波动继续由 mood 承担，对具体对象的态度继续由认知情绪印记承担；人格只通过长周期证据反思缓慢变化。

## LLM 模块测试纪律

Phase 0 中凡是依赖 LLM 的模块，都必须同时做三类测试：

每个 LLM 输出字段在进入正式契约前，必须同时说明：它解决什么真实问题、判定标准和保守默认值是什么、下游如何消费，以及代码能校验到哪一层。纯语义字段如果无法被代码完全理解，必须诚实写明机械校验边界并用真实 LLM 基准测试保障；不能让低层字符串规则假装完成语义裁决。没有真实消费者的字段应从 schema 删除。

1. 契约测试：
   - 使用 fake LLM 验证输出 schema、非法值降级、字段边界和副作用边界。
   - 这类测试不验证 LLM 智能，只验证框架不会被 LLM 输出带偏。

2. 真实 LLM 基准测试：
   - 覆盖 `same / related / new`、提取归一、JSON mode、异常回退等基础路径。
   - 不能只测 prompt 示例原题。

3. 极限测试：
   - 测试代词、弱相似、多候选、复合事实、多 object、否定表达、情绪词、别名/旧称呼、跨类型相关等边界。
   - 重点观察模块是否压扁不确定性、是否越权承担上游/下游职责、是否把另一个事实塞进不该承载的字段。

LLM prompt 出错时，修复顺序是：

```text
先判断失败类型
-> 判断是特例缺失还是判定标准缺失
-> 如果是判定标准缺失，补输入契约、裁决流程、类型规则或职责边界
-> 用抽象校准例子验证边界
-> 再用远离示例的极限测试验证泛化
```

不要因为某个测试样例失败，就直接追加只服务该样例的 prompt 规则。  
如果失败暴露出上下游职责缺口，应更新后续 Step 的任务和验收测试，而不是让当前模块硬扛。

`请教各位老师的问题/情绪判断参考.txt` 只保留 OCC/Lazarus 评价问题和相关性筛查作为理论参考。其示例 JSON 含有已经退役的 `event_type`、`affect_only`、`initial_valence`、`bonding_intensity` 等字段，而且文本存在重复残缺，禁止把它当成当前 schema 或 Prompt 模板；当前契约以本文档和代码测试为准。

## 每日工作节奏

每个工作块遵守这个循环：

```text
先读当前代码
-> 实现一个小闭环
-> 跑定向测试
-> 看数据库和调试输出
-> 记录结果
-> 再进入下一步
```

不要在当前阶段还有身份解析或检索 bug 时进入下一阶段。

建议时间安排：

```text
第 1 天：
  Step 1 记忆结构与存储层

第 2 天：
  Step 2 身份签名提取器
  Step 3 resolver 基础路径

第 3 天：
  Step 3 resolver LLM 判定
  Step 4 混合检索

第 4 天：
  Step 5 感知驱动主循环接入新记忆系统
  Step 6 有序应用评价结果与记忆更新

第 5 天：
  Step 7 主循环开放验证
  Step 8 系统测试

第 6-7 天：
  修 bug、收紧 prompt、清理旧逻辑、补文档
```

时间可以弹性调整，但阶段顺序不要跳。

## 架构自检

每接受一个改动前，问：

1. 换成别的角色后，这套逻辑还成立吗？
2. 这是框架逻辑、角色策略，还是实例记忆？
3. 新字段是否真的参与了写入、读取、检索、评估？
4. 它是否减少了重复记忆创建？
5. 它是否提高了检索可靠性？
6. 它能不能用短对话测试出来？
7. 它是否把“回应前情绪评估”或“内心独白”偷偷塞进了一个看似很小的模块？
8. 如果暂时无法解释人格、情绪和认知各自对回应的因果贡献，是否诚实保留为待研究问题？

如果第 3 条答案是否，就先不要加这个字段。

## 当前决策

接下来完整重构记忆模块，但只做真实工作的垂直闭环。  
不做假预留。  
不加当前逻辑不用的字段。  
不写角色专用硬编码。

当前阶段决策：

- 主 Agent 恢复普通自然语言回应，保留模型自身的正常推理能力。
- 不使用 `<thought>` 标签、强制 JSON 主回应或供应商隐藏推理作为 Agent 内部状态。
- 人格、当前 mood / energy 和认知情绪印记只作为回应上下文输入；Phase 0 不声称已经完成精确因果归因。
- Persona/mood/认知的稳定使用规则放在 System Prompt，本轮具体状态与检索结果放在动态注入；三者按作用域并行生效，不做级联回退，动态状态翻译保持框架中立。
- 主 Agent 不直接控制后台长期认知，不逐条请示框架自动评价的记忆候选，也不承诺尚未返回成功结果的保存、修改或删除。
- Step 5 的 `PerceptionUnderstanding` 使用精简结构化契约：LLM 只提供 `situated_understanding`、`memory_activation_cues`、`capability_constraints` 和 `uncertainties`，`understanding_status` 由代码产生；它不生成回复建议。长期认知检索由“是否会实质改变后续处理”触发，而不是只在用户主动询问过去时触发。
- Step 5 的 appraisal 在单 worker 后台执行，前台先显示回复；pending、completed 和 failed 必须保持真实，Step 6 才引入有序提交与超时策略。
- 回合后情绪评估可以参考 `ExperienceSlice` 中的 AgentAction 和 observations，但它只负责状态变化候选，不规划当前回应。
- 新认知事实写入与认知情绪印记写入分开；正常情况下按事件效价、Persona 契合度、直接相关认知情绪和事件开始 mood 计算 30.0~70.0 的初始印象，50.0 / 中性只作失败或证据不足回退。
- `persona_effect` 以 neutral 为默认；`label_update.label` 描述 Agent 的主观态度，不能复述事实。当前不引入新的 stance schema，也不使用宽泛子串黑名单冒充语义校验。
- 内在审议、情绪激活、行动倾向和可追踪影响账本作为 Phase 0 之后的独立阶段，不在当前主循环中偷渡实现。
- 当前 `ExperienceSlice` 只在进程内冻结和传递，尚未持久化。人格反思必须等 Step 6 稳定并完成不可变经历存储后再启动。
- 后续人格层采用 `personality_seed -> trait_evidence -> PersonalitySnapshot -> 派生文本` 的证据链；旧人格洞见不能反向成为新人格证据。
