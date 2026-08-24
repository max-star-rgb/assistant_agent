# LangChain-native Tool 与扩展架构

最后更新：2026-08-24

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产主链 Tool schema、执行、HITL、MCP 与 Provider-native 能力权威 |
| Owns | `BaseTool`、`ToolRuntime` 注入、ToolNode、effect metadata、官方 MCP 装配、Tool middleware |
| Does not own | 父图路由、Memory、Provider HTTP wire、媒体 WebSocket、后台感知和 durable 状态机 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/tools.py`、`src/assistant_agent/tools/`、`src/assistant_agent/mcp/` |
| 验证入口 | `docs/authority.toml` 中 `tool-calling.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md)；视觉能力见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 生产边界

生产 Agent 的硬边界是 LangChain 标准 Tool，不再是
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。受信 composition 显式列出内建 Plugin，构造既有
进程内 Tool。所有内建 Tool 都由官方 `@tool` 工厂创建为标准 `BaseTool`，不使用项目自定义的具体
`BaseTool` 子类，也不存在动态生成的项目 Tool schema 层。新主链不做文件扫描、动态 Python module
discovery 或 Registry lookup。

进程 composition 仍把完整静态 `BaseTool` inventory 注册给 `create_agent` / `ToolNode`，但完整注册不等于每次
模型调用全部可见。`ProgressiveToolExposureMiddleware` 只使用原生 `wrap_model_call`，按受信 Skill manifest 和
fast agent 子图当前执行的 state/checkpoint namespace 内 `active_skill_ids` 缩小 `ModelRequest.tools`：
未加载 Skill 时，其 `governed_tools` schema 不发给
模型；它不处理 `load_skill` 的 Tool 调用或结果。`load_skill` 自身只加载指导正文与 reference，并按 LangGraph
原生 Tool state-update 契约直接返回包含标准 `ToolMessage` 的 `Command(update=...)`，由既有 `ToolNode` 把
`active_skill_ids` 与窄 `skill_reference_grants` 写入 state；Tool observation/artifact 不返回 Tool 名或 capability
grant。下一次模型调用时，独立 exposure middleware 才从 state 与 composition 注入的受信 catalog 机械派生可见
Tool schema。Skill/reference channel 可在 planning agent 与 task 子 Agent 间通过标准 state reducer 合并，但不进入
`AssistantRootGraph` 或 Memory Graph。Tool 名只从 `skill.toml` 重新解析，不接受模型或 Tool
artifact 声明任意 grant；该可见性层不替代具体 Tool 的身份、授权、参数和副作用校验。fast 在 Skill 加载后获得对应
业务 Tool schema。planning coordinator 是另一个官方 `create_agent`，其 Tool inventory 只来自
`TodoListMiddleware.write_todos` 与 Deep Agents `SubAgentMiddleware.task`；它不注册 Skill control 或业务 Tool。
`task` 是进入内置 `ToolNode` 的真实 `StructuredTool`，内部调用预编译 `AssistantFastAgent`。Supervisor 固定关闭
Provider-native search，Skill 加载、业务联网与执行只发生在 task 子 Agent。

成功的 `load_skill` 继续通过标准 `Command(update=...)` 写入受信 `active_skill_ids` 和 reference grant；成功
`load_skill_reference` 的标准 `ToolMessage` 表明本轮实际读取的 reference。task 子 Agent 复用同一个
`AssistantFastAgent`，渐进暴露 middleware 仍按受信 Skill state决定业务 Tool schema；默认业务 Tool 仍按静态
inventory 可见。Deep Agents 只通过标准 state update 把实际生成的 Skill/reference state 合并回 planning agent，
不建立 authorization envelope、capability catalog 或节点 Tool allowlist；Todo、子 Agent 文本或 Tool artifact 都不能
声明新 grant。

媒体 Tool 使用另一条与 Skill 正交的条件暴露链。`ConditionalToolExposureMiddleware` 只过滤已经静态注册在
`request.tools` 中的 Tool，并按 Tool metadata 的封闭 `availability` 枚举读取可信运行事实：
`uploaded_media_inspect` 要求最新用户消息含 `source=uploaded` 的图片或视频；`live_view_inspect` 要求当前
WebSocket 已成功完成 `callType=VIDEO` 的 control 握手，且本轮冻结投影已经包含 selector 选中的目标关键帧；
`visual_memory_search` 还要求当前
`user/thread/as-of sequence` 已存在可检索视觉文本；`visual_reminder_manage` 要求媒体侧至少已有一个视频包
成功解码并绑定到该连接，不能只靠 VIDEO 握手出现。它不读取 `active_skill_ids` 或
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

