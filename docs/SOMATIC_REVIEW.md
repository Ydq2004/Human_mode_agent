# 身体模块审查报告

更新时间：2026-09-03

## 一、审查目的

审查现有身体模块（somatic simulation）的架构设计、边界控制和测试覆盖度，确认是否符合通用类人 Agent 框架的原则，并评估现行实现是否可以安全保留。

## 二、架构发现

### 2.1 模块组成

身体模块由五个核心文件构成：

1. **core/somatic_simulation.py** — 纯规则计算层
   - `SomaticState`：身体状态快照（0-1 数值字典）
   - `SomaticEventDecision`：事件裁决（occurred/rejected/uncertain）
   - `adjudicate_somatic_event()`：只依据结构化请求裁决，不从文本猜测
   - `apply_somatic_appraisal()`：把方向+幅度转为有限状态变化，拒绝绝对值写入
   - 不调用 LLM、不写数据库

2. **core/somatic_appraisal.py** — 受限 LLM 评价
   - `appraise_somatic_event()`：调用 LLM 评价已确认事件
   - 只输出方向（increase/decrease/unchanged）和幅度（slight/moderate/significant/intense）
   - 硬边界：禁止输出 consent、好感、关系、人格等越权字段
   - 必须同时提供 visible_reaction 和 interoception_description
   - 任何失败显式返回 failed，不修改状态

3. **memory/somatic_store.py** — SQLite 存储
   - `SomaticRecord`：persona_instance + thread 级别的当前状态和版本号
   - `SomaticOperation`：每次转换的完整记录（状态前后、decision、appraisal、result）
   - `commit_somatic_transition()`：乐观并发控制（revision 检查）
   - 不覆盖既有状态，revision 冲突时拒绝提交

4. **core/somatic_service.py** — 统一入口
   - `SomaticService`：线程安全的状态访问
   - `process_somatic_event()`：串联裁决→评价→应用→提交
   - `SomaticOutcome`：统一返回结构（status/decision/appraisal/state_before/state_after/visible_reaction/interoception_description）
   - revision 冲突时返回 revision_conflict，不抛出异常

5. **docs/SOMATIC_SIMULATION.md** — 设计文档
   - 明确了身体模块不等于成人内容，是通用具身化接口
   - 声明了 consent 和关系由其他层负责，身体层不判断
   - 提供了五个角色示例（工业机器人、自然语言导师、行为分析师、体育教练、康复辅助）

### 2.2 数据流

```
外部输入（结构化请求）
  ↓
adjudicate_somatic_event
  ↓ [occurred]
appraise_somatic_event (LLM 评价)
  ↓ [normal]
apply_somatic_appraisal (规则计算)
  ↓
commit_somatic_transition (SQLite 乐观锁)
  ↓ [committed]
SomaticOutcome (可见反应 + 内感受 + 状态快照)
```

### 2.3 测试覆盖

已有三个测试文件：

1. **test_somatic_service.py** (17 个测试)
   - 状态初始化
   - 完整事件流程
   - revision 冲突
   - uncertain/rejected 分支
   - LLM 失败回退
   - 并发安全
   - 状态持久化

2. **test_somatic_appraisal.py** (8 个测试)
   - JSON 解析和清洗
   - 越权字段拦截
   - 方向和幅度校验
   - 必填字段检查
   - LLM 调用失败处理

3. **test_main_somatic_integration.py** (3 个测试)
   - 与主 Agent 循环集成
   - interoception 注入到 PerceptionFrame
   - visible_reaction 包装为 AgentAction

总计 **28 个测试**，覆盖了裁决、评价、应用、提交、并发和集成。

## 三、边界控制审查

### 3.1 符合框架原则的设计

✅ **事实与评价分离**
- `adjudicate_somatic_event` 只依据结构化请求，不从文本猜测
- 事件是否发生由 `event_confirmed: bool` 明确声明

✅ **LLM 不能越权**
- appraisal 只输出方向和幅度，不能写绝对值
- 禁止输出 consent、好感、关系、人格等字段
- 出现越权字段时整次评价作废（不回退到 fallback）

