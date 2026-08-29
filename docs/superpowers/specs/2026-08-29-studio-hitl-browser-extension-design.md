# Studio HITL 浏览器扩展设计

日期：2026-08-29

## 1. 背景与目标

当前生产 Agent 已通过 Deep Agents `HumanInTheLoopMiddleware` 输出标准 LangGraph HITL
interrupt：`action_requests` 描述待审批 Tool 调用，`review_configs` 声明允许的
`approve`、`edit`、`reject` 决策。后端审批、checkpoint 和 resume 语义正确；问题在于托管的
LangSmith Studio 要求用户手写 JSON 作为 interrupt response，日常使用不友好。

本次新增一个仅用于本地开发的 Google Chrome Manifest V3 扩展。在用户当前打开的 Studio
thread 页面中注入隔离审批浮层，将标准 HITL interrupt 渲染为可查看、可按字段编辑、可批准或
拒绝的表单，并通过 Agent Server 公开 API 恢复同一个 thread。扩展不修改托管 Studio 私有代码，
不改变生产 Agent 的 HITL 协议。

成功标准：

- 用户始终停留在当前 Studio 页面；发生 HITL 时自动出现审批浮层。
- 普通运行不显示浮层，也不改变 Studio 的 Graph、Trace、state 和原始 JSON 能力。
- 用户能够查看参数、按字段修改参数、批准或填写原因后拒绝。
- 多个并行 action 按原顺序收集全部 decision 后一次 resume。
- stale interrupt、网络失败或服务端拒绝时不提交、不自动重试副作用。

## 2. 范围与非目标

第一版只支持：

- Google Chrome Manifest V3；
- `https://smith.langchain.com/studio/*`；
- 本机 `http://127.0.0.1:8089` Agent Server；
- LangChain 当前标准 `action_requests` / `review_configs` HITL payload；
- `approve`、`edit`、`reject` 三类决策。

第一版不做：

- fork、镜像或重新实现完整 LangSmith Studio；
- Firefox、Edge、远端部署或任意 `baseUrl`；
- 自建用户系统、审批队列、审计数据库或另一套 run manager；
- 修改 Tool schema、HITL middleware、checkpoint 或副作用幂等语义；
- 为未知自定义 interrupt 猜测表单或 resume payload。

## 3. 方案选择

评估过三个方案：

1. 直接操纵 Studio 的私有 DOM，并填充、点击原始 JSON 控件。代码最少，但强绑定托管页面结构，
   Studio 更新后容易失效。
2. Chrome 扩展从 Studio URL 取得公开 `baseUrl` 与 `thread_id`，直接调用 Agent Server 公开 API，
   在 Shadow DOM 中维护独立审批浮层。该方案不依赖 Studio 私有组件，是本次选择。
3. 自建完整 Agent Chat UI。官方模式成熟，但会形成第二个页面，不能满足“停留在当前 Studio”的目标。

选择方案 2。扩展只把 Studio 当作承载页面和 thread 导航，不读取 React 内部状态，也不模拟 Studio
按钮点击。

## 4. 组件与边界

实现保持为无构建链的原生扩展，放在 `showcases/studio_hitl_extension/`。最小组件为：

- `manifest.json`：声明 Manifest V3、Studio content script、本机 Agent Server host permission 和
  background service worker。
- content script：观察 SPA URL、维护当前 thread、请求 background 读取状态、挂载 Shadow DOM、
  渲染表单并收集决策。
- background service worker：只代理到固定 `http://127.0.0.1:8089` 的 state、runs 和 resume 请求，
  不保存业务数据。
- 纯逻辑模块：解析标准 HITL、构造字段模型、保持值类型、生成 decision、比较 interrupt identity；
  供 content script 和 Node 自检复用。
- 离线 HITL showcase Graph：不调用模型、Tool、Memory 或 Provider，只产生确定性标准 HITL payload，
  用于 Chrome 联调。

Shadow DOM 隔离扩展样式，避免影响 Studio CSS。Tool 名称、description、参数键和值全部通过 DOM
`textContent`/表单属性渲染，不使用 `innerHTML`。

## 5. 数据流

1. content script 仅在 Studio thread 路由激活，从 URL 解析 `baseUrl` 与 `thread_id`。
2. 若 `baseUrl` 不是精确允许的本机地址，扩展保持静默。
3. thread 页面且标签页可见时，每 1 秒轮询 `GET /threads/{thread_id}/state`；标签页不可见时每 5 秒一次，
   离开 thread 路由后停止。
4. 从顶层 `interrupts` 以及 task 的 `interrupts` 中提取标准 HITL payload。第一版只接管恰好一个 native
   interrupt；该 interrupt 内可以有多个 `action_requests`。零个、多个或未知 payload 均不渲染。
5. interrupt identity 定义为当前 checkpoint ID 与 native interrupt ID 的组合。发现新 identity 后显示浮层；
   相同 identity 不重复重置用户正在编辑的内容。缺失任一 ID 时不接管。
