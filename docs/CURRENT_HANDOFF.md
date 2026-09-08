# CURRENT_HANDOFF

更新时间：2026-09-08

用途：新会话开始时，先读取 `AGENTS.md`、`docs/Phase0_Memory_Architecture_Redesign.md` 和本文件，再继续工作。

## 项目北极星

这是一个通用类人 Agent 框架，不是单一角色实现。

任何方案先区分：

- 框架层：适用于所有 persona 的状态真值、来源边界、记忆身份、检索、更新和副作用机制。
- 角色层：人格、语气、知识边界、关系设定和表达策略，应进入角色卡。
- 实例层：当前线程、情绪状态、具体用户和当前认知库。

当前最重要的架构原则：不要把聊天文本当成 Agent 的世界入口；不要把一次回合后处理当成全部反思；不要为通过单个样例持续给 prompt 打补丁。

## Codex 的协作权限

- 默认给出审查、实现示例、修改位置和测试命令，由用户自己修改 `.py` 文件。
- 用户明确授权前，不直接修改 `.py` 文件。
- 文档和计划文件可在用户明确同意后修改。
- 当前工作必须遵循 `AGENTS.md` 中的架构自检和输出纪律。

## 已完成：Step 1-4

### Step 1：结构化认知主库

- `MemoryEntity` / `MemoryRelation` 已建立。
- SQLite 是结构化权威库；Chroma 只负责语义索引。
- Chroma 使用 `concept_id` 作为文档 id。
- `canonical_name`、`aliases` 与稳定 `concept_id` 已分离。

### Step 2：身份签名提取

- `memory/identity_extractor.py` 已完成并经过真实 LLM 极限测试。
- 能归一偏好、回避、别名、互动暗号、状态触发等输入。
- extractor 只处理一个原子认知；复合事实的拆分属于上游候选生成责任。

### Step 3：身份解析与裁决

- `memory/identity_resolver.py` 输出确定的 `same / ambiguous / new`。
- `memory/identity_judge.py` 只把 ambiguous 裁决为 `same / related / new`。
- LLM judge 不查库、不写库、不输出 self-reported confidence。
- 已做 same 改写、别名、实体-类别、弱相似、矛盾偏好、多候选等极限测试。

### Step 4：混合检索与一跳关系

- `memory/retrieval_engine.py` 支持精确名称/alias、结构化 filters、向量召回、按 `concept_id` 合并、一跳关系扩展。
- 向量候选区分 accepted 与 weak/rejected 调试结果。
- 已验证 query list、filters、alias 去重、一跳不递归、`include_related=False`、top_k 和直接命中优先于关系邻居。
- 当前待补强：关系 `weight/confidence` 还没有参与一跳扩展预算和排序，不能把所有邻居平等注入主 Agent。

## 当前真实状态：Step 6 进程内有序写入闭环已完成

代码已经完成感知理解、自动认知唤起、主 Agent、`ExperienceSlice`、后台 `ExperienceAppraisal`、规则计算和进程内有序提交闭环。主回复不等待 appraisal；评价完成后由独立 `CommitWorker` 按事件序号提交 mood、事实认知、认知情绪和一跳关系。

- 回应前：`core/context_builder.py` 使用 `PerceptionFrame`、精简后的 `PerceptionUnderstanding` 和新的 `memory.retrieval_engine`，把有限、非穷尽的结果作为自动认知唤起，返回结构化 `activated_memory_refs` 和 `memory_activation_state`。
- 回应后：`ExperienceSlice` 记录感知、理解、自动唤起引用、Agent 当时可见的能力快照、认知访问状态、AgentAction、观察、事件开始状态快照和结构化 `preceding_context`。现在会在提交后台评价前追加写入 `sql_db.db` 的 `experience_slices` 表，并支持按 id、时间范围和线程读取。`memory/experience_summary_store.py` 保存摘要，`memory/experience_summary_builder.py` 已提供不调用 LLM 的片段/每日/周/月稳定生成器；启动时周期检查、外部定时调度和人格证据存储地基已实现。
- 后台 appraisal 终态、提交状态、结果和错误会写入同一 SQLite 的 `commit_ledger` 表。它能在重启后告诉我们任务停在哪里；当前只记录和查询，不会自动重跑未完成任务。
- `process_perception_event` 冻结经历后立即向单 worker FIFO 队列提交 appraisal，只把 `pending/completed/failed` 任务真值和稳定 job id 返回给调用方，不等待后台 LLM。
- CLI 先显示主回复，再在后续事件边界输出已经完成的后台结果；后台异常保存在任务记录中，不伪装成 fallback，也不在线程中静默消失。
- `compute_appraisal_effects` 只计算 mood、已有认知情绪变化意图和新认知初始印象候选，不写运行状态或长期认知。已有认知更新不再携带会过时的绝对分数或标签。
- `PerceptionUnderstanding` 正式契约使用 `understanding_status`、`situated_understanding`、`memory_activation_cues`、`capability_constraints`、`uncertainties`。`understanding_status` 由代码产生；LLM 只提出情境理解、自动唤起线索、与本轮直接相关的能力约束和不确定性。
- 主 Agent 的 System Prompt 已加入 Persona、mood、局部认知情绪印记、当前证据优先级和后台记忆自我模型；每回合动态注入只提供当前事实和值。
- Appraisal 保留八步骨架，并已收紧高相关性证据、`persona_effect` 默认值、主观标签边界和原子修正规则。
- `core/commit_worker.py` 使用单独的非 daemon 线程和 `event_sequence` 水位有序提交；本次启动的水位可从 SQLite 中该 thread 的最大事件序号继续。评价失败和提交失败都显式进入终态并推进水位，重复 `job_id` 在进程生命周期内不会再次执行。
- `core/memory_commit.py` 按 `mood -> 事实认知 -> 认知情绪 -> 一跳关系` 提交。`create` 在提交时运行 resolver/judge；`same` 转为 reinforce 并保留候选情绪信号；`related` 创建独立实体后只建立一条经方向规范化的关系。
- `identity_judge` 已增加 `relation_direction`；`relation_service` 负责方向转换、对称端点排序、稳定 relation id、自环拒绝和方向不足时的保守降级。
- `reinforce` 使用 SQLite 原子 `mention_count + 1`，实体事实写入与认知情绪字段更新分离；结构化主库仍为 SQLite，实体更新同步到 Chroma。
- `main.py` 只在 commit 进入终态后释放对应 appraisal 引用；正常退出、`KeyboardInterrupt` 和输入流关闭都会先关闭/排空 appraisal，再关闭/排空 commit。
- 本次新增 ExperienceSlice 持久化测试 `4/4` 通过。完整回归当前有 2 个旧 mood 数值断言失败，原因是现行 `config.py` 参数与旧测试期望不一致，不是本次存储改动引起。
- 真实 LLM 开放验收已经覆盖 `create/update/reinforce`、已有认知情绪更新、`related` 独立实体和一跳关系、正负 mood 冲击、零冲击基线回归、SQLite/Chroma 跨重启召回以及退出排空。
- 开放验收中曾有 6 个提交任务按 `event_sequence=1~6` 全部进入 `committed`，没有 `commit_failed`。现在新的进程会从该 thread 已保存的最大序号之后继续编号；因此事件序号是长期经历顺序，CommitWorker 的内存水位仍只负责当前进程的提交顺序。