`uploaded_media_inspect`、`live_view_inspect`、`visual_memory_search` 和 `visual_reminder_manage` 都由原生
函数 Tool 工厂构造；Tool
层只负责标准执行与可信运行事实注入，视觉算法和资源复用全部由视觉 authority 负责。
`live_view_inspect` 的模型可见 content 固定为
`window[{sequence, captured_at}] + vlm_response`，其中北京时间由 Tool owner 从受信媒体时间机械格式化；
完整视觉状态与诊断只属于 artifact、contract 与 trace，不重复投影给主模型。

只读 Tool 由 `ToolRetryMiddleware` 做有界重试，重试耗尽后产生 error `ToolMessage`；非 read 内建 Tool
使用官方 `BaseTool.handle_tool_error` 把 native boundary 抛出的 `ToolException` 转为同类 error
`ToolMessage`，因此无需替换默认 `ToolNode`。native boundary 会先脱敏未知异常，领域 Request 校验也在
该边界内成为有界、可解释的 `ToolException`。fast 模式不触发 HITL；planning 模式的非 read Tool 由
`HumanInTheLoopMiddleware` 在执行前产生原生 interrupt。schema、身份与授权仍由具体 Tool/业务 adapter 校验；
外部副作用幂等属于具体 Tool 或业务 API，主链不再维护通用 operation ledger。
`live_view_inspect` 为避免重复当前画面调用而不进入 read retry 清单，但仍单独启用同一个
`BaseTool.handle_tool_error` 边界；关键帧 capability 在暴露后失效时只返回一次 error `ToolMessage`。

fast `create_agent` 使用官方 `ModelCallLimitMiddleware` 与 `ToolCallLimitMiddleware` 提供 per-invocation
安全上限；planning coordinator 也独立使用官方 limit middleware，不写 usage、reservation 或 recovery ledger。
需要独立 run 上限的业务 Tool 在受信静态
metadata 中声明正整数 `run_call_limit`；composition 从全部 Tool 机械构造唯一
`PerToolCallLimitMiddleware.after_model` 节点，由一个 Tool-name→limit 映射分别计数并为超限调用生成标准 error
`ToolMessage`，不执行被阻止的 Tool，未配置 Tool 原样放行。该策略不读取用户文本、模型参数或运行时 artifact，
也不是全 inventory limiter。当前 `live_view_inspect` 声明 `run_call_limit=1`，避免同一个 run 重复执行昂贵实时观察；
后续其他 Tool 可通过相同 metadata 进入同一通用节点，fast agent 装配不识别具体 Tool 名。

fast agent 的最内层 `ToolProgressMiddleware` 使用官方 `ToolRuntime.stream_writer` 向原生 custom stream 发出
每次逻辑 Tool 调用的 `started` 和 `completed|failed` 生命周期。事件只携带 `type=tool_progress`、标准
Tool name 与 tool call ID，不携带模型提交的参数、ToolMessage content、artifact 或异常正文。middleware 位于
retry 与 HITL 内层：审批完成后才发出 started，同一次有界 retry 只对外形成一组逻辑生命周期。媒体入口不订阅
custom；需要该生命周期的 Agent Server SDK/API 消费者需要显式选择 custom stream mode。Studio 的 graph/trace
调试可见性不构成任意 custom payload 的通用渲染承诺，完整协议核验以 SDK/API 订阅为准。

Tool 的 Provider adapter 先把外部响应规范化为业务 Pydantic result；Provider 原始响应不进入模型上下文或
`artifact`，需要审计时只保留受治理的引用。具体 Tool 再从完整业务 result 派生有界的模型投影：例如购物
Tool 的 `artifact` 保留全部规范化 `ShoppingSearchResult`，`content` 按每个 need 保留状态、选中商品、
购买链接、单价/小计与至多两个备选。两者是同一业务结果的完整视图与模型视图，不是两份相同 payload。
媒体入口需要兼容购物卡片时，只从当前轮成功 `shopping_search` 的标准 `ToolMessage.artifact` 确定性生成
`<detail>/<link>/<pic>` 文本块；模型正文和 Tool `content` 不负责拼接该 wire 协议。

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
production composition 在构造 inventory 前只加载一次 repo `SkillCatalog`，并把同一实例显式注入
`SkillLoadingPlugin` 与 fast agent；planning coordinator 只引用已经编译的 fast agent，不再读取 Skill catalog。
Skill loading plugin 不在 production inventory 构造时
再次读盘。只有直接省略 catalog 的非 production fixture 保留 plugin 自行加载的兼容行为。
高德 `amap_maps` 的驾车、公交、骑行和步行路线调用通过官方 MCP adapter 的 `tool_interceptors`
扩展点，在成功结果中追加由受信起终点坐标确定性生成的高德 HTTPS 路线规划链接；链接不包含 API Key，
失败结果、非法坐标和其他 MCP Tool 保持原样。最终答复原样保留该 Markdown 链接；移动端可尝试调起
高德 App，其他终端使用高德 H5 页面。

