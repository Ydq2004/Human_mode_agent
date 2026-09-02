# 文件工具

## 当前状态

第一版已经接入主 Agent，工具包括：

- `read_file`：读取白名单内的 UTF-8 文本文件。
- `list_directory`：列出白名单目录，支持有限递归和数量上限。
- `write_file`：写入白名单内文件；使用临时文件、`fsync` 和原子替换。
- `apply_patch`：要求旧文本唯一匹配后再替换，避免改错位置。

## 权限边界

权限配置位于实例层的 `Card_slot/file_access_policy.json`，不放在角色卡中。
`read_roots` 和 `write_roots` 分开配置。默认读取范围是项目目录，默认写入范围是
`docs` 和 `tests`。

代码会先解析绝对路径、`..` 和符号链接，再检查白名单。敏感文件（例如 `.env`、
私钥）和系统目录始终拒绝访问。白名单外的写入或补丁只返回
`confirmation_required`，不会执行；当前版本尚未接入 CLI 的交互授权流程。

所有工具都返回 JSON，至少包含 `operation`、`status` 和 `path`。主 Agent 只有在
看到 `status=success` 后才能说文件已经写入或修改。模型不能自行制造有效的授权编号。

## 经历记录

文件工具调用继续沿用现有 `AgentAction(tool_call)` 和
`PerceptionEvent(source="tool", modality="tool_result")`，随
`ExperienceSlice` 保存。回忆工具仍使用专用的 `memory_recall_result` 类型。
文件内容不会自动写入长期认知；若以后需要记住文件中的信息，应由 Agent 从工具
结果中提取事实，再经过正常的经历评价和提交链。

## 测试

`tests/test_file_tools.py` 覆盖：读取/写入白名单、路径越界、敏感文件、目录列表、
唯一补丁、重复匹配拒绝，以及结果不落盘的情况。授权交互和真实长对话中的工具
选择仍需后续手动验收。