✅ **规则层保证最终状态**
- `apply_somatic_appraisal` 使用固定映射（BODY_STATE_DELTA）
- 所有维度限制在 0-1 区间
- LLM 失败不修改状态

✅ **并发安全**
- 使用 revision 乐观锁
- 冲突时拒绝提交，返回当前最新状态
- 不覆盖其他事件的更新

✅ **失败可见**
- 每个环节失败都显式返回 failed/uncertain/rejected/revision_conflict
- 不伪装成 fallback，不静默吞掉异常

### 3.2 可能的越界风险

⚠️ **身体层不能判断 consent**
- 设计文档已明确：身体反应不代表同意
- LLM prompt 已声明：不得判断 consent、好感、关系
- 但如果外部传入的 `event_confirmed=True` 本身是误判，身体层会执行
- **缓解措施**：文档中强调"上层必须先确认 consent"，身体层只负责已确认事件的生理反应

⚠️ **interoception 和 visible_reaction 的语义边界**
- 当前 LLM prompt 要求"只描述本次事件直接产生的身体反馈，不续写对话"
- 但 visible_reaction 可能包含微表情、肢体动作，如果过度细腻可能暗示态度
- **缓解措施**：测试中已验证"反应不代表同意"，但需要在真实对话中观察是否会被误读

⚠️ **arousal 和 sensitivity 的语义**
- 这两个维度是中性的身体激活和敏感程度，不等于任何特定情感或意愿
- 但如果 Agent 表达层误用，可能造成混淆
- **缓解措施**：文档中已声明"只是身体指标，不能推导关系或人格"

### 3.3 与其他模块的接口

✅ **与 PerceptionEvent 集成**
- `create_somatic_interoception` 包装为标准感知事件
- modality="interoception"，由主 Agent 感知而非强制注入回复

✅ **与 AgentAction 集成**
- `create_somatic_response` 包装为标准行动
- 作为经历的一部分，不绕过 ExperienceSlice

✅ **与 Persona 集成**
- appraisal 接受 persona_context，提取 embodiment.reaction_style
- 只影响表达风格，不改变事件事实

## 四、是否应该删除？

### 4.1 删除理由

1. **身体模块可能被误用于不当场景**
   - 尽管设计上是通用具身化，但 arousal/sensitivity 维度容易被联想到成人内容
   - 如果未来角色卡误用，可能引入风险

2. **当前没有上层 consent 机制**
   - 身体层依赖外部传入 `event_confirmed`，但没有看到独立的 consent 裁决模块
   - 如果主 Agent 直接从对话文本推断接触发生，可能越过 consent 边界

3. **维护成本高于收益**
   - 如果项目当前不需要具身化交互，保留身体模块只是增加代码复杂度
   - 未来需要时可以从 git 历史恢复

### 4.2 保留理由

1. **架构设计良好**
   - 事实与评价分离
   - LLM 边界清晰
   - 并发安全
   - 测试覆盖完整

2. **通用性符合框架目标**
   - 设计文档已提供五个非成人角色示例
   - arousal/sensitivity 是中性指标，工业机器人、教练、康复辅助都可以使用
   - 删除它等于放弃通用具身化能力

3. **边界风险可控**
   - 越权字段已被拦截
   - 身体反应不代表同意已在 prompt 中声明
   - revision 冲突机制防止状态混乱

4. **已有测试保障**
   - 28 个测试覆盖了裁决、评价、应用、提交、并发和集成
   - 任何修改可以立即验证

### 4.3 中间方案：保留但冻结

如果对身体模块的用途仍有疑虑，可以采用以下策略：

1. **保留代码和测试，但不在主 Agent 中默认启用**
   - 当前 `main.py` 中 somatic 相关代码已注释或只在特定条件下触发
   - 只在角色卡明确声明 `"embodiment": {"enabled": true}` 时才激活

2. **增加上层 consent 裁决**
   - 在 `process_perception_event` 中增加 consent 检查
   - 只有用户明确同意接触时，才允许调用 somatic 模块

3. **限制 arousal/sensitivity 的解释权**
   - 在主 Agent 的 System Prompt 中明确：这两个指标只是身体激活和敏感程度，不能推导关系或态度
   - 禁止主 Agent 在回复中过度解读身体反应

