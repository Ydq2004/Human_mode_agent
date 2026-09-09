# 桌面应用与角色槽位方案

更新时间：2026-09-09

## 目标

把当前终端入口逐步变成一个本机桌面应用，保留现有 Agent、ExperienceSlice、后台任务和图片生成能力。第一版优先解决：

- 聊天历史可在重新打开后查看；
- 选择或拖拽图片后直接发送；
- Agent 生成的图片直接显示在消息中；
- 在当前回合结束后刷新配置；
- 启动时检查并按配置启动 ComfyUI；
- 在设置页新增、切换和移除角色实例槽位。
- 角色槽位可以直接登记已有的角色文件夹，切换后重启运行时生效。

这不是给 `main.py` 套一个窗口。终端、网页界面和未来的桌面壳都应调用同一层应用运行时。

## 已确定的实例边界

`Card_slot` 是默认的角色实例槽位，不是框架里写死的角色名称。一个槽位包含同一角色实例的：

- `persona_config.json`；
- `file_access_policy.json`；
- `memory_db/`（SQLite、Chroma、checkpoint 和后台账本）；
- `generated_images/`；
- `image_generation/` 工作流模板。

槽位注册表放在项目外部实例目录的 `.human_mode/card_slots.json`，所以切换槽位不会把注册表一起切走。当前实现位于 `core/card_slot_manager.py`：

- `default` 指向项目现有的 `Card_slot`，显示为柳如烟默认槽位；
- 默认槽位受保护，不能删除；
- 新槽位只复制角色配置和工作流，不复制历史数据库、checkpoint 或生图结果；
- 移除槽位只从注册表移除，保留原目录，避免设置页误删历史；
- 当前活动槽位不能直接移除，必须先切换到其他槽位；
- `HUMAN_MODE_CARD_SLOT` 只作为外部启动器的临时路径覆盖，不修改注册表。

配置模块在进程启动时解析活动槽位，然后所有数据库、向量库、checkpoint、策略和图片输出路径都跟随该槽位。已经打开的运行时不能在一次模型调用中途换槽位；正式切换应在当前回合结束后关闭旧运行时，再创建新运行时。

## `thread_id` 规则

`thread_id` 是同一角色槽位下的长期对话线程标识。聊天页面不提供随手编辑入口，只在设置页显示和修改。

修改流程：

1. 当前回合和需要提交的后台任务先完成；
2. 校验新的 `thread_id`；
3. 关闭当前 Agent/checkpoint/worker；
4. 重新创建运行时并加载新线程历史；
5. 用户确认后才保存设置。

线程切换不会删除旧经历。旧线程仍保留在同一槽位的数据库中，可以作为以后历史浏览或管理功能的来源。

## 推荐技术路线

```text
桌面界面（本机 Web 页面，后续可用 pywebview 包装）
        ↓
ApplicationRuntime / Session
        ↓
现有 Agent 处理链、ExperienceSlice、后台任务
        ↓
ComfyUIProcessManager
```

实施顺序：

1. 槽位管理和启动配置（当前）；
2. 从 `main.py` 抽取 `ApplicationRuntime`，CLI 仅作为适配器；
3. 本机 `127.0.0.1` Web UI：会话、历史、文本、图片和后台状态；
4. 配置刷新和安全的运行时重建；
5. ComfyUI 健康检查、启动、日志和退出管理；
6. 使用 `pywebview` 包装窗口，最后用 PyInstaller `one-folder` 打包。

当前可以先用下面的方式启动本机 Web MVP：

```powershell
.\.venv\Scripts\python.exe -m app
```

然后打开 `http://127.0.0.1:8765`。当前页面是开发用浏览器界面，尚未代表最终 exe；
第一次启动仍会加载本地嵌入模型和现有 Agent 依赖。

应用在项目目录的 `.human_mode/app.lock` 上执行单实例锁；已有 Web 后端仍运行时，第二次启动会直接
提示已有实例，避免多个进程争用同一槽位的 checkpoint。网页设置按钮固定在窗口右上角，历史打开后
自动定位到最新消息。

角色槽位管理使用“登记文件夹”：在设置中填写包含 `persona_config.json` 的角色目录绝对路径即可。
可选填写槽位 ID 和显示名称；登记不会复制目录或历史数据。