### Step 6 收口判定

```text
PerceptionEvent
-> 回应前自动认知唤起
-> 主 Agent 回复/行动
-> 冻结 ExperienceSlice
-> 后台 ExperienceAppraisal
-> 规则层 AppraisalEffects
-> CommitWorker 按 event_sequence 有序提交
-> mood / SQLite 认知实体与关系 / Chroma 索引同步
```

“Step 6 完成”严格表示：单进程生命周期内，评价结果能够在不阻塞主回复的前提下，按事件顺序、至多一次地进入当前权威状态和长期认知系统；失败状态可见，正常退出会排空后台任务。它不表示已经具备跨进程任务恢复或长期经历档案，这些能力不得被当前内存状态冒充。

当前仍未完成：

- ExperienceSlice 的第一版追加存储和跨进程读取已完成；提交账本、SQLite/Chroma 存储修复队列、摘要存储契约、无 LLM 的片段/每日/周/月摘要生成器、启动周期检查、外部定时调度和人格证据存储/验证/快照计算第一版已完成；人格反思候选生成、受控保存、快照版本保存和最新快照的主 Agent 动态注入已完成第一版。
- 周期摘要调度规则已接入启动流程：启动时检查昨天、上个完整自然周和上个完整自然月；来源切片未变化时跳过，发现迟到新切片时生成新版本。该检查只读 SQLite、不会调用 LLM；检查失败不会阻塞主 Agent 启动。以后仍可由外部定时任务复用同一入口。

已补齐：

- `memory/vector_repair_store.py` 创建 `vector_repair_queue`，保存实体、操作、失败原因、尝试次数、最后尝试时间和下一次重试时间。达到最大尝试次数后进入 `exhausted`，不再自动重试。`memory/store_manager.py` 在 SQLite 成功而 Chroma 失败时记录任务并继续抛出原始异常；`inspect_vector_consistency()` 会发现双向 id 差异，SQLite 缺失的索引自动入队，Chroma 孤立 id 只报告不删除；`repair_pending_vectors()` 重新读取 SQLite 当前实体后重建索引。`core/vector_repair_worker.py` 负责启动后和运行期间的低频、有界修复。

- `emotion.appraisal_rules.compute_committed_mood_change` 负责纯规则计算：只有 `mood_impact == 0` 时向 `MOOD_BASELINE` 回归一步；非零冲击不叠加回归，基线处保持不变。
- `emotion.manager.commit_mood_effect` 在同一个 SQLite 事务中写入最终 mood，避免先写入零变化再单独回归的中间状态。
- 评价失败不会调用 mood 提交；重复 `job_id` 仍由 CommitWorker 阻止重复执行。
- `emotion.models` 和 SQLite 记忆表改为应用启动时显式初始化，纯规则导入不再要求实例数据库目录存在；不会删除或覆盖已有实例数据。
- 基线回归已通过纯规则、临时 SQLite 事务、非零冲击、评价失败和提交委托测试，并已在真实对话中观察到 `mood_impact=0`、`55 -> 54`、`baseline_regression=-1` 的完整提交结果。

### 已完成：Step 0（ExperienceSlice 上下文契约）

- `core/experience.py` 的 `ExperienceSlice` 已加入冻结的 `preceding_context`。
- `main.process_perception_event` 会传入当前事件之前的有限上下文，不把 Persona 解释混入经历事实。
- Persona 快照作为 `process_perception_event` 返回结果中的独立 `persona_context` 提供给后续 appraisal。
- 已增加 `tests/test_experience_contract.py`，覆盖序列化、源数据修改隔离和空上下文回退；3 个测试已通过。

## 最终一致方案（2026-08-09 审核版）

以下内容覆盖本文件和 Phase 0 文档中较早的临时决定，尤其是“新认知永远使用中性情绪印记”的旧表述。

```text
前台：
PerceptionEvent
-> PerceptionFrame（一次事件状态快照）
-> 精简 PerceptionUnderstanding（情境、检索线索、能力约束、不确定性）
-> 新检索引擎
-> Agent 回复/工具行动/观察
-> ExperienceSlice（冻结的经历记录）
-> 立即返回并显示主回复

Step 5 后台：
ExperienceSlice
-> 单 worker FIFO ExperienceAppraisal
-> 规则层计算 mood 和情绪印记候选
-> 记录 completed / failed，不写运行状态和长期认知

Step 6：
-> 有序应用 mood、事实候选和情绪候选
-> resolver/judge/store 统一落库
```

最终边界：

1. 一次 LLM 调用输出三个逻辑区域：`experience_review`、`emotion_assessment`、`memory_assessment`。整体解析和每个区域都必须有保守回退；不允许因为某个区域非法而伪造结果。
2. `event_valence` 是整轮事件的统一效价。单个新候选只有在多候选且情绪方向可能不同时，才允许使用候选级 `candidate_valence`；不能让同一个事件被无意义地重复判断。
3. `event_relevance` 为 `none/low` 时，将 `event_valence` 归一为 `neutral`，mood 和已有认知情绪不变化，但事实记忆候选仍可保留。
4. Persona 通过 `persona_context` 影响语义判断；角色卡的 `emotion_profile.mood_reactivity` 只调节 mood 反应幅度，不创造新的情绪方向。该角色参数必须受框架范围校验。
5. 新认知初始 `emotion_score` 根据事件效价、Persona 契合度、被明确引用的直接相关认知情绪分和事件开始时的 mood 计算，限制在 `30.0~70.0`。`50.0/中性` 只作为评估失败或证据不足时的回退。
6. 已有认知的情绪更新只能由 `affected_memories` 触发；事实 `create/update/reinforce` 不能顺手改变情绪印记。
7. `label_update` 必须为 `null`，或是包含 `label`、`polarity`、`strength` 的结构化建议，不能是裸字符串。代码根据最终 `emotion_score` 校验极性与强度；一致才考虑采用 LLM 标签，冲突时保留分数并生成保守标签。`polarity` 只允许 `positive/neutral/negative`，标签强度只允许 `neutral/slight/moderate/strong`。
8. 候选被 resolver 判定为 `same` 时，不能丢弃其情绪信号；应将其事件效价转换为已有认知的 `change_direction`，并沿用经过校验的结构化 label 建议。
9. LLM 提交的 `concept_id` 和 `candidate_key` 都必须在清洗层做白名单校验。未知引用、重复 key 和悬空引用必须被丢弃或回退；resolver 在 `same` 裁决后生成的目标 id 走代码侧存在性检查，不受“本轮未检索到”限制。
10. `emotion_score` 使用 float，SQLite 是结构化主库，Chroma 只做同步索引。现有数据库迁移或重建必须单独确认，不能静默删除。
11. `energy` 在 Phase 0 明确冻结：不由 appraisal LLM 评估，只由离线恢复和未来真实行动机制推进。