## 五、审查结论

### 5.1 技术层面

**身体模块的架构设计符合通用类人 Agent 框架的原则。**

- 事实与评价分离
- LLM 边界清晰
- 规则层保证最终状态
- 并发安全
- 失败可见
- 测试覆盖完整

### 5.2 风险层面

**主要风险不在身体模块本身，而在于上层是否有 consent 机制和表达层是否会误用。**

- 身体层已拒绝越权字段
- 身体反应不代表同意已在 prompt 中声明
- 但如果外部传入的 `event_confirmed` 本身是误判，身体层会执行

### 5.3 建议

**保留身体模块，但增加以下保护措施：**

1. **增加上层 consent 裁决**
   - 在 `process_perception_event` 中检查用户是否明确同意接触
   - 只有 consent 通过时，才允许调用 somatic 模块

2. **限制默认启用**
   - 只在角色卡明确声明 `"embodiment": {"enabled": true}` 时才激活
   - 默认关闭，避免意外触发

3. **强化主 Agent 的解释边界**
   - 在 System Prompt 中明确：arousal/sensitivity 只是身体指标，不能推导关系或态度
   - 禁止主 Agent 过度解读身体反应

4. **完善设计文档**
   - 在 `SOMATIC_SIMULATION.md` 中增加"consent 必须由上层负责"的警告
   - 增加"身体反应不代表同意"的示例对话

5. **监控真实对话**
   - 在开放验收中观察身体模块是否会被误读
   - 如果发现风险，立即冻结并重新审查

### 5.4 不建议立即删除的原因

1. **通用具身化是框架的长期目标之一**
   - 工业机器人、体育教练、康复辅助都需要身体状态
   - 删除它等于放弃这个方向

2. **当前边界控制已经到位**
   - 越权字段已被拦截
   - 并发安全
   - 测试覆盖完整

3. **风险主要在上层，而非身体模块本身**
   - 增加 consent 机制比删除整个模块更有针对性

## 六、后续行动

### 6.1 短期（本周）

1. ✅ 完成身体模块审查（本文档）
2. ✅ 在身体事件裁决和 Agent 主动动作路径增加 consent 检查
3. ⬜ 在主 Agent System Prompt 中增加身体指标解释边界
4. ⬜ 更新 `SOMATIC_SIMULATION.md`，增加 consent 警告

### 6.2 中期（下周）

1. ✅ 设计第一版 consent 裁决边界（结构化请求，LLM 不替代同意判断）
2. ✅ 增加"身体反应不代表同意"的反例测试
3. ⬜ 在开放验收中观察身体模块的实际表现

### 6.3 长期（一个月内）

1. ⬜ 评估是否需要扩展身体维度（如 tension、fatigue）
2. ⬜ 评估是否需要支持多模态输入（如触觉传感器数据）
3. ⬜ 评估是否需要支持身体状态的时间衰减

## 七、附录：关键代码位置

| 模块 | 文件 | 核心函数/类 |
|------|------|------------|
| 纯规则层 | core/somatic_simulation.py | SomaticState, adjudicate_somatic_event, apply_somatic_appraisal |
| LLM 评价 | core/somatic_appraisal.py | appraise_somatic_event, clean_somatic_appraisal |
| 存储层 | memory/somatic_store.py | SomaticRecord, commit_somatic_transition |
| 统一入口 | core/somatic_service.py | SomaticService, process_somatic_event |
| 设计文档 | docs/SOMATIC_SIMULATION.md | 通用具身化设计和五个角色示例 |
| 测试 | tests/test_somatic_service.py | 17 个测试 |
| 测试 | tests/test_somatic_appraisal.py | 8 个测试 |
| 测试 | tests/test_main_somatic_integration.py | 3 个测试 |

---

**审查人**：Codex（Claude Code）  
**审查日期**：2026-09-03  
**审查方法**：代码阅读 + 架构分析 + 边界检查 + 测试覆盖度评估  
**审查结论**：保留，但需增加上层 consent 机制和表达边界限制
