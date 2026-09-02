# LangChain-native Tool 与扩展架构

最后更新：2026-09-02

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 统一生产 Agent 的 Tool schema、filesystem、执行、HITL、MCP 与 Provider-native 能力权威 |
| Owns | `BaseTool`、`ToolRuntime` 注入、`ToolNode`、角色 Tool 装配、Deep Agents filesystem、官方 MCP 与 Tool middleware |
| Does not own | 主图拓扑、Memory、Provider HTTP wire、媒体 WebSocket、后台感知和 durable 状态机 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/assistant_agent.py`、`native_agent/tools.py`、`runtime/local_backend.py`、`runtime/thread_resources.py`、`tools/`、`mcp/` |
| 验证入口 | `docs/authority.toml` 中 `tool-calling.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Durable task 见 [`durable-task-architecture.md`](durable-task-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md)；视觉能力见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 统一 Tool surface

生产边界是 LangChain 标准 `BaseTool -> ToolNode`。受信 composition 静态装配业务 Tool、官方 MCP Tool 和
Deep Agents middleware；不使用项目自建 Registry/Executor、动态 Python discovery 或第二套 Tool schema。
`AssistantAgent` 可在同一个模型循环中直接回答，或使用 `write_todos`、同步只读 `task`、业务 Tool、本机 filesystem
filesystem 与 `execute`。

主 Agent 把 working-directory backend 直接传给 `create_deep_agent`。因此主 filesystem Tool 由 Deep Agents
factory 自己拥有的 `FilesystemMiddleware` 注入，项目不再额外装配第二份 middleware 或重复注册文件 Tool。
`tests/tdd/unified-assistant-agent/test_unified_graph.py` 保留 Deep Agents #5388 的回归约束：主 Agent factory 持有
filesystem surface，项目 middleware 列表不再重复创建它，且 `filesystem` Profile 包含 `execute`。

当前 composition 的 backend 为：

- 主 Agent：`CompositeBackend` 默认 route 按 `AssistantRunContext.cwd` 动态创建 `LocalShellBackend`；`.` 映射到 cwd，
  `/`、`/.` 和其他绝对路径保持宿主 OS 语义，不装配 `/artifacts/`、`/scratch/`、`/uploads/` 等虚拟 route；
- 同步/异步 worker：复用主 Agent 的 working-directory backend 和完整 filesystem/`execute` surface；同步继承 run context，异步显式复制父 context；
- Skill：同一 composite 额外挂载只用于稳定寻址的 `/source-skills/` 与 `/cwd-skills/`，分别映射源码 `skills/` 与当前
  `<cwd>/skills/`；`SkillsMiddleware` 用它们发现目录，filesystem `read_file` 用同一路径按需读取正文。

`general-purpose` 使用与主 Agent 相同的模型和业务 Tool inventory，并拒绝业务 Tool 伪装成 filesystem、Todo、task、
async task 或 profile activation 等保留名称。`coder` 只接收 Deep Agents
filesystem 与 `execute`；`browser-operator` 只接收 Playwright Tool。角色 Tool 集、backend 和 `interrupt_on` 是实际
授权边界，Tool description、Skill、Profile 与 task 文本均不能扩权。

## Tool schema、可见性与结果

17 个内建业务 Tool（calendar/contacts、email、`file_read`、web、lodging 与价格订阅、shopping、图片搜索、
图片生成、图生 3D 和四个媒体 Tool）均由官方 `@tool` factory 返回标准 `BaseTool`。handler 直接接收
`ToolRuntime[AssistantRunContext]`，它是框架注入的隐藏参数，不进入模型可见 schema；认证 identity 只从
`server_info.user.identity` 读取。handler 成功直接返回 `content_and_artifact` 的 `(content, artifact)`，预期或
清洗后的领域失败抛出 `ToolException`。Tool metadata 只保存 `source=builtin|deepagents|mcp`、availability 等观测或
条件暴露事实，不参与授权、重试或 HITL。