本次审核新增边界：

12. `PerceptionUnderstanding` 的正式输出收缩为 `understanding_status`、`situated_understanding`、`memory_activation_cues`、`capability_constraints`、`uncertainties`。其中 `understanding_status` 由代码产生；原始事实已经由 `PerceptionEvent` 保存，不得再次生成冗长事实复述、注意力用途枚举或下一步回复建议。
13. 自动认知唤起不只服务“用户主动询问过去”。只要既有个人认知可能实质改变本轮理解、回应、行动、情绪评价或后续身份裁决，就应生成 `memory_activation_cues`；普通通用知识和仅靠当前上下文即可处理的事件仍返回空数组。
14. 主 Agent 的 System Prompt 保存稳定使用规则：当前 Phase 0 由 Persona 提供长期表达基线，mood 调节短时耐心、主动性和表达幅度，认知情绪印记只影响直接相关对象；三者按作用域并行生效，不做“有认知就忽略 mood”的瀑布回退。当前明确证据优先于可能过时的记忆。每回合动态注入只提供真实状态、被唤起认知和当前能力。
15. 长期认知的创建、修改和删除由后台框架评价、裁决和提交，主 Agent 不能直接操作这些副作用；主 Agent 可以调用只读 `recall_memory` 查询已有认知和经历。主 Agent 在回复时不知道候选最终会被写入、合并还是驳回，因此不逐条请示是否保存，也不能声称已经保存、更新或删除；用户明确的“不记住/仅临时”要求必须作为事件证据保留。
16. `event_relevance=high` 必须由已确认的重大影响支撑，并点明受到影响的关系、目标、边界/自主性、安全或关键任务。不能仅凭未确认的可能含义判 high；原因未知但重大影响本身已经确认时仍可判 high。
17. `persona_effect` 以 `neutral` 为默认。只有候选内容本身直接触碰 Persona 声明的目标、价值、边界或关系期待时，才能在有证据的情况下判为 `fitting/conflicting`；“记住它有助于服务”不构成 fitting。
18. `label_update.label` 的隐含主体必须是 Agent，只描述对该认知的主观态度，不复述 `concept_name` 或事实摘要；没有可靠、独特的主观态度时使用 `null`。代码继续校验极性、强度、空值和完全复制，不能假装能机械理解全部中文情绪语义；语义质量由真实 LLM 回归测试保障。
19. 用户修正只覆盖被明确否定或替换的原子命题，同一句旧陈述中未被撤回的其他事实保持不变。
20. Step 5 后台任务必须有真实状态（至少 pending/completed/failed）、事件或任务 id、独立异常边界和可追踪调试输出。Step 6 使用 `waiting/committing/committed/commit_failed` 和事件序号水位；尚未实现的持久账本或跨进程恢复不能由内存状态冒充。
21. `affect_only` 不再是事实候选 operation。已有认知的主观情绪更新始终通过独立的 `affected_memories` 路径进入 Step 6。

## 自动认知唤起与 Agent 自我模型（2026-08-14 统一）

此前文档把回应前路径笼统称为“长期认知检索”。从本次更新开始，必须区分两种不同用途：

```text
自动认知唤起：
当前感知 -> 少量线索 -> 有限、非穷尽的 MemoryEntity 浮现

主动回忆：
Agent 明确发起只读查询 -> 搜索当前整合认知和情景摘要
-> 必要时按来源 id 读取不可变 ExperienceSlice
```

框架不变量：

1. 自动唤起模拟当前事件触发的第一反应，不是全库证明性查询。`activated_memory_refs=[]` 只表示本轮没有认知自然浮现，绝不表示长期认知不存在。
2. `memory_activation_state.status` 使用 `normal/degraded/failed`：正常线索完成为 `normal`；感知理解失败而使用原始事件弱化线索为 `degraded`；底层唤起过程未完成为 `failed`。三个状态都不允许从空结果推出“从未发生”或“库中没有”。
3. `memory.retrieval_engine` 继续作为共享底层检索能力；当前 `context_builder` 使用它实现自动唤起，未来只读回忆工具也可复用，但两者的预算、查询范围和结果语义不能混为一谈。
4. Agent 可以把“本轮自然想起了什么”“这一刻没有自然想起什么”作为自身经历诚实表达；只有实际执行主动回忆后，才可以说“我查过”。即使主动查询无结果，也只能陈述本次查询范围内未找到可靠记录。
5. AgentAction 是“Agent 实际说了或做了什么”的一手证据。Agent 关于主观体验的自述可以作为第一人称证据；关于认知访问、工具调用、数据库写入、设备动作和其他副作用的自述，必须与 `capability_snapshot`、`memory_activation_state`、真实 observations 或成功提交结果一致。
6. 当前主 Agent 生成回复时看不到后台 appraisal/commit 的最终结果，因此可以说“收到、理解”，不能说“已经记住、保存、更新或删除”。它可以调用只读 `recall_memory` 查询已有认知和经历，但不能通过工具写入。这不是语气限制，而是运行时真值边界。
7. 具体互动事件与由该事件体现出的稳定偏好、关系或互动模式可以是 `related`，但不是同一个认知身份。跨 `memory_type` 的候选不得只因主体和主题相同被判为 `same`。

`ExperienceSlice` 必须保存当时的 `capability_snapshot`、`memory_activation_state` 和 `activated_memory_refs`。这样后台评价可以区分“Agent 当时自然想起了什么”“Agent 当时具有什么能力”以及“Agent 只是说了什么”，而不是从自然语言倒推系统状态。

## 当前 Card_slot 与时间记录状态

- 可替换的当前运行实例已经集中到 `Card_slot/`：角色模板位于 `Card_slot/persona_config.json`，当前实例的 checkpoint、SQLite 认知主库和 Chroma 索引位于 `Card_slot/memory_db/`。`config.py` 已统一指向这些路径。
- 该目录当前应理解为“角色模板 + 单个运行实例状态”的部署槽，不是纯角色卡。单角色、单实例阶段可以保持此布局；未来支持同一 Persona 的多用户或多实例时，必须再分离角色层模板与实例层数据库，不能让替换角色卡静默混用旧实例状态。
- `PerceptionEvent.occurred_at` 已经是事件发生时间的权威字段，checkpoint 也会把它保存在 `additional_kwargs["perception_event"]` 中。原始 `content` 只能保存感知内容，禁止为了让模型看见时间而把系统时间拼进用户原话。
- `core/model_io.py` 的 `render_perception_times_for_model()` 会在主模型真正调用前创建临时消息副本，把历史用户事件的 `occurred_at` 渲染为“系统记录时间”；`core/agent_factory.py` 的 `render_event_times` middleware 在每次模型请求前调用它。渲染不会写回 checkpoint 或 ExperienceSlice。
- `tests/test_model_io.py` 已覆盖当前事件、旧 checkpoint、普通消息、非用户事件和原消息不变。