Skill manifest 的 `governed_tools` 使用上述最终 namespace 名，因此 MCP Tool 与本地 Tool 使用同一渐进暴露机制。
未被任何 Skill manifest 声明为 `governed_tools` 的 allowlisted MCP Tool 默认对模型可见；当前高德
`maps_weather` 以只读 `mcp_amap_maps_maps_weather` 默认暴露，天气实时事实优先由该结构化 Tool 获取。

MCP 只有一份 `MCPServerConfig` 文件 schema：根对象包含 `servers` 列表，每个 server 只声明官方 adapter
connection、显式 Tool allowlist、read-only 集合与 namespace。MCP 未启用时不读取配置；显式启用后，配置文件
缺失、JSON/schema 非法或出现遗留字段时 composition 立即以脱敏错误失败，不静默跳过 server，也不兼容旧的
顶层数组、`email_tools`、`personal_assistant_tools` 或 server-local `timeout_seconds`。
当前 Gmail MCP 由 operator 在 Agent Server 启动前通过 `google-workspace-mcp` optional extra 固定安装
`workspace-mcp==1.22.0`；运行配置直接调用该环境内的 `workspace-mcp` executable，不使用会在进程启动期间
解析或下载依赖的 `uvx`，避免 Graph 冷装配被依赖下载阻塞。

本地 Plugin 只复用纯构造逻辑和 Provider adapter，生产装配清单是代码中的显式列表。旧 Tool CLI、动态
loader、Registry/Executor、离线 MCP server 与 Skill runtime 已删除。两个本地日历 system eval 也通过最小
StateGraph 的标准 `ToolNode` 执行真实 Tool。durable task 不伪造 Agent run：它使用显式工具名/effect allowlist
和窄业务 adapter，缺失 effect metadata 时按可能写入 fail closed。

## AI Coding Tool 边界

coding 模式使用独立静态 Tool inventory：`coding_repo_list`、`coding_repo_search`、`coding_repo_read`、
`coding_repo_status`、`coding_repo_diff` 与 `coding_propose_patch`。前五项是 read Tool；proposal 是
`effect=generate`，只返回经受信 backend 验证的候选 patch artifact，不写文件。身份、thread 和 workspace
均由 `ToolRuntime` 与 Agent Server 事实解析，不进入模型 schema。

只读并行 analysis agent 精确复用前五项 read Tool，且每次调用只能通过 checkpoint 中的精确
`workspace_ref + base_commit` 对 process-owned service 执行只读认证 lookup，再访问同一 active snapshot；它不调用
会创建、续期、reap 或清理 workspace 的 resolve 路径。analysis inventory 不包含 proposal 或任何 mutation Tool，
其模型调用固定 `provider_search_profile=none`，即使普通主链显式启用了 Provider-native search 也不得带入分析阶段。
analysis Tool 的 retry 只覆盖明确 transient cause chain；身份、权限、snapshot、schema、路径和其他安全错误立即传播，
耗尽后的 transient 错误也传播到 worker 的受信失败分类，不能转换成供模型继续生成 succeeded result 的 error ToolMessage。

final coding review 使用独立的静态只读 profile，并精确复用上述五个 snapshot-bound read Tool；三个并行 reviewer
只能读取父 checkpoint 冻结的 final snapshot，固定 `provider_search_profile=none`。review profile 不包含
`coding_propose_patch`、shell、validation command、dependency/artifact/credential、commit/merge 或任何 mutation
Tool；review decision 是父 Graph 自有 interrupt，也不是 Tool。普通 reviewer/capability failure 只能形成 signed
`unavailable` result 并由 canonical report 派生 `unavailable`，不会触发 Tool 写入、自动 patch、自动 repair 或自动 approval。
pending review 的 Tool read 要求 active v2 immutable manifest；legacy、schema/digest mismatch、identity/permission 和
snapshot contract 错误 fail closed，不能由 read retry 转换为可继续的模型观察。completed report resume 是否允许
fresh snapshot rebind 由 runtime checkpoint contract 决定，不扩大 Tool 的 snapshot validation 或生命周期 ownership。

实际 patch apply 是 `AssistantCodingGraph` 的确定性节点，不注册为 Tool。coding inventory 不加入普通
fast/planning Agent；coding 不提供 shell、delete、commit、merge、push 或任意宿主路径访问。路径、symlink、
protected glob、UTF-8、大小、base commit、file digest 和 patch digest 均由 Tool/backend fail closed 校验。

