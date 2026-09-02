# Step 6 完成后待解决问题

更新时间：2026-08-30

## 文件用途

本文件只记录已经通过代码或实际运行确认、但明确延后到 Step 6 稳定之后处理的问题。

Step 6 的进程内有序写入闭环已经通过自动化测试和真实 LLM 开放验收。阶段 2.2 的启动恢复审核问题已修正；当前施工主线切换为 SQLite/Chroma 一致性修复，其他能力继续按优先级排队，不与经历存储混在同一个改动中。

## 已确定的总体顺序

```text
Step 6 有序写入稳定
-> 持久化不可变 ExperienceSlice
-> 建立跨进程持久提交账本和存储修复机制
-> 修正时间上下文和短期对话压缩
-> 建立片段/每日情景摘要
-> 增加只读经历查询工具
-> 建立周/月反思与长期整理
-> 再启动人格证据和 PersonalitySnapshot
```

这里的“持久化”是逐轮完成的：ExperienceSlice 在每轮行动完成后立即追加保存；每日、每周、每月任务只负责生成可重建摘要，不负责保存原始经历。

## 已完成代码施工：自动认知唤起与自我模型

开放测试已经确认：回应前路径应定位为有限、非穷尽的“自动认知唤起”，而不是证明长期认知存在或不存在的完整查询。以下边界已完成代码接入，通过 `124/124` 单元测试，并完成真实 LLM 开放对话验收：

- 使用 `memory_activation_cues`、`activated_memory_refs` 和 `memory_activation_state` 表达自动唤起。
- 空唤起只表示本轮没有内容浮现；感知理解失败时使用 `degraded`，不能让主 Agent 据此否定既有认知。
- `ExperienceSlice` 保存能力快照和本轮认知访问状态，供后台评价核对 Agent 自我陈述。
- Agent 主观自述可以作为自身经历证据；关于检索、工具和副作用的陈述必须有结构化运行状态支持。
- 身份裁决增加跨类型 `same` 保护，具体事件与稳定偏好、关系或互动模式只能是独立但相关的认知。

未来只读经历查询工具仍按本文件后续 P1 方案建设，不提前塞进当前自动唤起闭环。

## P0：通用 checkpoint 总结器不符合项目架构（第一版已替换）

### 已确认事实

- 当前 `core/agent_factory.py` 使用 LangChain `SummarizationMiddleware`，在活动消息约达到 30000 tokens 时触发。
- 默认保留最近 20 条消息；“消息”不等于“回合”，工具消息也会占用数量。
- 触发后，中间件删除当前活动消息状态，再写入一条机器总结和最近消息；LangGraph 会把这个新状态保存为新的 checkpoint。
- 默认总结 Prompt 面向任务型 Agent，要求提取 session intent、summary、artifacts 和 next steps。
- 总结结果以带有 `lc_source=summarization` 元数据的 `HumanMessage` 进入历史；该元数据不会自动成为主 Agent 能理解的来源边界。
- 默认总结调用最多整理约 4000 tokens 的待总结内容，过长部分可能被裁剪。
- 后续达到阈值时可能再次总结先前的总结。当前还复用了温度较高的主 LLM，结果存在表达和判断漂移。

### 风险

- 把机器推断的关系进度、用户意图、角色感受或人格判断伪装成用户陈述。
- 把不确定解释压缩成确定事实，并在重复总结中形成自我强化。
- 丢失事件时间、来源和可回查证据。
- 触发总结的回合会额外同步调用一次 LLM，增加前台延迟。

### 处理要求

- 通用总结器不能被视为长期记忆或情景反思模块。
- 在长时间真实运行前，替换为项目自己的短期上下文压缩器；不能只改几句 Prompt 就宣称问题解决。
- 压缩结果必须明确标记为系统派生上下文，不得伪装成用户消息或原始经历。
- 压缩器只能影响模型工作上下文，不能覆盖不可变经历档案。
- 摘要必须区分明确事实、角色当时的解释和仍未解决的不确定性。

### 当前实现