## Checkpoint 与长期情景记忆边界

- LangChain 的通用 `SummarizationMiddleware` 达到阈值后会用“一条机器总结 + 最近消息”替换 Agent 的活动消息状态，随后由 LangGraph 写入新的 checkpoint；它不是只压缩某一次临时模型请求。
- 当前默认总结 Prompt 面向任务型 Agent，要求提取 session intent、summary、artifacts 和 next steps；它没有本项目的来源、事实、关系、情绪和不确定性边界。总结还会作为 `HumanMessage` 回到活动历史，存在把机器推断伪装成用户内容的风险。
- checkpoint 只负责短期运行恢复，不是长期经历权威库。旧 checkpoint 快照即使仍物理存在，也不能代替不可变事件档案。
- Step 6 已完成。当前短期上下文分成两部分：`core/context_builder.py` 每轮读取最近可用的日、周、月摘要作为长期背景，并保留最近几轮详细经历；`core/working_context.py` 在 checkpoint 消息达到阈值时调用项目专用总结 LLM，把更早消息整理成带来源标记的 `SystemMessage`，再与最近消息一起保存。两类摘要用途不同，互不替代：周期摘要直接来自 `ExperienceSlice`，Checkpoint 总结只服务当前运行连续性。每轮切片立即保存；所有派生摘要必须保留时间范围、来源 slice id、生成时间和算法/Prompt 版本，且不得覆盖原始经历。
- `MemoryEntity` 不保存完整的切片历史。它只提供 `created_at` 和 `last_mentioned_at` 作为时间线索；Agent 需要回忆具体过程时，按时间范围读取一组 ExperienceSlice，而不是把某个时间误当成唯一切片 id。
- 情景摘要用于快速定位，原始 ExperienceSlice 才是具体过程。摘要命中后，回忆工具应返回对应时间范围内按事件顺序排列的多条经历。
- 主 Agent 每轮可以看到最近可用的日、周、月摘要，作为较长时间背景，但不会自动接收按当前事件筛选出的相关摘要。当前事件、状态、近期详细经历、最近周期摘要和本轮自然浮现的少量长期认知共同组成基础上下文；当用户提到过去经历、给出时间线索或需要具体细节时，主 Agent 自己调用只读 `recall_memory`，先看摘要定位，再按时间范围或来源 id 查询原始 ExperienceSlice。普通当前事件不需要每轮调用工具。
- `recall_memory` 已接入单轮 10 次上限和覆盖范围返回。`memory.recall_tools` 使用同一轮共享的线程安全预算对象；即使 LangGraph 并行执行工具，第 11 次及以后也会返回 `call_limit_reached`，并发回归测试已通过。
- `recall_memory` 已返回当前实例可查询数据的覆盖范围：`data_coverage.memory_entities` 与 `data_coverage.experience_slices` 分别提供记录数、最早时间和最晚时间。Agent 只能据此说明“当前可查询范围内最早找到的记录”，不能据此声称整个历史不存在。
- 旧经历在长期记忆功能接入前没有持久化的部分不做补录；它们属于历史数据缺口，不作为本阶段施工目标。
- 通用 `SummarizationMiddleware` 已从主 Agent middleware 链路移除。`WorkingContextMiddleware` 现在使用独立的低温度总结 LLM，把旧消息整理成项目定义的短期工作上下文，并以 `SystemMessage` 和 `source=project_working_context` 标记保存；总结失败时回退为来源清晰的原始记录，不阻塞主 Agent。
- `WorkingContextMiddleware` 会先清理旧 checkpoint 中的孤立 `ToolMessage`，并按完整的 `AIMessage(tool_calls)` + 对应工具结果组保留最近窗口，不能把工具调用拆开。工具返回很长时只允许在工作上下文中省略，不能从 `ExperienceSlice` 删除。
- 总结器参数集中在 `config.py`：`WORKING_CONTEXT_LLM_TIMEOUT_SECONDS`、`WORKING_CONTEXT_LLM_TEMPERATURE`、`WORKING_CONTEXT_LLM_MAX_TOKENS` 和 `WORKING_CONTEXT_MAX_SUMMARY_CHARS`。这些参数只影响短期工作上下文，不影响主 Agent 回复或后台评价。
- `tools/test_working_context_llm.py` 提供三种手动验收：`extreme` 检查来源混杂和越权推断，`checkpoint` 读取真实线程历史，`trigger` 构造 41 条消息走完整 `before_model()` 阈值流程。三种模式均只读数据；`trigger` 已确认会生成项目派生 `SystemMessage` 并保留最近 20 条消息。
- 已确认的风险、施工顺序和验收条件统一记录在 `docs/POST_STEP6_KNOWN_ISSUES.md`。

## Step 6 之后的人格演化统一方案

人格演化属于 Phase 0 之后的慢速反思层。当前已完成 P1 纯代码基础设施：
`memory/personality_store.py` 保存不可变的 `personality_seed`、追加式 `trait_evidence`
和反思运行位置；`memory/personality_traits.py` 定义第一批连续特质，
`memory/personality_validator.py` 校验证据来源、跨度、反证和变化上限，
`memory/personality_calculator.py` 根据 seed 与 accepted 证据计算快照。
这些模块尚未把任何结果接入主 Agent，也不会自动改变人格。主程序首次启动时会调用
`ensure_personality_seed_from_persona()`，只把角色卡复制为一次性的出生人格；角色卡只是创建 Agent 实例时的模板；
只有首次初始化时保存的 `personality_seed` 才是该实例不可被静默覆盖的出生人格。

权威数据流固定为：

```text
persona_config.json（角色模板）
-> personality_seed（实例的不可变出生快照）

ExperienceSlice（不可变原始经历）
-> trait_evidence（追加式结构化人格证据）
-> PersonalitySnapshot（seed + 有效证据计算出的当前人格版本）
-> TRAIT_INSIGHT / _statistic_override（可检索或可注入的文本渲染）
```

边界：