阶段 2 的 test/lint/format/build 也不注册为模型 Tool。服务端 repository allowlist 把稳定 command ID 映射为
固定 argv、kind 和资源上限，并由 Graph 在已批准 patch apply 后按固定 sequence 调用；模型、客户端和消息都
不能提交 argv、cwd、env、shell syntax 或 command ID。test/lint/build 的 scratch 写入全部丢弃；format 的
增量 diff 只有重新通过 patch validator 和独立 HITL 后才写入 worktree。命令执行资源生命周期归 Agent Server
authority。repository 显式启用阶段 4A sandbox 后，这些固定命令迁入本地 digest-pinned Docker image，并只由
协议合规镜像内的固定 trusted runner 执行；runner 只承担只读输入复制、离线命令执行和有界结构化结果返回。
sandbox backend、Docker CLI、image、container ID、network、mount、environment 和资源参数都不是
`BaseTool`，也不进入模型 schema。sandbox 默认断网且不注入宿主秘密，任何失败禁止回退宿主执行。联网、
Stage 4B1/4B2 的 dependency intent、依赖审批、credential lease 审批、proxy/gateway/downloader 与离线 install
同样不是 Tool：模型、客户端和消息不能
提交 lockfile、package、registry、host、port、image、argv、proxy、network 或环境变量。intent 只来自 patch
changed paths 与 repository 静态 profile；验证容器仍断网。私有 registry 只支持 operator-owned profile、专用
环境变量 broker 和固定 gateway stdin loader，凭据不进入模型 schema、ToolRuntime、Graph state 或 downloader。
用户提交 secret、身份驱动 credential selection、其他 ecosystem 和通用 artifact 外部资源能力仍不存在。

阶段 3 的 controlled commit、merge preview 与 merge apply 同样不是模型 Tool。模型和客户端不能提交 target
branch、commit message、author、Git argv、strategy 或 result commit；这些事实只来自 repository 配置和受信
integration service。merge approval 是 Graph 自有 interrupt，不把 commit/merge 暴露进任何 Tool inventory。
push、PR、fetch/pull、远程凭据和自动冲突修复没有注册或隐式执行路径。
final review 的 reviewer、report 与 user decision 同样不会增加这些能力；integration 关闭时 review approve 只结束
Graph，不能借由 Tool inventory 创建 commit 或 merge。

## Provider-native 能力

Qwen 等模型原生联网参数属于 `BaseChatModel` 请求能力，不伪装成本地 Tool。real 模式选择 qwen 且
`QWEN_CHAT_API_PROTOCOL=dashscope` 时，生产主链构造实现标准 LangChain `BaseChatModel` 的
`DashScopeNativeChatModel`，使用官方 text-generation Generation API；不得静默回退到 OpenAI-compatible。
显式选择 `openai_compatible` 时才构造 `ChatOpenAI`。planning coordinator 无论全局配置如何都强制关闭
Provider-native search；联网检索若属于计划，只能通过 task 委派给 fast 子 Agent。

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

### Stage 5C immutable review Tool profile（2026-08-24）

- Final-review profile 固定且只读：`coding_repo_list`、`coding_repo_search`、`coding_repo_read`、`coding_repo_status`、`coding_repo_diff`。五条生产路径全部调用 `CodingWorkspaceService.*_analysis_snapshot`，由 immutable manifest、identity/thread/workspace binding、inode/mode/size/content digest 与 snapshot 前后校验保护；`coding_repo_read` 的受信 artifact 额外携带实际 content bytes digest；不得把 `resolve_analysis_snapshot()` 返回的裸 `Path` 交给旧 live `list_files` / `search`。
- reviewer Tool 错误若 cause chain 含 `CodingWorkspaceError`、`PermissionError`、LangGraph control-flow 或 cancellation，必须原样传播到父图 fail closed；current-v2 read observation 的 snapshot/tree/content/path binding mismatch 同样转换为 `coding_review_binding_mismatch`，只有非安全的 provider/structured-output 不可用才规范化为 `unavailable`。
- reviewer evidence 的 `content_digest` 来自受信 `coding_repo_read` artifact 覆盖的实际内容字节，`evidence_digest` 再绑定 path、line、excerpt 与该 content digest；模型自述文本不能自行成为证据 identity。
- reviewer 总 model round 上限为 9，总 ToolCall 上限同为 9：最多 8 次 read Tool 后，第 9 次只供 ToolStrategy 结构化结果；未知 Tool 仍进入总 ToolCall/model round 预算，不能借名称过滤获得额外轮次。
