# 后台任务基础设施

更新时间：2026-09-07

## 目标与边界

`background_tasks` 是 Agent 或用户主动发起的可选耗时任务基础设施。它与
`ExperienceAppraisal`、`CommitWorker` 使用的核心经历流水线分开：前者服务咨询、
审核等业务任务，后者保证每轮经历和认知状态按顺序提交。

第一版使用进程内后台线程，SQLite 保存任务真值；Worker 不调用主 Agent、不写
checkpoint，也不直接修改长期记忆。任务结果通过 `background_task_events` 交给主循环。

## 状态与恢复

```text
queued -> running -> succeeded / failed / timeout / exhausted
                 \-> retry_wait -> queued
queued/running -> cancel_requested -> cancelled
```

启动时会把超过 `BACKGROUND_TASK_STALE_MINUTES` 的失联任务恢复为 `queued`（仍有
重试次数）或 `abandoned`，并重新扫描 `queued/retry_wait`。任务使用幂等键避免重复创建。

## 两个业务入口

- `consult_advanced_model`：同步、只读、单轮有调用次数上限，直接返回高级模型的分析、
  假设、建议和不确定性。
- `start_code_review`：异步创建 `code_review` 任务，立即返回 `task_id`。审核器只能
  使用任务范围与全局读取白名单交集中的 `review_list_directory`、`review_read_file`，
  不能写文件、执行命令或修改数据库。完成后使用 `get_background_task_status` 读取报告。

辅助工具：`list_background_tasks`、`cancel_background_task`。

模型地址和模型名由 `config.py` 的 `ADVANCED_LLM_CONFIG` 提供，密钥只从其中
`api_key_env` 指定的环境变量读取。当前 `base_url` 仍为空，因此真实高级模型调用会明确
返回 `unavailable`，但不会影响主 Agent 启动；接入具体供应商后再做真实咨询和长审核验收。

## 通知语义

后台线程完成或失败后写入持久事件并发布进程内通知。CLI 在每轮开始的安全边界消费通知；
通知只说明任务状态，不把审核结果伪装成用户消息。终端当前由阻塞式 `input()` 驱动，
因此不能保证审核完成时立即打断正在等待的输入；用户可在下一轮调用状态工具查看完整结果。

## 当前未实现

- 独立操作系统进程、跨机器调度和实时打断终端输入。
- 后台结果自动转换为新的 `PerceptionEvent` / `ExperienceSlice`；当前先通过任务账本
  和状态工具保留可追溯结果。
- 审核器自动修改代码。任何修复仍需主 Agent 的受控文件工具和用户授权。