1. ExperienceSlice 已有第一版追加式持久化，并保留 `slice_id/event_id/action_id/observation_id` 等来源引用。
2. `trait_evidence` 与其他结构共用同一个 SQLite；当前已完成追加、去重和来源校验，保存反证、作用域、行为来源和算法版本。
3. `TRAIT_INSIGHT` 未来可以作为新的记忆类型进入普通检索，但它只是 `PersonalitySnapshot` 的自然语言派生摘要。人格反思不能把旧 `TRAIT_INSIGHT` 当成新的行为证据，防止自我强化循环。
4. `PersonalitySnapshot` 是当前人格的结构化权威状态；`_statistic_override` 只能是它的文本渲染，不能由 LLM 随意覆盖。
5. 人格 LLM 只输出特质、方向、强度、作用域、行为来源和证据引用等语义；最终数字、变化上限、去重、版本和回滚由代码负责。
6. 人格证据来自多段已经提交的独立经历。当前纯代码验证器要求至少 3 条经历并跨越至少 3 天；Agent 自报、单轮输出、任务要求或 Persona 风格执行、旧反思结论都不能单独证明人格变化。
7. 框架层定义连续特质 registry 的协议、行为锚、未知状态和聚合规则；persona 层提供有依据的出生初值并选择已定义扩展；实例层保存证据与当前版本。Big Five、荣格八维或自定义维度现在不拍板，MBTI 只可作派生展示或诊断。
8. 当前人格接入后，每轮只注入 `current_personality`，不能再把出生人格作为第二份行为权重重复注入。身份、知识、安全和副作用边界不随人格演化改写。

P1 当前边界：

- 第一批特质为 `openness`、`conscientiousness`、`extraversion`，仅作为可扩展注册表，不代表永久锁定 Big Five。
- 证据验证默认要求至少 3 条经历并跨越至少 3 天；反面证据会降低置信度，过强时拒绝。
- 快照变化按 `strength × confidence × trait_delta_scale` 计算，单次反思对单个特质不超过 5 分。
- `ExperienceSlice` 目前只有 `thread_id`，不会把它错误当成 `persona_instance_id`。人格反思运行器由调用方显式传入 `persona_instance_id + thread_id`；当前事件序号只在单线程内递增，因此不接受无线程边界的跨线程反思。
- P1 不调用 LLM、不接入主 Agent、不增加 `ExperienceSlice.autonomy_level`。

P2 当前进度：

- `memory/personality_reflection_llm.py` 已加入候选生成器。它只读取原始 ExperienceSlice，
  要求 LLM 返回候选证据数组；不写 `trait_evidence`、不计算最终分数、不修改主 Agent。
- 反思候选仍必须经过 `personality_validator.py`；非法 JSON 或越权字段不会直接进入人格状态。
- `memory/personality_reflection_runner.py` 已加入受控运行器：默认至少积累 30 条新经历、且距上次反思至少 7 天（首次要求这批经历自身跨 7 天）才触发；运行记录按 `persona_instance_id + thread_id` 区分。
- 运行器先登记 `running`，再调用候选生成器和验证器；成功才追加 `trait_evidence` 并推进 `last_analyzed_sequence`，失败只登记 `failed`，不会推进水位或修改已有证据。
- 证据使用包含 `persona_instance_id` 的稳定去重键，重复运行不会重复写入，不同实例不会互相吞掉证据；当前只提供显式调用入口，不放进每轮前台请求。
- `personality_reflection_llm.py` 会把少数明确同义枚举（如 `positive/negative`、`autonomous`）转换为正式值，并在 Prompt 中明确要求正面证据跨至少 3 天；未知写法仍由 validator 拒绝。`tools/run_personality_reflection.py --reprocess` 可用于人工重新分析已经处理过的经历。
- P2 的独立外部入口已加入；`PersonalitySnapshot` 现已可按实例追加保存为版本记录，内容由 seed + accepted evidence 重算得到；最新快照会以受控的 `current_personality` 文本进入主 Agent 动态上下文。
- 已新增 `tools/run_personality_reflection.py` 作为一次性外部调度入口；它读取当前角色卡和实例 seed，按指定 `thread_id` 调用运行器，适合交给 Windows 任务计划程序，不进入前台聊天循环。
- 人格演化实现先收口；真实 LLM 长期效果、出生特质基线和多次跨日积累等未验证内容记录在 `docs/PERSONALITY_EVOLUTION_UNVERIFIED.md`。

## 后续可选扩展：Somatic Simulation

`Somatic Simulation`（身体状态与内感受模拟）是未来的通用框架扩展，成人身体模型只是其中一个可选扩展，不属于当前 Persona 的硬编码行为。

它的目标不是让主 Agent 根据括号文本自行想象身体反应，而是把可能影响 Agent 身体的模拟事件转换成可追踪的身体状态、内感受和本能行动。按需触发的建议流程如下：

```text
PerceptionEvent
-> 通用 module_requests 路由提示
-> 模拟事件裁决（occurred / rejected / uncertain）
-> Somatic Appraisal 提出状态变化方向
-> 代码校验并提交 SomaticState
-> 生成内感受 PerceptionEvent 与可见本能 AgentAction
-> 主 Agent 回复
-> ExperienceSlice 保存完整来源链
```

边界：

1. 用户文本或括号动作只是模拟动作提议，不等于身体接触已经发生。`module_requests` 只能请求模块处理，不能直接改变身体状态。
2. 模拟事件裁决必须保留对象、部位、强度、持续时间和不确定性；只有确认发生的事件才能进入身体状态更新。
3. 身体评价 LLM 只提出方向、幅度和解释；最终状态由代码校验、钳制和提交，不能由 LLM 直接覆盖绝对值。
4. 内感受属于 Agent 的内部感知，可作为 `somatic/interoception` 事件注入主 Agent；可见本能反应属于 `AgentAction`，必须进入 `ExperienceSlice`，不能只打印后丢失。
5. mood 可以作为身体评价的背景输入，但身体反应不能直接证明同意、好感或关系升级；身体状态与情绪、关系和安全边界必须保持分离。
6. 通用框架只定义身体事件、状态真值和模块协议；成人专用的生理字段、身体模型和表达方式放入可选扩展或角色配置。
7. 该扩展必须在经历证据持久化契约稳定后再接入，确保身体状态变化、内感受、可见反应和主 Agent 行动都能回溯到原始 `ExperienceSlice`。

当前决定：Somatic Simulation 仍是后续通用扩展，不插入 ExperienceSlice 持久化、情景记忆和人格演化的当前主线。它以后产生的身体状态、内感受和可见反应，必须和普通用户事件一样进入 ExperienceSlice，不能另建一套无法回溯的历史。

### Somatic Simulation 详细架构流程（Claude 审核版 2026-08-30）

