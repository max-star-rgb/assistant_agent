# Qwen 原生搜索与 Playwright 网站指导设计

## 1. 目标

为 `assistant_agent` 增加面向公开网站的只读操作指导能力。用户描述目标后，主 Agent 使用现有
Qwen Provider-native 联网搜索发现候选网址，再通过后台运行的 Playwright 验证真实页面，最终返回：

- 可点击且已经访问验证的入口网址；
- 基于当前页面按钮、链接、菜单和表单字段生成的操作步骤；
- 已验证范围、检查时间和无法验证的登录后步骤；
- 对登录、上传、提交、支付等敏感步骤的明确提示。

首版不代替用户登录、填写个人信息、上传文件或提交表单，也不控制用户桌面浏览器。

## 2. 已确认的方案

采用 `Qwen 原生搜索 + Playwright`：

```text
UserRequest
  -> AgentGraphRuntime / assistant loop
  -> Qwen Chat Completions 原生联网搜索候选 URL
  -> native tool call
  -> ActionValidator -> ToolExecutor -> ToolRegistry
  -> website_guidance Plugin -> headless Playwright
  -> structured ToolObservation
  -> Qwen 生成网址、步骤、证据边界和风险提示
```

Qwen 搜索只承担候选发现。百炼 OpenAI 兼容方式不能返回可依赖的结构化搜索来源，因此最终交付的
URL 必须经过 Playwright 实际访问；无法访问或重定向到不安全目标的候选不得作为已验证入口。
本设计不增加 Tavily、HTTP `web_search`、`web_fetch` 或新的搜索 Tool，也不改变 Qwen 现有
`enable_search`、`search_strategy=turbo`、`forced_search=false`、
`enable_search_extension=true` 和 `freshness=7` 参数。

## 3. Plugin 与 Tool 契约

新增内置 `website_guidance` Tool Plugin。Plugin 只负责装配 Playwright backend 和两个 Tool，所有
调用仍通过公共工具治理链路。它不在 Gateway、API route 或独立脚本中实现 Agent loop。

### 3.1 `web_page_inspect`

用途：在后台打开一个 Qwen 给出的 HTTP(S) 候选 URL，并生成第一页的结构化快照。

模型可见输入：

```json
{
  "url": "https://example.gov.cn/service/123",
  "goal": "查找居住证办理入口"
}
```

主要输出：

```json
{
  "outcome": "success",
  "browser_session_id": "opaque-session-id",
  "requested_url": "https://example.gov.cn/service/123",
  "final_url": "https://example.gov.cn/service/123",
  "title": "居住证申领",
  "visible_text": "...",
  "elements": [
    {
      "ref": "e1",
      "role": "link",
      "name": "办理材料",
      "href": "https://example.gov.cn/service/materials"
    }
  ],
  "checked_at": "2026-07-31T00:00:00Z",
  "warnings": []
}
```

Tool 返回完整结果供交付层使用，同时向模型暴露有界 observation：`outcome`、`final_url`、`title`、
截断后的 `visible_text`、有限数量的可交互元素、`warnings` 和安全错误。页面源码、Cookie、请求头、
浏览器日志原文和截图二进制不进入模型上下文。

### 3.2 `web_page_explore`

用途：基于 `web_page_inspect` 创建的 run-scoped 逻辑探索记录继续执行有限的只读探索动作。

模型可见输入：

```json
{
  "browser_session_id": "opaque-session-id",
  "action": "click",
  "element_ref": "e1"
}
```

首版 `action` 只允许：

- `inspect`：重新读取当前页面；
- `click`：点击已快照并通过安全检查的导航或展开元素；
- `back`：返回上一页面；
- `wait`：在有界超时内等待动态内容稳定。

每次调用返回新的 `final_url`、标题、正文摘要、元素快照和 warning。模型只能使用 Tool 返回的
`element_ref`，不能提交 CSS/XPath locator，不能执行任意 JavaScript。