## 不能省略的成熟应用基础

这些不是装饰功能，而是产品化时必须补齐的边界：

- 同一 `thread_id` 的请求串行化，避免 checkpoint 和事件序号冲突；
- UI 不直接读写 SQLite、Chroma 或 checkpoint，只调用运行时接口；
- 聊天历史使用结构化经历和 checkpoint 的受控读取，不解析终端输出；
- 图片上传和展示使用受控附件/生成图目录，不能暴露任意本地路径；
- 模型调用、视觉调用和 ComfyUI 生图的处理中、失败、取消状态要可见；
- ComfyUI 已运行时复用，未运行时才由应用启动，只关闭应用自己启动的进程；
- 配置刷新采用“校验、排空、重建”，不使用 `importlib.reload` 半途替换对象；
- API 密钥继续只从环境变量读取，不写入槽位配置和前端页面；
- 应用默认只监听本机，不直接开放局域网端口；
- 关闭应用时等待或明确记录仍未完成的后台任务。

### 退出应用

Web 界面打开“设置”，点击“退出应用”即可关闭本机 Web 后端和 Agent 运行时；关闭请求会先让
评价、提交等后台 worker 完成收尾。若是从终端直接启动服务，则使用 `Ctrl+C`，效果相同。

默认情况下，应用不会关闭已经独立运行的 ComfyUI。只有应用自己启动且配置了
`COMFYUI_STOP_ON_APP_EXIT=True` 时，退出应用才会同时停止该 ComfyUI 进程。

## 当前实现进度

已新增 `app/runtime.py` 的第一版 `ApplicationRuntime`。它把一个槽位和一个
`thread_id` 的 Agent、后台 worker、事件序号、历史读取和关闭顺序放进同一生命周期容器：

- `submit_text()` / `submit_image()` 统一进入 `PerceptionEvent`；
- 同一运行时通过锁串行处理事件；
- `history()` 从 `ExperienceSlice` 生成 UI 消息，不解析终端输出；
- `process_perception_event()` 现在返回经过 `generated_images` 目录校验的媒体元数据，UI 不需要
  解析工具 JSON 或接触绝对本地路径；
- `status()` 提供槽位、线程、事件序号和能力状态；
- `shutdown()` 先排空评价/提交，再关闭其他 worker。
- 运行时保留 checkpoint 连接句柄，关闭/重建时显式释放，避免刷新配置或切换线程后累积 SQLite 锁。

当前运行时仍由现有 `main.py` 的业务函数提供处理逻辑，CLI 行为保持不变；下一步要把终端循环
改成调用该运行时，并为 UI 增加非阻塞任务状态与文件授权确认桥。

同时新增 `core/comfyui_process.py` 监督器。应用启动时先健康检查当前
`COMFYUI_BASE_URL`，已有服务直接复用；只有显式配置 `COMFYUI_EXECUTABLE`、参数和工作目录时
才启动外部 ComfyUI。启动日志写入当前槽位的 `runtime_logs/`，启动超时会记录失败但不阻塞文字
聊天，退出时只按 `COMFYUI_STOP_ON_APP_EXIT` 关闭本程序拥有的进程。

ComfyUI 的路径不写死在框架里。可通过本机环境变量配置：

```powershell
$env:COMFYUI_EXECUTABLE = 'E:\ai\ComfyUI-aki-v1.6\ComfyUI-aki-v1.6\python\python.exe'
$env:COMFYUI_WORKDIR = 'E:\ai\ComfyUI-aki-v1.6\ComfyUI-aki-v1.6\ComfyUI'
$env:COMFYUI_ARGS_JSON = '["main.py","--auto-launch","--preview-method","auto"]'
```

未配置可执行文件时只复用已经运行的 ComfyUI，不会猜测用户机器上的路径。

## 当前未完成

当前仍未实现：

- CLI 完整迁移到 `ApplicationRuntime`；
- 非阻塞后台任务状态和文件授权确认桥；
- pywebview/PyInstaller 打包。

这些模块应逐个形成可测试的小闭环，不把所有改动一次塞进 `main.py`。
