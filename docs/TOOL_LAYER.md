# 通用工具层（第一阶段）

工具是框架层能力。角色卡可以影响表达方式，但不能改变工具的真实执行结果、权限边界或副作用。

## 已落地工具

- `get_current_time`：读取指定时区的机器时间。
- `calculate`：使用受限 AST 计算数学表达式，不执行任意 Python。
- `search_text`：在文件读取白名单内搜索文本，返回有限的路径、行号和匹配行。
- `read_image_metadata`：读取图片格式、宽高、大小和 SHA-256；不解释画面内容。
- `execute_command`：不经过 shell 执行当前白名单中的 Python 测试/版本查询和 Git 只读命令；默认同步执行，也可用 `background=true` 进入后台任务。
- `http_request`、`web_search`：受限公网读取和外部搜索；结果不自动写入长期记忆。
- `browser_open`、`browser_snapshot`、`browser_close`：临时只读浏览器会话。
- `browser_click`、`browser_fill`：只提出确认请求，必须由宿主界面批准后执行。
- `start_process`、`get_process_status`、`get_process_output`、`terminate_process`：复用后台账本运行和管理受限长命令。

新工具统一返回 `tool`、`status`、`result`、`error`、来源和时间字段。主 Agent 只能依据真实 `status` 描述结果。
能力快照还会为每个已注册工具记录本地执行位置和是否可能产生副作用，便于感知理解和后台审计。

## 执行边界

`execute_command` 接收 `argv` 数组，不接收 shell 字符串，不支持 `&&`、管道或重定向。第一阶段只允许：

- `python -m unittest ...`
- `python --version`
- `git status`
- `git diff`
- `git log`

命令有超时、输出长度和工作目录白名单限制，失败、超时和拒绝都会明确返回。安装依赖、删除文件和任意后台进程仍不开放；后台进程工具只接受相同的只读命令白名单。
预计很快完成的命令直接执行；测试、构建等可能超过同步超时的命令才选择后台，返回 `task_id` 后使用后台任务状态工具查询或取消。
文本搜索的匹配范围受读取白名单约束，并拒绝绝对或越级 glob；Git 参数会拒绝输出文件、外部 diff 和 textconv 等可能产生副作用的选项。

## 后续方向

浏览器交互通过 `confirmation_id` 交给宿主界面确认，Agent 不能自行批准。现实设备副作用仍未开放。