成功结果使用标准 `ToolMessage(content, artifact)`：`content` 是有界模型投影，`artifact` 保存结构化业务结果；
失败由 `ToolException` 或 `handle_tool_error` 转为可解释 error `ToolMessage`。Provider 原始响应、secret 和宿主路径
不进入模型上下文或 artifact。`image_generation` 成功后复用已经落盘且经过大小、路径和媒体类型校验的生成物，
在文本 observation 后追加标准 `image` content block；结构化图片元数据继续保存在同一 ToolMessage 的 artifact。
高德路线 MCP interceptor 在保留模型可见导航 Markdown 的同时，把同一受校验 URL 写入 namespaced
`structuredContent`；终端入口只从转换后的标准 ToolMessage artifact 确定性交付，不解析模型正文。

需要确定性交付时，Tool 在自身 artifact 中写入严格、有界的
`assistant_agent_delivery_v1={text, output_refs}`。购物、酒店和图片生成只在领域结果完成校验后写入；
全局 MCP interceptor 先删除外部结果中的同名保留字段，仅在成功校验 AMap 路线后重建并写入
`structuredContent`。这是 `ToolMessage.artifact` 的应用数据，不是自定义
message/state 协议。传输层只通用读取该 key 和标准 `type=file` block，不依赖模型复述，也不按 Tool 名解析业务 artifact。
只读 Tool 使用官方 `ToolRetryMiddleware` 有界重试；`live_view_inspect` 为避免重复
当前画面推理不进入自动 retry 清单。

`ToolContext`、`invoke_native_tool` 和以 `ToolResult` 为中心的生产兼容执行链均已删除。`ToolResult` 类型仅由
历史兼容切面 `runtime/state.py` 保存，不能作为新 Tool 的输入、执行或结果边界。

`ToolProfileMiddleware` 先用当前 Graph 的真实 Tool inventory 裁剪受信静态 catalog，只保留非空 Profile 和其中
实际注册的 Tool；没有可用 Profile 时不暴露 `activate_tool_profile`。模型调用
`activate_tool_profile` 后，middleware 从私有 `active_tool_profile_ids` 缩小后续 `ModelRequest.tools`，并在执行
边界复核历史消息中的 ToolCall；激活结果的 `activated_tool_names` 返回该运行角色实际注册并开放的 Tool 名称。
至少一个 Profile 激活后，模型可自主调用 `deactivate_tool_profile` 精确隐藏指定 Profile 的实际 Tool 集；重复取消
幂等成功，其他 Profile 和常驻 Tool 不受影响，取消后的历史 ToolCall 仍在执行边界被拒绝。没有激活 Profile 时不向模型
暴露取消 Tool；取消只影响后续可见性，不中止已开始的 Tool，也不撤销既有副作用。
Skill、Todo、worker 文本和 artifact 都不能声明 profile 或授权 Tool。

Git 使用独立 `git` Tool Profile。Tool 接收 `target_path` 与不含可执行文件名的参数数组，先对目标路径执行
`git -C <path> rev-parse --show-toplevel`，再以 `shell=False` 在解析出的仓库根执行命令。main、通用 worker 与
coder 共用该 middleware；`execute` 的可见描述要求优先使用 `git` Tool，并在调用边界拒绝直接或 shell 链中的
Git CLI，返回 `use_git_tool` 错误，不启动 shell。

媒体 Tool 的条件暴露与 Skill/Profile 正交：`uploaded_media_inspect` 依赖受信 uploaded media block；
`live_view_inspect` 依赖本轮冻结 target；`visual_memory_search` 还要求当前 thread 有可检索文本；
`visual_reminder_manage` 要求连接已经收到并绑定有效视频帧。条件来自入口签发的 capability 与服务端事实，不读取
用户关键词；Tool handler 会再次校验身份、thread 和冻结边界。当前 media custom route 不注入
`source=live_camera` message block，因此不会满足这三个实时 Tool 的条件；详见视觉 authority。

## 统一 HITL 与 LocalShell 边界

所有显式列入 `interrupt_on` 的操作在 handler 前统一经过 Deep Agents `HumanInTheLoopMiddleware`：

- filesystem 的 `write_file`、`edit_file`、`delete`、`execute`；
- `git` Tool；
- composition 显式列入 `interrupt_tool_names` 的业务与 MCP Tool；
- `start_async_task`、`update_async_task` 与 `cancel_async_task`。

