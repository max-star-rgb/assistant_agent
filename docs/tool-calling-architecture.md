# LangChain-native Tool 与扩展架构

最后更新：2026-08-19

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产主链 Tool schema、执行、HITL、MCP 与 Provider-native 能力权威 |
| Owns | `BaseTool`、`ToolRuntime` 注入、ToolNode、effect metadata、官方 MCP 装配、Tool middleware |
| Does not own | 父图路由、Memory、Provider HTTP wire、媒体 WebSocket、后台感知和 durable 状态机 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/tools.py`、`src/assistant_agent/tools/`、`src/assistant_agent/mcp/` |
| 验证入口 | `docs/authority.toml` 中 `tool-calling.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

## 生产边界

生产 Agent 的硬边界是 LangChain 标准 Tool，不再是
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。受信 composition 显式列出内建 Plugin，构造既有
进程内 Tool。所有内建 Tool 都由官方 `@tool` 工厂创建为标准 `BaseTool`，不使用项目自定义的具体
`BaseTool` 子类，也不存在动态生成的项目 Tool schema 层。新主链不做文件扫描、动态 Python module
discovery 或 Registry lookup。

进程 composition 仍把完整静态 `BaseTool` inventory 注册给 `create_agent` / `ToolNode`，但完整注册不等于每次
模型调用全部可见。`ProgressiveToolExposureMiddleware` 在原生 `wrap_model_call` 中按受信 Skill manifest 和
fast agent 子图当前执行的 state/checkpoint namespace 内 `active_skill_ids` 缩小 `ModelRequest.tools`：
未加载 Skill 时，其 `governed_tools` schema 不发给
模型；成功执行 `load_skill` 后，middleware 把原 `ToolMessage` 与窄 Skill/reference grant 一起写入标准
`Command(update=...)`，下一次模型调用才暴露对应 Tool。Skill/reference channel 不进入父图或 Memory
Graph。Tool 名只从 `skill.toml` 重新解析，不接受模型或 Tool
artifact 声明任意 grant；该可见性层不替代具体 Tool 的身份、授权、参数和副作用校验。

媒体 Tool 使用另一条与 Skill 正交的条件暴露链。`ConditionalToolExposureMiddleware` 只过滤已经静态注册在
`request.tools` 中的 Tool，并按 Tool metadata 的封闭 `availability` 枚举读取可信运行事实：
`uploaded_media_inspect` 要求最新用户消息含 `source=uploaded` 的图片或视频；`live_view_inspect` 要求当前
WebSocket 已成功完成 `callType=VIDEO` 的 control 握手；`visual_memory_search` 还要求当前
`user/thread/as-of sequence` 已存在可检索视觉文本。它不读取 `active_skill_ids` 或
`skill_reference_grants`，不根据用户关键词推断意图，探针异常时 fail closed。Tool 自身在执行时再次校验
握手、媒体来源和身份边界，避免绕过模型可见性直接调用。

每个内建 Tool：

- 是官方 `@tool` factory 返回的标准 `BaseTool`；函数签名中的
  `ToolRuntime[AssistantRunContext]` 是隐藏参数，LangChain/LangGraph 官方注入会直接将其从模型可见
  `tool_call_schema` 排除，不经项目生成 execution schema 或剥离字段；
- `ToolNode` 注入的 runtime 提供当前 state、thread/run、Store 和 `server_info`；受信用户身份只读取
  `server_info.user.identity`，不从 Runtime Context 复制；
- 成功时统一 native boundary 把有界模型投影 JSON 序列化为一个标准 text content block，并把完整
  结构化业务结果作为 artifact，`response_format="content_and_artifact"` 返回这组标准二元值；LangChain
  据此原生构造 `ToolMessage(content, artifact)`。该边界不定义项目私有 Tool 输出或 UI 渲染协议；失败
  抛出 `ToolException`；
- metadata 至少声明 `effect=read|generate|write|dangerous` 与 `source=builtin|mcp`。

`uploaded_media_inspect`、`live_view_inspect` 和 `visual_memory_search` 都由原生函数 Tool 工厂构造；复杂逻辑
保留在普通 service 对象中，不再通过视觉 Tool 之间的 Python 继承共享字段。上传图片与用户主动上传视频
复用 `VisualPerceptionModule` 持有的进程级 VLM client；摄像头实时视频仍由后台视觉观察链处理。

只读 Tool 由 `ToolRetryMiddleware` 做有界重试，重试耗尽后产生 error `ToolMessage`；非 read 内建 Tool
使用官方 `BaseTool.handle_tool_error` 把 native boundary 抛出的 `ToolException` 转为同类 error
`ToolMessage`，因此无需替换默认 `ToolNode`。native boundary 会先脱敏未知异常，领域 Request 校验也在
该边界内成为有界、可解释的 `ToolException`。fast 模式不触发 HITL；planning 模式的非 read Tool 由
`HumanInTheLoopMiddleware` 在执行前产生原生 interrupt。schema、身份与授权仍由具体 Tool/业务 adapter 校验；
外部副作用幂等属于具体 Tool 或业务 API，主链不再维护通用 operation ledger。

Tool 的 Provider adapter 先把外部响应规范化为业务 Pydantic result；Provider 原始响应不进入模型上下文或
`artifact`，需要审计时只保留受治理的引用。具体 Tool 再从完整业务 result 派生有界的模型投影：例如购物
Tool 的 `artifact` 保留全部规范化 `ShoppingSearchResult`，`content` 按每个 need 保留状态、选中商品、
购买链接、单价/小计与至多两个备选。两者是同一业务结果的完整视图与模型视图，不是两份相同 payload。