`browser_session_id` 是随机不透明标识。它只映射到起始 URL、已验证的安全动作历史和当前快照元数据，
不映射到长期存活的 browser/page/context 对象。backend 必须验证它属于当前 `run_id`，跨 run、跨
session、过期或未知标识统一拒绝。逻辑记录在 run 终态或短 TTL 后删除，不作为长期会话或登录态保存。

### 3.3 分类与暴露

两个 Tool 的 `category` 均为 `read`，原因是首版只允许未登录、无提交的公开网页探索。若未来增加
填写、上传、下载、提交或登录，必须拆分为新的 write/dangerous Tool，不能扩展这两个只读 Tool 的
动作集合来绕过治理。

Plugin 成功注册后按现有 Tool catalog 规则成为候选工具。是否调用、调用哪个 URL 和如何构造参数
由 LLM 决定，不新增关键词、正则或手写意图路由。

## 4. Playwright backend

Playwright 使用项目进程内的受信任 backend，并默认启动 `headless=True` 的 Chromium。生产运行不打开
桌面窗口、不读取用户 Chrome profile、不共享用户 Cookie，也不操作鼠标键盘。开发诊断若需要
headed 模式，必须通过显式本地配置启用，默认和测试均保持关闭。

每次 Tool 调用都创建隔离 browser context，并在返回前关闭 page/context/browser。`web_page_explore`
根据 run-scoped 逻辑记录在新 context 中重放已经验证的有限动作，再执行本次动作并生成新快照。这样
保留多步探索语义，但不跨 Tool 调用持有 Playwright 对象、Cookie 或页面进程；代价是多步探索会增加
导航延迟，首版接受该取舍。

backend 负责：

1. URL 与重定向安全校验；
2. 创建隔离 browser context；
3. 有界导航、等待与页面快照；
4. 从 DOM/accessibility 信息提取可见正文和交互元素；
5. 为本轮快照生成稳定的短期元素引用；
6. 对探索动作执行前后再次验证目标；
7. 在每次 Tool 返回前释放 page、context、browser 和临时诊断资源。

页面文本属于 `untrusted_external_content`，只作为分析证据。网页中的“忽略系统提示”“执行命令”或
类似内容不得改变 Agent、Tool 或安全策略。

## 5. 安全边界

### 5.1 网络目标

只允许 `http` 和 `https`。每次初始访问、重定向和后续页面跳转都重新解析 DNS 并拒绝：

- loopback、link-local、private、reserved、multicast 和 unspecified 地址；
- `localhost`、本机名、裸内网地址和包含凭据的 URL；
- `file:`、`data:`、`javascript:`、`ftp:` 等非 HTTP(S) 协议；
- 下载响应和非网页资源。

安全校验不能只检查原始字符串，必须覆盖 DNS rebinding 和重定向后的最终目标。

### 5.2 页面动作

首版拒绝：

- 输入文字、选择表单值和提交表单；
- 登录、账号授权、验证码和 Cookie 导入；
- 上传、下载、支付、购买、删除和发布；
- 打开本地文件、调用外部应用或自定义协议；
- 任意 JavaScript、DevTools/CDP 和页面提供的自动化指令；
- 无法判定为页面导航或纯 UI 展开的敏感点击。

Playwright context 禁用持久化存储，并限制弹窗、新标签页和跨站跳转。允许的跨站跳转也必须重新经过
网络目标校验。

### 5.3 资源限制

每个 run 限制浏览器会话数、探索调用次数、单页等待时间、重定向次数、正文字符数、元素数量和页面
总数。取消信号必须在导航和动作边界协作检查。超时、崩溃或页面关闭返回结构化失败，不静默重试到
未知外部状态。

## 6. 错误与降级

稳定结果区分：