- `core/working_context.py` 的 `WorkingContextMiddleware` 已替代通用 `SummarizationMiddleware`。
- 达到阈值后，旧消息会交给独立的工作上下文总结 LLM；结果以带 `source=project_working_context` 的 `SystemMessage` 保存，并和最近消息一起组成新的 checkpoint 工作窗口。
- 总结 Prompt 只允许保留已确认事实、Agent 行动、工具事实、时间和未确定内容；禁止总结关系、人格或情绪结论。总结失败时回退为带来源的原始记录，不阻塞主 Agent。
- 当前每轮基础上下文还会由 `core/context_builder.py` 注入最近可用的日、周、月摘要。这些周期摘要直接来自 `ExperienceSlice`，Checkpoint 总结不能替代它们，也不能把它们再次总结。
- 这两类派生上下文都不自动重写或删除 ExperienceSlice；原始切片仍是长期情景记忆和人格反思的唯一证据来源。

### 真实运行发现的问题

旧版本按消息数量切开时，一次主动回忆可能产生一条带多个 `tool_calls` 的 `AIMessage`，随后才是多条对应的 `ToolMessage`。如果保留窗口从这些结果中间开始，checkpoint 就会留下没有前置 `tool_calls` 的孤立 `ToolMessage`；下一轮发给模型时会被 API 拒绝：`Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`。当前实现已在总结前清理孤立结果，并按完整工具调用组保留最近窗口；仍需真实长对话继续观察。

### 已实施与后续观察

- 裁剪前先识别工具调用组：`AIMessage(tool_calls)` 和它的全部对应 `ToolMessage` 必须作为一个整体保留或一起移除。
- 保留窗口的起点如果落在工具结果中间，就向前扩展到对应的 `AIMessage`；如果无法找到对应调用，则丢弃该孤立结果，不把它发送给模型。
- 快照正文可以省略工具返回的长内容，但不能破坏 checkpoint 中的消息配对。完整工具结果仍由 `ExperienceSlice` 保存。
- 已增加测试覆盖：总结成功、总结失败回退、结果使用 `SystemMessage`、多工具调用组不拆开、孤立工具结果清理、普通用户/助手消息不受影响。
- 仍需在真实长对话中观察总结延迟、重复总结后的稳定性，以及总结文本是否始终不越权解释关系、人格和情绪。

### 验收条件

- 同一份原始经历始终可通过稳定 id 回查。
- 主 Agent 能区分用户原话、Agent 行为、系统事件和机器摘要。
- 多次压缩不会把未经证实的关系或情绪推断升级为事实。
- 有测试证明压缩只改变工作上下文，不改变原始 ExperienceSlice。

第一版对应测试：`tests/test_working_context.py`。工作快照仍不是长期情景摘要；摘要生成器对应 `tests/test_experience_summary_builder.py`，不能把它误称为已经完成的日/周/月自动记忆。

## P0：ExperienceSlice 持久提交链尚未完全稳定

### 已确认事实

- 当前已经有 `experience_slices` SQLite 表、追加式写入和按 id/时间范围/线程读取，也已经有 `commit_ledger` 记录 appraisal/commit 状态。
- SQLite/Chroma 不一致时的持久修复队列已完成阶段 3 第一版：`vector_repair_queue` 会记录实体、操作、失败原因、尝试次数、最后尝试时间和下一次重试时间；达到最大次数后进入 `exhausted`，不再自动重试。
- checkpoint 只包含 Agent 运行状态，不能完整替代感知、状态快照、检索引用、行动和观察组成的经历切片。

### 处理要求

- 继续完善追加式、不可变的经历存储验收。向量修复可由 `memory.store_manager.repair_pending_vectors()` 显式触发，也由 `core.vector_repair_worker.VectorRepairWorker` 低频按批次执行；修复时重新读取 SQLite 当前实体，不使用旧快照覆盖权威事实。
- 至少保存 `slice_id`、`event_id`、事件时间、感知来源、理解结果、检索引用、`action_id`、`observation_id`、状态快照和完成时间。
- 原始经历不得被摘要覆盖；更正应通过新记录或明确版本关系表达。
- 写入必须使用稳定 id 幂等，避免重启或重试制造重复经历。
- 认知实体只需暴露创建时间和最近提及时间作为回忆线索；具体回忆由工具按时间范围读取多条 ExperienceSlice。不要把单个时间戳当成某条经历的唯一身份。

### SQLite/Chroma 不一致的当前行为