`image_generation` 沿用同一原生双通道，不创建第二条 UI 消息。Provider adapter 返回远程图片地址后，Tool
先把图片下载到受管本地目录，再令 `content` 只包含成功状态与 `image_id`，令
`artifact.images[]` 按图片保存 `image_id`、受管 `output_ref`、`mime_type`，以及配置公开 origin 时才出现的
绝对 `url`。Provider 临时地址不进入模型观察；Studio 不负责渲染 artifact，媒体等程序入口按自身协议读取它。

## MCP 与 Plugin

本地与 MCP Tool 通过一个 native inventory 一次完成装配。外部 MCP 只通过官方 `MultiServerMCPClient`
发现和执行；受信 `MCPServerConfig` 机械转换为 stdio connection，发现后
应用显式 allowlist、read-only effect 和 `<namespace>_<server>_<tool>` 命名。主链不建立 MCP proxy、ToolSpec
镜像、plugin-private runner 或 Registry。旧 `personal_assistant_tools` / `email_tools` 远端映射已删除，MCP
能力直接使用官方 adapter 生成的标准 Tool。MCP tool discovery 属于 worker 进程 composition，只执行一次；schema、history、state 与
run 复用同一个 compiled graph 和 Tool 集合，实际 MCP Tool 调用仍遵循官方按调用创建 session 的行为。

Skill manifest 的 `governed_tools` 使用上述最终 namespace 名，因此 MCP Tool 与本地 Tool 使用同一渐进暴露机制。
未被任何 Skill manifest 声明为 `governed_tools` 的 allowlisted MCP Tool 默认对模型可见；当前高德
`maps_weather` 以只读 `mcp_amap_maps_maps_weather` 默认暴露，天气实时事实优先由该结构化 Tool 获取。

MCP 只有一份 `MCPServerConfig` 文件 schema：根对象包含 `servers` 列表，每个 server 只声明官方 adapter
connection、显式 Tool allowlist、read-only 集合与 namespace。MCP 未启用时不读取配置；显式启用后，配置文件
缺失、JSON/schema 非法或出现遗留字段时 composition 立即以脱敏错误失败，不静默跳过 server，也不兼容旧的
顶层数组、`email_tools`、`personal_assistant_tools` 或 server-local `timeout_seconds`。

本地 Plugin 只复用纯构造逻辑和 Provider adapter，生产装配清单是代码中的显式列表。旧 Tool CLI、动态
loader、Registry/Executor、离线 MCP server 与 Skill runtime 已删除。两个本地日历 system eval 也通过最小
StateGraph 的标准 `ToolNode` 执行真实 Tool。durable task 不伪造 Agent run：它使用显式工具名/effect allowlist
和窄业务 adapter，缺失 effect metadata 时按可能写入 fail closed。

## Provider-native 能力

Qwen 等模型原生联网参数属于 `BaseChatModel` 请求能力，不伪装成本地 Tool。real 模式选择 qwen 且
`QWEN_CHAT_API_PROTOCOL=dashscope` 时，生产主链构造实现标准 LangChain `BaseChatModel` 的
`DashScopeNativeChatModel`，使用官方 text-generation Generation API；不得静默回退到 OpenAI-compatible。
显式选择 `openai_compatible` 时才构造 `ChatOpenAI`。

`QWEN_CHAT_ENABLE_SEARCH=true` 时原生请求设置 `enable_search=true`，默认使用 turbo、
`forced_search=false`，同时设置 `enable_source=true`、`enable_citation=true` 和 `citation_format=[<number>]`。
只有显式结构化 `provider_search_profile=deep_research` 才使用 max 与 forced search，不从用户文本关键词推断。
每次模型回复的来源清洗后把审计副本保存在该 `AIMessage.response_metadata.provider_search_sources`；本地 Tool 和
ToolNode 不另行接管或聚合这些来源。Provider 返回的 `message.content` 是唯一正文：adapter 不把
`search_info.search_results` 投影为 Citation annotations，不追加来源列表，也不改写 Provider 已插入的
`[1]` 角标。流式响应只在 terminal chunk 写入稳定 response metadata、usage 和来源，避免标准 chunk 合并把
标量 metadata 重复拼接；每个正文 chunk 始终把 Provider 文本交给 LangChain 原生消息序列化。生产装配显式使用
LangChain 原生 `output_version="v1"`，由框架把正文和 ToolCall 统一序列化为标准 content block；Studio 只需
原生显示正文，自定义 UI 或媒体入口可按正文角标与 `provider_search_sources[].index` 自行渲染可点击来源。
`finish_reason=tool_calls` 的中间消息同样只保留标准正文与 ToolCall，搜索来源仅留在 response metadata，不插入聊天内容。DashScope
ToolCall 的 Provider-local index 会映射到标准 content block 的全局 index：正文为 0，ToolCall 从 1 开始，
避免流式合并时把 ToolCall 字段并入正文 block。原生 adapter 按官方
message/tool_call/tool_call_id 结构与标准
LangChain ToolCall 双向转换，流式 SSE 同时保留 usage、finish reason 与终态来源。是否调用候选本地 Tool 仍由
模型按标准 tool calling 协议决定。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_extension_contract.py
```