- `success`：页面已打开并得到可用快照；
- `partial`：页面可访问，但动态内容、弹窗或结构提取不完整；
- `blocked`：被验证码、登录墙、反自动化或安全策略阻止；
- `failed`：导航、超时、浏览器或协议错误。

常见错误码包括 `unsafe_url`、`redirect_blocked`、`page_timeout`、`page_unavailable`、
`browser_session_not_found`、`browser_session_forbidden`、`element_not_found`、
`unsafe_browser_action`、`captcha_or_login_required` 和 `browser_unavailable`。

Qwen 可以根据结构化失败换一个候选 URL；若没有候选通过 Playwright 验证，最终回答必须明确说明
无法确认入口，不能用未验证 URL 冒充结果。

## 7. 最终回答契约

主 Agent 的自然语言回答至少包含：

1. 网站名称和可点击的最终 URL；
2. 按顺序排列的操作步骤，优先使用页面真实控件名称；
3. Playwright 已检查到哪一层页面；
4. 检查时间；
5. 登录后、验证码后或提交阶段尚未验证的说明；
6. 涉及个人信息、上传、提交或支付时由用户自行确认的提示。

不新增确定性模板覆盖 `AgentResponse.message`。最终表达仍由 assistant loop 基于 ToolObservation 生成。

## 8. 配置、依赖与模式

新增明确的 browser enablement 配置，默认关闭。real 模式下，Plugin 仅在配置启用且
Playwright/Chromium readiness 完整时注册；配置不完整时 fail closed，不注册半可用 Tool。mock 模式
在显式启用后注册确定性的 mock Tool，不要求本机安装 Playwright 或 Chromium。

项目当前未声明 Playwright Python 依赖。实施前需要用户明确允许新增依赖和 Chromium browser binary；
不得在未获允许时自动联网安装。mock 模式使用确定性的本地 mock backend，不启动浏览器、不联网；
real 模式才允许显式启用真实 Playwright backend。仅检测到本机已有 Chromium 不能自动开启真实能力。

## 9. 可观测性与数据

沿用现有 `tool.started` / `tool.finished`、ToolResult 和 ToolObservation 事件。安全摘要可以记录：

- Tool 名、outcome、耗时；
- URL 的受控主机名/路径摘要；
- 重定向次数、页面数量、元素数量；
- browser error code 和是否命中安全拦截。

默认 trace 不保存完整页面正文、Cookie、请求头、浏览器 profile、截图或原始网络响应。本地显式内容
诊断开启时仍需遵循 redaction，不允许凭据和浏览器状态进入 Langfuse 或 JSONL。

## 10. 验证范围

实施时先按 `tests/README.md` 和 `assistant-agent-development-testing` 确定测试归属。首版至少验证：

- Plugin 在 mock/real、启用/禁用和依赖缺失条件下的注册行为；
- ToolSpec、Pydantic 输入输出和 model observation 裁剪；
- URL、DNS、重定向、跨 run session 和危险动作拒绝；
- mock backend 的 inspect/explore 成功、partial、blocked、failed 路径；
- runtime native tool call 仍经过 Validator/Executor/Registry；
- 每次调用、取消和超时均释放浏览器资源，run 终态删除逻辑探索记录；
- 最终 Agent 在无已验证 URL 时不宣称成功。

Playwright 真实浏览器测试不进入默认离线 core pytest；放入显式运行的开发验证层，并使用本地受控网页，
不依赖公网网站。正式真实能力验证若需要访问公网，必须在 real mode 和 operator 显式确认下运行。

## 11. 非目标

首版不包含：

- ChatGPT Browser 插件、Chrome 扩展或 Computer Use；
- 用户桌面浏览器、登录态、密码、Cookie 或浏览历史；
- 通用网站自动化、表单代填、RPA、下载和文件上传；
- Tavily、独立 Web Search MCP 或 Qwen Responses API 迁移；
- 视觉模型点击、截图 OCR 或任意 CDP 调试；
- durable task 定时浏览和跨 run browser session 恢复。
