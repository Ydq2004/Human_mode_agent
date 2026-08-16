# CURRENT_HANDOFF

更新时间：2026-08-14

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
- 回应后：`ExperienceSlice` 记录感知、理解、自动唤起引用、Agent 当时可见的能力快照、认知访问状态、AgentAction、观察、事件开始状态快照和结构化 `preceding_context`。当前它只是冻结的进程内对象，尚未写入 SQLite；进程退出后不能作为长期反思证据读取。
- `process_perception_event` 冻结经历后立即向单 worker FIFO 队列提交 appraisal，只把 `pending/completed/failed` 任务真值和稳定 job id 返回给调用方，不等待后台 LLM。
- CLI 先显示主回复，再在后续事件边界输出已经完成的后台结果；后台异常保存在任务记录中，不伪装成 fallback，也不在线程中静默消失。
- `compute_appraisal_effects` 只计算 mood、已有认知情绪变化意图和新认知初始印象候选，不写运行状态或长期认知。已有认知更新不再携带会过时的绝对分数或标签。
- `PerceptionUnderstanding` 正式契约使用 `understanding_status`、`situated_understanding`、`memory_activation_cues`、`capability_constraints`、`uncertainties`。`understanding_status` 由代码产生；LLM 只提出情境理解、自动唤起线索、与本轮直接相关的能力约束和不确定性。
- 主 Agent 的 System Prompt 已加入 Persona、mood、局部认知情绪印记、当前证据优先级和后台记忆自我模型；每回合动态注入只提供当前事实和值。
- Appraisal 保留八步骨架，并已收紧高相关性证据、`persona_effect` 默认值、主观标签边界和原子修正规则。
- `core/commit_worker.py` 使用单独的非 daemon 线程和 `event_sequence` 水位有序提交；评价失败和提交失败都显式进入终态并推进水位，重复 `job_id` 在进程生命周期内不会再次执行。
- `core/memory_commit.py` 按 `mood -> 事实认知 -> 认知情绪 -> 一跳关系` 提交。`create` 在提交时运行 resolver/judge；`same` 转为 reinforce 并保留候选情绪信号；`related` 创建独立实体后只建立一条经方向规范化的关系。
- `identity_judge` 已增加 `relation_direction`；`relation_service` 负责方向转换、对称端点排序、稳定 relation id、自环拒绝和方向不足时的保守降级。
- `reinforce` 使用 SQLite 原子 `mention_count + 1`，实体事实写入与认知情绪字段更新分离；结构化主库仍为 SQLite，实体更新同步到 Chroma。
- `main.py` 只在 commit 进入终态后释放对应 appraisal 引用；正常退出、`KeyboardInterrupt` 和输入流关闭都会先关闭/排空 appraisal，再关闭/排空 commit。
- 当前完整单元测试为 `124/124` 通过。
- 真实 LLM 开放验收已经覆盖 `create/update/reinforce`、已有认知情绪更新、`related` 独立实体和一跳关系、正负 mood 冲击、零冲击基线回归、SQLite/Chroma 跨重启召回以及退出排空。
- 开放验收中 6 个提交任务按 `event_sequence=1~6` 全部进入 `committed`，没有 `commit_failed`。因此 Step 6 的“进程内有序写入闭环”正式完成。

当前仍未完成：

- 跨进程持久提交账本；当前幂等集合只在本次进程内有效。
- SQLite 已成功而 Chroma 同步失败时的持久修复队列；当前会把任务标成 `commit_failed`，不会伪装成完整成功。
- 不可变 `ExperienceSlice` 持久化和跨进程读取；这是 Step 6 完成后的下一条施工主线。

已补齐：

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
-> 精简 PerceptionUnderstanding（情境、检索线索、不确定性）
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

12. `PerceptionUnderstanding` 的正式输出收缩为 `understanding_status`、`situated_understanding`、`memory_activation_cues`、`uncertainties`。原始事实已经由 `PerceptionEvent` 保存；不得再次生成冗长事实复述、注意力用途枚举或下一步回复建议。
13. 自动认知唤起不只服务“用户主动询问过去”。只要既有个人认知可能实质改变本轮理解、回应、行动、情绪评价或后续身份裁决，就应生成 `memory_activation_cues`；普通通用知识和仅靠当前上下文即可处理的事件仍返回空数组。
14. 主 Agent 的 System Prompt 保存稳定使用规则：当前 Phase 0 由 Persona 提供长期表达基线，mood 调节短时耐心、主动性和表达幅度，认知情绪印记只影响直接相关对象；三者按作用域并行生效，不做“有认知就忽略 mood”的瀑布回退。当前明确证据优先于可能过时的记忆。每回合动态注入只提供真实状态、被唤起认知和当前能力。
15. 长期认知由后台框架评价、裁决和提交，不是主 Agent 可直接操作的工具。主 Agent 在回复时不知道候选最终会被写入、合并还是驳回，因此不逐条请示是否保存，也不能声称已经保存、更新或删除；用户明确的“不记住/仅临时”要求必须作为事件证据保留。
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
6. 当前主 Agent 生成回复时看不到后台 appraisal/commit 的最终结果，因此可以说“收到、理解”，不能说“已经记住、保存、更新或删除”。这不是语气限制，而是运行时真值边界。
7. 具体互动事件与由该事件体现出的稳定偏好、关系或互动模式可以是 `related`，但不是同一个认知身份。跨 `memory_type` 的候选不得只因主体和主题相同被判为 `same`。

`ExperienceSlice` 必须保存当时的 `capability_snapshot`、`memory_activation_state` 和 `activated_memory_refs`。这样后台评价可以区分“Agent 当时自然想起了什么”“Agent 当时具有什么能力”以及“Agent 只是说了什么”，而不是从自然语言倒推系统状态。