```text
SQLite 写入成功
-> Chroma 同步失败
-> 写入 vector_repair_queue
-> 原始错误继续向 CommitWorker 报告为 commit_failed
```

这表示“结构化认知已经保存，但检索索引暂时不可用”，不是完整提交成功。后续显式调用修复函数后，任务才会进入 `repaired`。

### 阶段 3 新增的一致性检查

- `memory.store_manager.inspect_vector_consistency()` 比较两边的 `concept_id`。
- SQLite 有而 Chroma 没有：自动加入 `upsert` 修复队列。
- Chroma 有而 SQLite 没有：只返回 `orphaned_in_chroma` 报告，不自动删除。
- `VectorRepairWorker` 启动后先检查一次，之后按配置间隔运行；单次最多处理 `VECTOR_REPAIR_BATCH_SIZE` 条任务。
- `mark_failed()` 根据 `VECTOR_REPAIR_MAX_ATTEMPTS` 和 `VECTOR_REPAIR_RETRY_DELAY_SECONDS` 安排重试；耗尽后保留 `exhausted` 记录，后续需人工查看。

### 验收条件

- 进程重启后能够按 id、时间范围和线程读取完整 ExperienceSlice。
- 同一 `thread_id` 的新进程会从 SQLite 中已有的最大 `event_sequence` 之后继续取号；`event_id` 仍是每个事件的永久唯一身份。
- 同一事件重复提交不会产生两份原始经历。
- 摘要、人格证据和认知候选都能保存并校验来源 slice id。
- 一致性检查能发现双向 id 差异；孤立 Chroma id 不会被自动删除。
- 修复任务达到重试上限后停止自动执行，并能看到最后错误和时间。

## P1：事件时间已保存，并已完成模型输入渲染

### 已确认事实

- `PerceptionEvent.occurred_at` 已保存具体时间。
- 用户事件进入 checkpoint 时，完整事件位于 `additional_kwargs["perception_event"]`。
- 普通用户消息的可见 `content` 仍只有用户原话，因此模型不一定能读取 `additional_kwargs` 中的时间。
- 把系统时间直接追加进 `PerceptionEvent.content` 会污染原始事实，并在调用方传入 `occurred_at` 时产生两个不同时间。

### 当前实现

- `core/model_io.py` 的 `render_perception_times_for_model()` 只创建模型请求用的临时消息副本。
- `core/agent_factory.py` 的 `render_event_times` middleware 在模型调用前统一应用渲染。
- `tests/test_model_io.py` 已验证旧 checkpoint 消息、当前事件和原始消息不变。

### 处理要求

- `content` 继续只保存原始感知内容。
- 在模型调用前，根据结构化 `occurred_at` 创建临时消息副本，为主 Agent 渲染事件时间。
- 临时渲染不得写回 checkpoint 或 ExperienceSlice。
- 时间格式必须统一；未来如改为带时区时间，应统一迁移事件、行动和切片时间，不能只改一个字段。

### 已完成验收

- Agent 能区分“几分钟前”“昨天”和长时间中断。
- checkpoint 中用户原始 `content` 保持不变。
- 旧 checkpoint 中已经带 `perception_event` 元数据的消息也能被正确渲染。
- 显式传入的 `occurred_at` 是唯一权威时间，不会被当前系统时间覆盖。

后续仍需在真实长对话中观察“几分钟前/昨天/长时间中断”的表达质量，但这属于模型表现验收，不再是消息时间没有进入模型输入的代码缺口。

## P1：缺少多时间尺度情景记忆

### 目标边界

```text
原始 ExperienceSlice：不可变证据
片段/会话摘要：一次连续互动的派生索引
每日摘要：当天经历的时间索引和主题概览
周/月反思：跨片段模式，不覆盖每日或原始记录
MemoryEntity：稳定事实认知和局部情绪印记
```

MemoryEntity 的 `created_at` 和 `last_mentioned_at` 只负责告诉 Agent “大约什么时候形成、最近什么时候被提到”。它们不替代情景记忆来源关系；需要具体过程时，查询工具按时间范围返回一组按事件顺序排列的切片。

### 处理要求