```
用户输入："（轻抚你的大腿）"
    ↓
PerceptionEvent(source="user", content="（轻抚你的大腿）")
    ↓
PerceptionUnderstanding → 识别出 module_requests: ["somatic_simulation"]
    ↓
【Somatic Simulation Module】
    ├─ 1. 模拟事件裁决（确认接触确实发生）
    │   结构化输出：
    │   {
    │     "event_confirmed": true,
    │     "contact_object": "user",
    │     "contact_area": "thigh",
    │     "intensity": "gentle",
    │     "duration": "brief",
    │     "uncertainty": "low"
    │   }
    ↓
    ├─ 2. 身体评价 LLM（只输出方向/幅度，类似 mood 处理）
    │   输入：事件裁决结果 + 当前身体状态快照 + mood 背景
    │   输出：
    │   {
    │     "arousal_change": "increase",      # 只给方向，不给绝对值
    │     "arousal_magnitude": "slight",     # 幅度：slight/moderate/significant/intense
    │     "sensitivity_change": "increase",
    │     "sensitivity_magnitude": "slight",
    │     "visible_reaction_type": "muscle_twitch",
    │     "interoception_description": "大腿处传来温热触感，身体有些紧张",
    │     "reason": "接触非敏感区域，反应轻微"
    │   }
    ↓
    ├─ 3. 代码计算最终状态（类似 mood 边界衰减）
    │   使用固定 delta 映射表（框架层）：
    │   BODY_STATE_DELTA = {
    │     "slight": 0.05,
    │     "moderate": 0.10,
    │     "significant": 0.15,
    │     "intense": 0.20,
    │   }
    │
    │   arousal: 0.50 → clamp(0.50 + 0.05, 0.0, 1.0) = 0.55
    │   sensitivity: 0.60 → clamp(0.60 + 0.05, 0.0, 1.0) = 0.65
    ↓
    ├─ 4. 生成可见本能反应（AgentAction，必须进入 ExperienceSlice）
    │   create_agent_action(
    │     action_type="somatic_response",
    │     content="身体微微颤了一下",
    │     metadata={
    │       "triggered_by": "somatic_module",
    │       "body_state_snapshot": {arousal: 0.55, sensitivity: 0.65, ...}
    │     }
    │   )
    ↓
    └─ 5. 生成内感受事件（PerceptionEvent，在本轮主 Agent 回复前注入）
        PerceptionEvent(
          source="somatic_module",
          modality="interoception",
          content="大腿处传来温热触感，身体有些紧张",
          metadata={
            "arousal": 0.55,
            "sensitivity": 0.65,
            "triggered_by_event": "evt_xxx"
          }
        )
    ↓
主 Agent 回应（感知到内感受后回应）
"（略微紧张地看着主人）嗯..."
    ↓
ExperienceSlice 完整记录：
  - perception_event: 用户轻抚动作
  - response_or_actions: [可见本能反应, 主 Agent 回应]
  - observations: [内感受事件]
  - 所有输出可回溯到原始接触事件
```

关键设计决策：

1. **LLM 只输出方向和幅度，代码负责最终数值**：避免 LLM 输出不连续、边界越界或与当前状态冲突。类似 `emotion_assessment` 的 `change_direction` + `EMOTION_AFFECT_DIRECTION_DELTA` 模式。

2. **内感受作为独立 PerceptionEvent 注入下一轮**：不直接混入当前轮的动态提示词，而是作为新的感知事件让主 Agent 在下一轮自然回应。这保持了"所有感知走统一入口"的原则。

3. **可见本能反应必须进入 ExperienceSlice**：不能只打印后丢失。它属于 `AgentAction`，与主 Agent 的 `visible_response` 并列，但由身体模块生成。

4. **身体状态不直接证明同意或关系**：arousal=0.8 不等于"Agent 同意了"或"Agent 喜欢这个人"。身体反应与情绪、认知、安全边界是分离的四个系统。

5. **模拟事件裁决是必要前置步骤**：用户说"（轻抚）"不等于接触已发生。裁决层必须判断：对象是谁、部位在哪、强度多大、是否确认发生。不确定的事件不进入身体状态更新。

6. **通用框架与成人扩展分离**：框架层只定义 `body_state` 的协议（key-value 存储、delta 更新规则、LLM 评价接口）。具体字段（genital_arousal、moisture 等）和身体模型放入可选的成人扩展配置。

实现优先级：该模块在 ExperienceSlice 持久化、情景摘要和人格证据**全部稳定后**再接入，确保身体状态变化能像其他经历一样被回溯、查询和用于长期反思。

## 已完成的两项基础修正

- 一跳关系邻居现在使用 `weight × confidence` 作为关系分数；直接命中始终优先，关系分数只用于邻居之间排序。
- `memory/vector_store.py` 在 `EMBEDDING_LOCAL_FILES_ONLY=True` 时设置 Hugging Face/Transformers 离线模式，并要求使用本机缓存权重。断网启动不会再主动访问 Hub；本地权重缺失时会直接报错。

## 后续方向：高级模型咨询工具

主 Agent 当前使用 Flash 类模型，遇到复杂架构、调试或长链路问题时，未来可以调用一个独立的
“高级模型咨询”工具，把难题交给更强的模型分析。这是模型能力升级通道，不是第二个主 Agent。

基本流程：

```text
主 Agent 判断问题过于复杂
-> 调用高级模型咨询工具
-> 咨询模型读取当前问题、必要上下文和真实能力快照
-> 返回分析、方案和不确定性
-> 主 Agent 根据结果组织最终回复或调用本地工具
```

边界：

- 咨询模型只提供分析和建议，不直接写文件、改数据库、调用设备或执行其他副作用。
- 实际修改仍必须经过本地文件工具、记忆提交链或其他明确工具，并返回真实成功结果。
- 咨询请求应尽量只携带当前问题和必要来源，不能默认发送全部历史或隐藏推理。
- 咨询失败、超时或返回不确定时，主 Agent 必须保留不确定性，不能把建议当成已经完成的事实。
- 咨询调用和结果应像其他工具一样记录为 `AgentAction` 与 `observation`，进入对应的 `ExperienceSlice`。
- 咨询模型使用独立的模型配置、API 密钥、超时和调用预算，不能影响主 Agent、感知理解或后台评价。

当前只记录方案，不接入实现。正式施工前需要确定：复杂问题触发条件、请求上下文范围、返回格式、
调用预算、失败回退，以及是否向用户显示“已请求高级模型咨询”。

## 下一步施工顺序

1. 在真实长对话中验收项目专用 Checkpoint 总结的延迟、稳定性和工具消息协议。
2. 继续使用当前已持久化的 ExperienceSlice 建立摘要和回忆能力；长期记忆功能接入前缺少的旧经历不做补录。
3. 经历证据稳定后，再依次建设 `trait_evidence`、`PersonalitySnapshot` 和只作派生展示的 `TRAIT_INSIGHT`；在此之前不实现人格更新。
4. 延迟分布、mood 20/50/80 表达差异、新认知初始分边界堆积、单次互动候选过多和关系邻居排序继续作为观察项，不阻塞当前存储主线。
5. 经历证据持久化稳定后，再设计按需 `Somatic Simulation`：先完成通用模块请求和模拟事件裁决，再建设身体状态提交、内感受事件和可见本能行动；成人身体模型作为后续可选扩展。

候选在 Phase 0 期间只在进程内从 Step 5 传给 Step 6；进程崩溃时丢弃，不建立持久化任务队列。

## 继续时的提醒

当前最大风险不是缺少更多 prompt，而是把新认知身份、情绪印记、Persona 解释和旧 Chroma-only 副作用再次混在一起。每完成一个小闭环，都要先看结构化输出、数据库变化和状态时序，再进入下一步。