## 当前 Card_slot 与时间记录状态

- 可替换的当前运行实例已经集中到 `Card_slot/`：角色模板位于 `Card_slot/persona_config.json`，当前实例的 checkpoint、SQLite 认知主库和 Chroma 索引位于 `Card_slot/memory_db/`。`config.py` 已统一指向这些路径。
- 该目录当前应理解为“角色模板 + 单个运行实例状态”的部署槽，不是纯角色卡。单角色、单实例阶段可以保持此布局；未来支持同一 Persona 的多用户或多实例时，必须再分离角色层模板与实例层数据库，不能让替换角色卡静默混用旧实例状态。
- `PerceptionEvent.occurred_at` 已经是事件发生时间的权威字段，checkpoint 也会把它保存在 `additional_kwargs["perception_event"]` 中。原始 `content` 只能保存感知内容，禁止为了让模型看见时间而把系统时间拼进用户原话。
- 当前尚未实现“模型调用前把历史 `occurred_at` 临时渲染成可见上下文”。这属于模型输入适配问题，不是事件存储问题；已登记到 `docs/POST_STEP6_KNOWN_ISSUES.md`，不插入当前 Step 6 主线。

## Checkpoint 与长期情景记忆边界

- LangChain 的通用 `SummarizationMiddleware` 达到阈值后会用“一条机器总结 + 最近消息”替换 Agent 的活动消息状态，随后由 LangGraph 写入新的 checkpoint；它不是只压缩某一次临时模型请求。
- 当前默认总结 Prompt 面向任务型 Agent，要求提取 session intent、summary、artifacts 和 next steps；它没有本项目的来源、事实、关系、情绪和不确定性边界。总结还会作为 `HumanMessage` 回到活动历史，存在把机器推断伪装成用户内容的风险。
- checkpoint 只负责短期运行恢复，不是长期经历权威库。旧 checkpoint 快照即使仍物理存在，也不能代替不可变事件档案。
- Step 6 稳定后，应先持久化不可变 `ExperienceSlice`，再建设短期上下文压缩、片段/每日摘要、周/月反思和只读经历查询工具。所有派生摘要必须保留时间范围、来源 slice id、生成时间和算法/Prompt 版本，且不得覆盖原始经历。
- 已确认的风险、施工顺序和验收条件统一记录在 `docs/POST_STEP6_KNOWN_ISSUES.md`。

## Step 6 之后的人格演化统一方案

人格演化属于 Phase 0 之后的慢速反思层，当前不修改人格代码。角色卡只是创建 Agent 实例时的模板；只有首次初始化时保存的 `personality_seed` 才是该实例不可被静默覆盖的出生人格。

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

1. `ExperienceSlice` 当前没有持久化。Step 6 稳定后、人格反思启动前，必须先建立不可变经历存储，并保留 `slice_id/event_id/action_id/observation_id` 等来源引用。
2. `trait_evidence` 与其他结构可以住在同一个 SQLite；“独立”表示职责和表结构独立，不表示新建数据库。它必须保存来源、反证、作用域、行为来源、去重状态和算法版本。
3. `TRAIT_INSIGHT` 未来可以作为新的记忆类型进入普通检索，但它只是 `PersonalitySnapshot` 的自然语言派生摘要。人格反思不能把旧 `TRAIT_INSIGHT` 当成新的行为证据，防止自我强化循环。
4. `PersonalitySnapshot` 是当前人格的结构化权威状态；`_statistic_override` 只能是它的文本渲染，不能由 LLM 随意覆盖。
5. 人格 LLM 只输出特质、方向、强度、作用域、行为来源和证据引用等语义；最终数字、变化上限、去重、版本和回滚由代码负责。
6. 人格证据来自多段已经提交的独立经历。Agent 自报、单轮输出、任务要求或 Persona 风格执行、旧反思结论都不能单独证明人格变化。
7. 框架层定义连续特质 registry 的协议、行为锚、未知状态和聚合规则；persona 层提供有依据的出生初值并选择已定义扩展；实例层保存证据与当前版本。Big Five、荣格八维或自定义维度现在不拍板，MBTI 只可作派生展示或诊断。
8. 当前人格接入后，每轮只注入 `current_personality`，不能再把出生人格作为第二份行为权重重复注入。身份、知识、安全和副作用边界不随人格演化改写。

## 下一步施工顺序

1. 持久化不可变 `ExperienceSlice`：使用稳定 id、追加式写入，支持按 id、时间范围和线程跨进程读取，并保留感知、理解、唤起引用、能力快照、行动、观察和状态快照的来源边界。
2. 建立跨进程持久提交账本和必要的存储修复机制，不能继续用进程内集合冒充崩溃恢复能力。
3. 基于持久化经历修正时间上下文和短期对话压缩，再建设片段/每日摘要与只读经历查询；派生摘要不能覆盖原始经历。
4. 经历证据稳定后，再依次建设 `trait_evidence`、`PersonalitySnapshot` 和只作派生展示的 `TRAIT_INSIGHT`；在此之前不实现人格更新。
5. 延迟分布、mood 20/50/80 表达差异、新认知初始分边界堆积、单次互动候选过多和关系邻居排序继续作为观察项，不阻塞 ExperienceSlice 持久化主线。

候选在 Phase 0 期间只在进程内从 Step 5 传给 Step 6；进程崩溃时丢弃，不建立持久化任务队列。

## 继续时的提醒

当前最大风险不是缺少更多 prompt，而是把新认知身份、情绪印记、Persona 解释和旧 Chroma-only 副作用再次混在一起。每完成一个小闭环，都要先看结构化输出、数据库变化和状态时序，再进入下一步。