- 每份摘要保存覆盖时间、来源 slice id、生成时间、Prompt/算法版本和不确定性。
- `memory/experience_summary_store.py` 已提供 `experience_summaries` 表和追加式保存/读取，并增加 `experience_summary_attempts` 记录摘要生成状态；`memory/experience_summary_builder.py` 已提供不调用 LLM 的片段/每日/周/月稳定生成器；`memory/experience_summary_pipeline.py` 的 `generate_daily_summary(day)` 可以按日期读取、生成并幂等保存 daily 摘要；`memory/summary_scheduler.py` 已在启动时检查昨天、上周和上月，并对失败按次数和时间有限重试。当前仍没有独立的外部定时调度，也不负责周/月的更深层反思。
- 周期摘要的计划规则：每日检查昨天；每周检查上一个完整自然周；每月检查上一个完整自然月。当前已接入 `main()` 启动检查；检查只读 SQLite、不会调用 LLM，失败不阻塞主 Agent。以后如果进程长时间不重启，可由独立定时任务复用同一入口。不需要全天重复检查同一周期。
- “已生成”不等于永远跳过：如果摘要保存的 `source_slice_ids` 与该周期当前实际切片一致，则直接跳过；如果后来补进迟到切片，来源列表发生变化，就生成新的摘要版本。旧摘要和原始切片保留，不覆盖历史。
- 周/月摘要应直接读取对应周期的原始 `ExperienceSlice`，不能只把旧的每日摘要再次总结，避免误差逐层累积。所有周期边界使用实例统一时区，并按自然日、自然周、自然月计算。
- 周/月反思不能只对旧摘要继续做无来源的“总结的总结”；至少要能回查并抽样核对原始证据。
- 摘要生成失败会写入 `experience_summary_attempts`；达到 `SUMMARY_MAX_ATTEMPTS` 后标记 `exhausted`，不再无限重试。失败不会删除原始切片或覆盖已有摘要。
- 关系变化、人格变化和情绪印记不能因为摘要措辞而直接更新，仍要经过各自证据链和提交规则。
- 主 Agent 每轮会看到最近可用的日、周、月摘要，作为较长时间背景，但不会自动接收按当前事件筛选出的相关摘要。长期细节和具体过程仍由主 Agent 在确有回忆需要时调用 `recall_memory` 获取，避免每轮进行全库检索。
- 单轮主动回忆不能无限循环。`config.py` 中的 `RECALL_MAX_CALLS_PER_TURN=10` 是硬上限；`memory.recall_tools.recall_call_budget()` 在一次 `agent.invoke()` 外建立共享的线程安全计数器，即使工具并行执行，第 11 次及以后也返回 `result_status=call_limit_reached`。提示词只负责引导尽快停止，代码层负责最终限制。
- 回忆工具已返回数据覆盖范围，至少包含 MemoryEntity 和 ExperienceSlice 各自的记录数、最早和最晚可查询时间。覆盖范围只描述当前数据库实际存在的记录，不代表完整人生历史，也不能证明范围之外从未发生过事情。
- 旧经历缺少 ExperienceSlice 的部分不安排补录。后续系统从当前已经持久化的经历开始建立摘要和回忆能力。

### 约定的上下文组合顺序

```text
当前事件
-> 最近几轮详细经历
-> 最近可用的日/周/月摘要（长期背景）
-> 本轮自然浮现的少量长期认知
-> （需要时）主 Agent 调用 recall_memory 获取相关摘要和原始经历
```

日/周/月摘要会作为背景注入主 Agent，但它们只用于了解大致时间线，不是原始对话。`recall_memory` 返回的摘要仍只用于定位；如果需要具体过程，再按摘要指向的时间范围读取多条 ExperienceSlice。普通当前事件不需要调用工具。

并行工具调用的 10 次硬限制已通过回归测试：同一预算上下文内只放行前 10 次，后续调用返回 `call_limit_reached`。真实长对话仍需观察 Agent 是否能在拿到足够结果后主动停止。

### 验收条件

- 摘要中的每项重要结论都能追溯到原始经历。
- 删除并重建派生摘要不会改变原始经历和权威认知。
- 更换 Persona 后，同一原始事件仍保持不变，新的解释不会反写旧事实。

## P1：只读经历查询工具（第一版已完成）

### 目标

主 Agent 平时只接收短期详细上下文和本轮自然浮现的认知；当用户提到某段往事、给出时间线索或当前上下文不足时，应调用 `recall_memory`，先查长期认知/摘要，再按时间范围读取当时的一组具体经历。

