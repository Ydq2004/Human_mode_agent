# 网络、搜索与浏览器工具

这三类能力属于框架层工具，不属于角色卡，也不会自动写入长期记忆。

## HTTP 请求

`http_request` 只允许 GET，默认只访问 HTTPS 公网地址，禁止 URL 用户名密码、私网地址和未经检查的重定向。响应体有大小上限，工具返回真实 HTTP 状态和是否截断。Agent 不能传入任意请求头，因此不能借此调用 ComfyUI 管理接口或泄露凭据。

## 网页搜索

`web_search` 通过 `WebSearchService` 调用已配置的供应商（当前适配 Brave）。API Key 只从环境变量读取并由服务层注入，搜索结果统一为标题、URL、摘要、来源和排名。结果是外部资料，不能直接成为角色个人经历或长期记忆。

## 浏览器

浏览器是可选依赖。每个运行时创建独立的临时 Playwright BrowserContext，并使用不可猜测的 session/page ID。第一阶段只提供打开页面、读取有限页面快照和关闭会话；点击、输入、提交、登录、下载和上传等副作用动作保留为后续确认式能力。缺少 Playwright 或浏览器内核时，工具返回 `unavailable`，不会假装成功。

## 配置与验证

搜索配置：`WEB_SEARCH_PROVIDER`、`BRAVE_SEARCH_API_KEY`、可选 `WEB_SEARCH_ENDPOINT`。能力快照会分别记录 HTTP、搜索和浏览器是否可用。网络策略在每次 HTTP 重定向和浏览器打开后的最终 URL 上重新检查。