## 2026-08-30 后续施工计划（覆盖较早的临时顺序）

### 阶段 2：跨进程提交账本恢复（P0）

当前 `commit_ledger` 只在 appraisal 进入终态后写入记录，因此不能把账本中不存在的
`appraisal_status=pending` 任务假装恢复。第一步只恢复已有可靠结果：

当前进度：**2.1 和 2.2 已完成，pending appraisal 的持久输入和启动恢复第一版也已完成**。
`commit_store` 保存 `persona_context`、`mood_reactivity` 等可重建输入；启动流程会恢复
唯一、未过期、输入完整的 pending appraisal，以及评价已完成但仍等待提交的任务。旧账本
没有这些输入时不会猜测重跑，会保留人工处理路径。

审核补充：序号缺口判断会把已进入终态的中间序号视为已知，不会误阻塞后续等待任务；
无法从 ExperienceSlice 重建的等待任务会在启动时保留原因并标记为 `abandoned`，不再反复尝试。

- `appraisal_status=completed` 且 `commit_status=waiting`：重新交给 `CommitWorker`，提交函数必须保持幂等。
- `commit_status=committing`：启动时先根据结构化主库检查是否已经产生写入，再决定继续提交或标记为 `abandoned`。
- `appraisal_status=failed`：只有能从 `ExperienceSlice` 和保存的角色/状态快照重建完整评价输入时才允许重评；否则标记 `abandoned` 并保留原因。

超过保留时间或缺少原始经历/输入的任务只做明确的 `abandoned` 归档，不静默删除。

### 阶段 3：SQLite/Chroma 一致性修复增强（P0）

- 当前进度：**第一版已完成并通过定向测试**。
- `inspect_vector_consistency()` 会比较两边的 `concept_id`；SQLite 中有而 Chroma 缺失的实体进入有界 `upsert` 修复队列。
- Chroma 中有而 SQLite 缺失的记录只标记为孤立索引并报告，不自动删除事实或索引。
- `vector_repair_queue` 保存最后尝试时间和下一次重试时间；达到 `VECTOR_REPAIR_MAX_ATTEMPTS` 后进入 `exhausted`，禁止无限重试。
- `VectorRepairWorker` 在主程序中以独立后台线程启动，先检查一次，之后按 `VECTOR_REPAIR_INTERVAL_SECONDS` 和 `VECTOR_REPAIR_BATCH_SIZE` 周期运行；异常只记录，不阻塞主 Agent。
- 仍需在真实运行中观察修复线程日志和网络故障后的恢复效果；本阶段没有自动删除孤立 Chroma 索引。

### 阶段 4：情景摘要可靠性（P1）

当前进度：**第一版已完成并通过定向测试**。

周/月摘要直接读取原始 `ExperienceSlice`，不再重复总结旧摘要。`experience_summary_attempts`
保存每个周期和来源切片批次的生成状态、失败原因、尝试次数、最后尝试时间和下一次重试时间；
达到 `SUMMARY_MAX_ATTEMPTS` 后进入 `exhausted`，不会无限调用。启动检查仍然不调用 LLM，
生成失败不会阻塞主 Agent。迟到切片会形成新的来源批次和新的尝试记录，旧摘要与原始经历保留。

摘要失败会按有限次数重试；同一批来源切片达到上限后进入 `exhausted`。
库内调用默认保持“一个周期失败不影响其他周期”。外部定时器打开失败报告开关，
在所有周期处理完后以非零退出码通知任务计划程序，避免失败被静默吞掉。

### 外部周期调度与深层摘要

- `tools/run_periodic_summaries.py` 是一次性入口，适合交给 Windows 任务计划程序或 cron 每天运行。
- 该入口默认使用角色卡配置的独立 LLM 生成 weekly/monthly 深层摘要；daily 仍使用稳定的代码摘要。
- `--no-llm` 可在离线或诊断时只运行代码摘要。
- 外部入口会在本次检查出现摘要生成失败时返回非零退出码；失败记录仍保存在
  `experience_summary_attempts`，不会影响其他周期继续处理。
- LLM 摘要只读取原始 `ExperienceSlice`，输出摘要文本和不确定性；来源切片、时间范围和摘要 id 由代码掌握，不能写回认知或人格状态。
- `attempt_count` 统计每一次实际生成尝试，包括成功和失败；它不是只记录失败次数。

## 文件工具（第一版已完成）

文件工具已作为框架层能力接入主 Agent，详细说明见 `docs/FILE_TOOLS.md`。
实例权限配置位于 `Card_slot/file_access_policy.json`，读取和写入白名单分离；
路径解析、符号链接、敏感文件、系统目录和大小限制由代码执行。当前工具包括
`read_file`、`list_directory`、`write_file`、`apply_patch`。白名单内写入使用
原子替换。白名单外写入由 CLI 中间层询问用户；拒绝返回
`confirmation_required`，确认后只执行当前一次“操作 + 路径”，令牌在工具进入
执行阶段后失效。系统目录和敏感文件不能通过授权绕过。

工具调用继续进入现有 `AgentAction -> PerceptionEvent -> ExperienceSlice` 链路，
文件结果使用通用 `tool_result` 类型，回忆工具保留 `memory_recall_result`。
文件内容不会自动进入长期认知。定向测试位于 `tests/test_file_tools.py`；
二进制文件、读取大小限制和目录截断已有测试；符号链接测试在当前环境因无法
创建链接而跳过，需要在真实 Windows 权限环境再验收。主循环真实对话仍需手动观察。
长期真实环境还要继续留意权限配置变化、授权令牌的一次性和重启行为、并发调用、
外部替换文件、磁盘空间不足，以及 Agent 是否始终依据真实 `status=success` 判断
文件副作用。一次测试通过不代表这些长期运行场景已经完成验收。

## 模拟身体模块（P1-P8 已完成，P9 自动化验收完成）

说明：本节是当前生效方案；前文旧版流程中出现“注入下一轮”的描述已废弃，实际实现是在本轮主 Agent 回复前注入内感受。

已完成通用身体事件协议、实例级 SQLite 状态、`module_requests` 路由、独立身体评价
LLM、`somatic_action` 工具、主 Agent 同轮内感受注入，以及 ExperienceSlice 完整记录。
身体状态按 `persona_instance_id + thread_id` 隔离，当前只有 `arousal` 和 `sensitivity`
两个 0 到 1 维度。每次状态变更使用 `operation_id + revision` 幂等提交，重复调用不会
重复更新，旧版本不能覆盖新状态。

用户动作流程为：感知理解提出候选 -> 代码裁决
`occurred/rejected/uncertain` -> 只有 `occurred` 才评价和更新 -> 本轮注入内感受。
Agent 主动动作必须调用 `somatic_action`；工具返回成功后才算角色世界中的动作发生。
外在反应使用 `AgentAction`，内感受使用 `PerceptionEvent(source="somatic_module",
modality="interoception")`，两者和状态前后值一起进入原始 ExperienceSlice。