### 边界

- 工具只允许查询，不允许主 Agent 直接创建、修改或删除长期记忆。
- 查询结果必须标明时间、事件顺序和内容类型，不能把摘要伪装成原文。
- 第一阶段只做“搜索经历”和“读取经历详情”，不加入自动遗忘、改写或复杂多跳推理。
- 工具结果作为新的感知事件进入主循环，而不是偷偷写进 System Prompt。

### 当前实现

- `memory/recall_tools.py` 注册只读 `recall_memory` 工具，同时返回 `memories`、`summaries` 和 `experiences` 三个区域。
- 认知查询复用 `memory/retrieval_engine.retrieve_memories()`；`concept_id` 走 SQLite 精确读取。
- 经历查询支持关键词、`start_at`、`end_at`、`thread_id` 和 `concept_id`。关键词第一版在 `experience_slices.payload_json` 中匹配，覆盖原始事件、Agent 行动和观察，结果最多 50 条。
- 摘要目前按时间范围读取，再在摘要文字中做关键词筛选；摘要只用于导航，完整过程仍从 ExperienceSlice 读取。
- 主 Agent 的工具调用和工具返回分别记录为 `AgentAction(tool_call)` 与 `PerceptionEvent(source=tool, modality=memory_recall_result)`，随本轮 ExperienceSlice 保存。
- 查询不会写入 MemoryEntity、不会修改 mood，也不会改变 ExperienceSlice；无结果返回 `result_status=no_match`。

### 验收条件

- 能按时间范围、关键词和相关认知线索查询，并能按 slice id 做开发调试定位。
- 摘要命中后能按时间范围读取对应的一组原始事件、Agent 回应和观察。
- 无结果时明确返回未知，不由模型补写一段似是而非的回忆。

第一版实现与测试：`memory/recall_tools.py`、`tests/test_recall_tools.py`。当前关键词匹配是稳定的文字匹配，不等同于语义相似；后续可增加全文索引或语义候选，但仍需保留 SQLite/ExperienceSlice 的来源边界。

## P2：Card_slot 混合了角色层与实例层

### 当前判断

- `Card_slot/persona_config.json` 属于角色层模板。
- `Card_slot/memory_db/` 下的 checkpoint、SQLite 和 Chroma 属于当前运行实例。
- 单角色、单实例阶段把它们作为一个整体替换包是合理的阶段性实现，不是当前阻塞问题。

### 后续触发条件

只有开始支持以下任一能力时再拆分：

- 同一 Persona 对应多个用户或实例。
- 更换角色模板但保留或迁移部分实例经历。
- 实例备份、导入、导出或版本管理。

拆分时必须避免新角色卡静默加载旧实例记忆，也不能因替换角色卡删除原实例数据。

## P2：其他已知技术债

### 一跳关系排序

`MemoryRelation.weight/confidence` 已参与一跳邻居排序，综合分为 `weight × confidence`；直接命中仍优先于关系邻居。后续仍可继续校准关系扩展预算。

### Embedding 离线启动

本地 BGE 模型现已通过 `EMBEDDING_LOCAL_FILES_ONLY=True` 设置 Hugging Face/Transformers 离线模式；已验证本机缓存权重可以正常导入。后续仍需在真正断网环境下做一次完整主程序启动测试。

## 不在本文件中重复的内容

- Step 6 自身的 resolver/judge/store、有序提交、mood 提交、SQLite/Chroma 同步和 `same` 情绪信号转换，继续以 `docs/Phase0_Memory_Architecture_Redesign.md` 为准。
- 人格演化继续以 `personality_seed -> trait_evidence -> PersonalitySnapshot -> 派生文本` 为准；它依赖 ExperienceSlice 持久化，不与情景记忆抢先施工。
- 已经修复并通过测试的问题不放入本清单，避免把历史故障误当成当前待办。

## 后续方向：高级模型咨询工具

当前主 Agent 使用 Flash 类模型。未来可以增加一个独立的高级模型咨询工具，
在主 Agent 遇到复杂架构、调试或长链路问题时提供分析建议。