6. 用户为每个 action 选择批准、编辑或拒绝。多 action 必须全部决定后才能提交。
7. 提交前重新读取 thread state，并比较 interrupt identity；不一致则丢弃本地草稿、提示状态已变化并
   重新渲染。
8. 通过 `GET /threads/{thread_id}/runs?status=interrupted` 取得与当前 checkpoint 对应的最新
   `assistant_id`，然后 `POST /threads/{thread_id}/runs`，请求体使用
   `command.resume={"decisions": [...]}` 与 `multitask_strategy="reject"`。decision 顺序严格等于
   `action_requests` 顺序。
9. 提交期间禁用全部操作，resume 请求超时为 10 秒且不重试。服务端接受新 run 后，浮层转为非阻塞状态条；
   扩展轮询该 run，终态或下一次 interrupt 出现后重新加载当前 Studio thread 页面一次，使 Trace 与
   checkpoint 使用服务端最新事实。

扩展不创建自定义 resume endpoint，不更新 thread state，不绕过 Agent Server auth，也不直接执行 Tool。

## 6. 审批表单

每个 action 卡片显示 Tool 名称、可选 description、原始参数和当前决策。字段渲染优先使用
`review_configs[].args_schema`；当前上游没有提供 schema 时，根据 `action_requests[].args` 的现有 JSON
值递归构造控件：

- string：单行输入或多行文本框；
- number：数字输入，提交时保持 number；
- boolean：复选框；
- array：按索引递归渲染；
- object：按键递归分组；
- null 或无法确定类型的值：明确标记为类型未知，仅允许保持原值或使用高级 JSON 兜底编辑。

表单只允许编辑 args，不允许改变 action name。修改后的字段需有可见标记；提交 edit 时构造：

```json
{
  "type": "edit",
  "edited_action": {
    "name": "原 action name",
    "args": {}
  }
}
```

批准构造 `{"type":"approve"}`。拒绝要求填写非空原因并构造
`{"type":"reject","message":"..."}`。只显示对应 `allowed_decisions` 明确允许的操作；若配置没有任何
已支持决策，扩展不接管该 interrupt。

## 7. 安全与失败语义

- host permission 只包含托管 Studio 与 `127.0.0.1:8089`，不接受用户输入的任意远端地址。
- 扩展不请求、记录或持久化 API Key、Provider 凭据和 Provider 原始响应。background 从公开 state API
  收到完整响应后只解析并向 content script 投影 checkpoint 与 interrupt，不解析、传递或保存 messages。
- action payload 视为不受信数据；禁止 HTML 注入、动态代码执行和 `eval`。
- 网络中断、超时、`409`、`422`、未知响应、找不到 interrupted run 或 stale interrupt 均 fail closed。
- resume 请求不自动重试；提交按钮在单次请求完成前保持禁用，避免用户重复批准副作用。
- 未识别的 payload、非允许 decision 或字段类型转换失败时保留 Studio 原始 HITL 界面，并显示有界错误，
  不猜测数据。
- 扩展关闭、卸载或自身异常不影响 Studio 与 Agent Server；用户仍可使用原 JSON 流程恢复。

## 8. 验证策略

本次不改变 `tests/core/INVARIANTS.md` 中任何 core invariant，也不新增永久 pytest。

纯逻辑使用 Node 标准库的一个可运行检查覆盖：

- 从顶层和 task interrupts 解析标准 HITL；
- 拒绝未知 payload；
- string、number、boolean、array、object 的递归字段模型与类型保持；
- approve、edit、reject payload；
- 多 action decision 顺序；
- interrupt identity 和 stale 检测。

浏览器联调使用完全离线的 showcase Graph，在唯一 `8089` 开发服务上验证：

1. 普通 state 不显示浮层；
2. HITL 自动显示，包含嵌套参数；
3. approve 后同一 thread 继续；
4. edit 后 Graph 收到保持类型的修改参数；
5. reject 后 Graph 收到拒绝原因；
6. 多 action 一次提交且顺序正确；
7. 模拟 stale state 和服务端失败时没有 resume；
8. Studio Trace 在恢复后显示最新 checkpoint。

全部验证使用 mock/local/offline，不调用真实 Provider。

## 9. 文档与交付

扩展目录提供简短 README，说明：

- Chrome `chrome://extensions` 中启用开发者模式并“加载已解压的扩展程序”；
- 如何使用现有 `scripts/run_server.py` 与 showcase config 在唯一 `8089` 启动；
- 如何触发离线 HITL showcase；
- 如何卸载，以及原生 Studio JSON 始终可作为兜底。

生产 Agent 的 Tool、HITL、Agent Server 和 auth authority 只需复核，不因纯开发扩展机械修改。若实现时
发现必须改变生产协议或服务端 API，本设计失效，应停止并重新确认范围。