`AssistantRunContext.require_tool_approval` 默认为 true；在 Studio 保存为 false 的 Assistant 通过原生 `when`
谓词跳过上述全部 interrupt，Tool 直接进入执行链，不需要自动提交 approve/resume。
未列入 `interrupt_on` 的 Tool 按 Deep Agents 原生默认直接执行。审批、interrupt、checkpoint 与 resume 使用 LangGraph/Agent Server 原生协议；外部副作用幂等
仍属于具体 Tool 或业务 API。HITL 只是治理，不提供进程隔离。批准 `execute` 等价于允许 Agent Server 的 OS identity
在当前 run cwd 下执行完整 command；该命令可能访问宿主路径、网络和 Git。filesystem Tool 与 shell 共享
Agent Server OS identity 的权限边界。当前 backend 仅适合受信本地个人 Agent，多租户或不可信
生产必须替换为 container 或 remote sandbox backend。

## MCP、Plugin 与异步任务

本地和 MCP Tool 进入同一 native inventory。外部 MCP 只通过官方 `MultiServerMCPClient`，配置必须提供显式
`allowed_tools`、`general_purpose_tools`、`interrupt_tools` 与确定性 `<namespace>_<server>_<tool>` 命名。
`general_purpose_tools` 只标记可安全自动重试的查询 Tool，不限制 worker 的 Tool surface；`interrupt_tools` 是原生 HITL
名单，两者互不推导；未列入
`interrupt_tools` 的 MCP Tool 默认执行。
生产不建立 MCP proxy、ToolSpec 镜像、plugin-private runner 或旧远端 Tool 映射。
浏览器能力只由固定版本的官方 Playwright MCP 提供；项目不再维护内建 Playwright backend。MCP discovery 成功后
`mcp_playwright_browser_*` 才进入 `browser` Profile 和 `browser-operator`；是否审批由配置中的具体 Tool 名决定，
不从通用读写分类推断。配置为 `session_scope=thread` 的 server 按
`identity + thread_id + server_name` 复用官方 `ClientSession`；Playwright 进程 cwd 与输出目录都直接使用首次调用的
`AssistantRunContext.cwd`，不创建 browser workspace、scratch 或 artifact 映射。`browser-operator` 只持有原生
Playwright Tool，不装配 filesystem middleware。截图预览沿用 Playwright MCP 原生图片结果与 LangChain 标准
`image` content block；需要预览时不传 `filename`，固定文件名只用于用户明确要求的落盘场景。配置为 `call` 的 server 保持官方逐调用生命周期；空闲 thread session
按 TTL 关闭，进程关闭时统一释放剩余 session。

Deep Agents `AsyncSubAgentMiddleware` 的 `start/check/update/cancel/list_async_task(s)` 归入 `async-tasks` Profile。
start、update 与 cancel 显式进入 HITL；check/list 未列入 `interrupt_on`，按原生默认执行。start 创建 `assistant-worker-v2` thread/run 时
只保留父子 thread/run correlation。start/update 的 loopback SDK 请求还携带进程内 internal
capability；只有 auth 转换出的 worker permission 配合严格 metadata 才能创建 worker run，外部 shape-only 请求被
拒绝。该 capability 不进入 Tool 参数、state、task handle 或 thread/run metadata。worker 不能递归 delegation。

## Provider-native 能力

Qwen 的原生联网参数属于 `BaseChatModel` 请求能力，不伪装成本地 Tool；生产不注册本地 `web_search` Tool。
只有 real Qwen、DashScope 协议和原生搜索开关同时启用时，统一 dynamic prompt 才向模型说明本次调用具备原生联网搜索；
未启用时不注入搜索提示，也不提供本地搜索替代。real 模式选择 DashScope 协议时使用
`DashScopeNativeChatModel`；显式选择 OpenAI-compatible 时才使用 `ChatOpenAI`，不得静默回退。统一主 Agent 可按
结构化配置启用 Provider search；worker 复用主 Agent 的同一模型配置。主 Agent 使用官方 `ModelRetryMiddleware`
对 DashScope 模型失败重试一次，耗尽后由本地固定 `AIMessage` 结束本轮，不再依赖模型生成故障说明。

Provider 返回的 message content 是唯一正文，来源只保存在同一 `AIMessage.response_metadata`。adapter 不把来源
改写为 ToolMessage 或追加来源列表；媒体和自定义 UI 可按正文角标与结构化来源自行投影。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_extension_contract.py \
  tests/tdd/unified-assistant-agent
```