身体状态数值只供身体模块内部计算、恢复和审计；不会进入感知理解提示词、主 Agent
动态上下文或模型可见的工具返回。Agent 只能看到内感受、可见反应和执行状态。用户接触
已经由感知路径处理时，`somatic_action` 不能再次用来描述被动反应，避免同一动作重复更新。

2026-09-04 已收束身体相关提示词：身体评价只处理身体层的方向、幅度和反馈；主 Agent
只依据工具状态决定是否把主动动作写入回复；工具说明只描述通用输入输出和当前执行通道。
重复动作、关系含义等不再依赖一串历史案例禁令，统一由事件边界、真实工具结果和代码状态负责。

`narrative_embodied` 只表示角色世界通道，不自动控制现实设备。身体反应不等于同意、
好感、关系升级或人格变化；成人专用身体字段暂不实现。定向测试已覆盖协议、存储、
评价、服务、工具和主流程集成。身体数值只保留在模块内部、原始经历和审计数据中，
主 Agent、感知理解和回忆工具只能看到内感受、可见反应和执行状态。用户接触已经由感知
路径处理时，Agent 不应再次调用 `somatic_action` 描述被动反应。

长期观察项：真实 LLM 输出稳定性、连续互动后的状态是否长期偏高、主动动作与被动反应
是否被混用、单轮 3 次调用上限、Windows 重启后的状态连续性、回忆旧经历时的数值脱敏、
以及长时间运行中的幂等和并发安全。当前没有自然衰减机制，未观察前不把长期稳定性当成
已完成能力。

## 视觉感知模块（V1 路径型工具闭环）

已新增 `core/vision_input.py`：终端可用 `/image 图片路径` 创建图片感知事件；框架会校验
普通文件、支持的图片格式、大小并记录 SHA-256、类型和路径元数据。`core/model_io.py` 在
能力快照明确 `model.capabilities.vision=native` 时，只在本次模型请求中临时加入图片块，
不会把 base64 写入 checkpoint。当前能力快照已记录 `vision.input_available/native/tool_available`。

设计和边界见 `docs/VISION_MODULE.md`。已新增 `VisionService`、路径型 `vision_describe_image` 工具及独立
LLM 观察适配器；当前角色卡声明 `vision="native"`，用户提交的图片会直接交给主模型，Agent 通过
`list_directory` 找到的图片则由视觉工具临时回传主模型；明确配置为 `vision="tool"` 或未声明
视觉能力时，工具调用独立视觉模型。
视觉模型的地址、名称和密钥环境变量统一由 `config.py` 的 `VISION_LLM_CONFIG` 提供；主循环会绑定本轮视觉上下文。视觉观察必须带来源和不确定性，
不能直接升级为身份、情绪、关系、人格或长期记忆。
`list_directory` 现在为每个条目返回规范化绝对 `path`，可直接传给视觉工具；读取白名单仍由
`file_access_policy` 强制执行。native 图片和工具图片都只在模型请求副本中展开为 Base64，工具结果、
checkpoint 与 ExperienceSlice 不保存图片二进制。
Agent 主动观察的目录必须配置在 `Card_slot/file_access_policy.json` 的 `read_roots`；用户直接
通过 `/image` 提交的附件仍按当前图片事件校验流程处理。

主模型、感知理解或视觉工具遇到 502、超时、格式错误等失败时，只结束当前事件并提示用户，
清理本轮上下文后继续运行聊天进程；不能因单轮失败退出整个程序，也不能把失败伪装成已看懂图片。
摄像头、视频、多图比较、人脸绑定和复杂界面暂不实现。

2026-09-06：部分中转站会把模型分析说明混入 PerceptionUnderstanding 正文，导致严格
`json.loads` 失败。解析器现先接受完整 JSON，再从夹杂说明的正文提取协议对象；若仍无法
确定则继续使用原有保守降级，不把模型说明当作已确认理解。

## 通用后台任务基础设施（2026-09-07，第一版）

已新增 `background_tasks` / `background_task_events` 持久账本、进程内 Worker 和事件总线。
它服务 Agent 主动发起的高级咨询与代码审核，不替代 appraisal/commit 核心流水线。主循环
启动时恢复失联任务，回合边界消费完成/失败通知；详细结果通过状态工具读取。

已注册工具：`consult_advanced_model`（同步只读咨询）、`start_code_review`（异步只读审核）、
`get_background_task_status`、`list_background_tasks`、`cancel_background_task`。审核器只能读取
任务范围与全局白名单的交集，不能写文件、执行命令或修改数据库。实现和当前限制见
`docs/BACKGROUND_TASKS.md`。

尚未完成独立操作系统进程、实时打断阻塞式 CLI 输入、后台结果自动转为新的
`PerceptionEvent/ExperienceSlice`；这些属于后续扩展。真实长时间运行需重点观察失败日志、
重启恢复、重试次数和审核报告质量。

2026-09-08 已核实并修正核心审查报告中的可靠性风险：后台审核增加总时限和轮次截止检查；
任务调度提交失败会持久标记 `dispatch_failed`；Worker 轮询异常会记录并退避；
CommitWorker 将业务提交成功与账本同步失败分离，后者保留在 `ledger_error` 中；视觉服务和
身体评价增加服务入口级超时。同步调用无法被 Python 线程强制杀死，因此仍需依赖底层客户端
请求超时，长期运行继续观察阻塞调用和账本补偿。

## 图片生成模块（第一阶段地基）

已新增 ComfyUI HTTP 客户端、图片生成服务和 `generate_visual` 工具，并接入主循环。
工具只接收通用 prompt、尺寸、seed 和可选的 upscale；工作流模板、字段映射、ComfyUI 地址及输出目录
属于 `config.py` 运行配置，不暴露给 Agent。没有工作流模板或映射时返回
`configuration_missing`，不会伪造生成成功。生成成功还必须通过 ComfyUI history、输出文件
存在性、输出目录边界和 SHA-256 校验。

当前已配置两份 ComfyUI API 工作流：`workflow_api.json` 为普通输出，
`workflow_api_upscale.json` 为放大输出。`generate_visual` 默认使用普通工作流，只有
`upscale=true` 才切换到放大工作流；两份工作流使用各自独立的字段映射，避免运行时删除节点或
把一套工作流的节点编号错误套到另一套。输出通过 ComfyUI `/view` 读取后复制到
`Card_slot/generated_images/`，该目录已加入 Git 忽略。
2026-09-08 已完成一次真实生成：ComfyUI `0.18.2` 返回 `prompt_id`，项目成功取得 PNG 并
完成 SHA-256 校验；同时修复了可选 seed 未提供时错误写入 `null` 的问题。生成与再次视觉观察
仍是两个独立事件，图片二进制不进入 checkpoint 或普通经历文本。ComfyUI 未启动、任务失败、
超时和输出替换仍需继续做故障验收。