它只返回建议，不直接执行文件、数据库、设备或网络副作用；实际操作仍由本地工具和现有提交链完成。
咨询调用需要独立的模型配置、API 密钥、超时和次数预算，失败时返回明确的不确定状态。
调用和返回结果应进入 `ExperienceSlice`，以便后续回溯“Agent 为什么采用某个方案”。

该方向目前只完成架构记录，尚未实现。施工前需要先确定触发条件、上下文范围、返回格式和用户可见性。

## 2026-08-30 后续施工计划（历史记录，当前进度见 CURRENT_HANDOFF）

### 阶段 2：跨进程提交账本恢复（P0）

当前进度：**阶段 2.1、2.2 以及真正的 pending appraisal 恢复第一版均已完成**。
账本会保存可重建的评价输入；启动时只恢复唯一、未过期、输入完整且处于
`appraisal_status=pending`、`commit_status=waiting` 的任务。

施工边界：

1. 已完成 appraisal、但 `commit_status=waiting` 的记录，只恢复到 `CommitWorker`。
2. `commit_status=committing` 的记录，启动时先核对结构化主库，再决定继续提交或标记 `abandoned`。
3. `appraisal_status=failed` 只有在能从 ExperienceSlice 和保存快照重建完整输入时才重评，否则人工处理并标记 `abandoned`。
4. pending appraisal 必须依赖账本中的可重建输入和原始 ExperienceSlice，不能凭 job id 猜测。
5. 缺少输入、原始经历或超过保留时间时，明确标记 `abandoned` 并保留原因。

### 阶段 3：SQLite/Chroma 一致性修复增强（P0）

1. 增加 Chroma 孤立索引检测，但不自动删除 Chroma 记录。
2. SQLite 仍是唯一事实来源，修复时重新读取 SQLite 当前实体。
3. 增加最大重试次数、失败时间和下一次重试时间，禁止无限重试。
4. 自动调度采用有限批次；修复失败不能阻塞主 Agent。

### 阶段 4：情景摘要可靠性（P1）

当前进度：**第一版和失败重试已完成**。

- 周/月摘要直接读取原始 ExperienceSlice；可选使用独立 LLM 做深层归纳。
- `experience_summary_attempts` 保存状态、错误、尝试次数和下次重试时间。
- `attempt_count` 每次实际尝试都会递增，成功和失败都计入。
- 达到 `SUMMARY_MAX_ATTEMPTS` 后进入 `exhausted`，不再自动重试。
- 外部 `tools/run_periodic_summaries.py` 会在所有周期处理完后，以非零退出码报告本次失败，
  便于 Windows 任务计划程序发现问题。
- 迟到切片形成新的来源批次，旧摘要和原始经历不会被覆盖或删除。

## 当前人格演化施工入口

`memory/personality_store.py` 已建立 `personality_seed` 和 `trait_evidence` 的 SQLite 存储地基。
当前已完成从多段已提交 ExperienceSlice 生成候选、代码验证并追加保存证据的第一版闭环；
它不修改角色卡，但已能保存 `PersonalitySnapshot` 版本并将最新快照的渲染文本以动态上下文
形式提供给主 Agent。

P1 纯代码基础设施现已完成：特质注册表、证据验证器、反思运行位置记录、快照计算器和边界测试
已加入。P2 的候选生成器和 `personality_reflection_runner.py` 已加入；运行器按新增经历和时间间隔
触发，成功才保存合格证据，失败不会推进分析水位。证据去重键包含实例标识，不同实例相互隔离。
人格结果已通过受控的 `current_personality` 动态上下文进入主 Agent；缺少快照时回退到角色卡中的静态人格描述。

`tools/run_personality_reflection.py` 提供一次性外部调度入口。它需要明确的 `thread_id`，
适合由系统定时任务调用；当前尚未做跨线程的实例级合并反思。反思成功后会追加保存
`personality_snapshots` 版本记录，但不会修改角色卡。

2026-09-02 使用现有 `project_test_001` 做了诊断性反思：共有 26 条经历、覆盖约 3 天；
在降低触发门槛后，LLM 能提出候选，但所选正面证据没有跨满 3 天，全部被 validator 拒绝。
当前实例的 `personality_seed` 也没有初始 `traits`，所以即使写入证据，快照也没有可计算的特质分数。
这不是降低校验标准的理由；后续需要补充角色层的出生特质基线，并积累真实跨日经历。
