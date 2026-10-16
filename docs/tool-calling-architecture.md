# LangChain-native Tool 与扩展架构

最后更新：2026-09-03

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

Studio/SDK 直接运行 `assistant-business-v2`，使用业务 Agent 的完整受信 Tool surface。
以下 filesystem、Profile、MCP 与具体 Tool 规则适用于业务 Agent 及其 worker，不直接暴露给 Voice。
浏览器原生 Realtime 暴露业务任务提交、直接百炼搜索、get_business_task 与 cancel_business_task；仅业务委派创建独立业务 run。
直接搜索复用受信 inventory 内无需审批的只读搜索 BaseTool 及其原生 schema，返回搜索内容，异步等待不阻塞 WS 收音。
这些 run 复用该语音会话专属的业务 thread，以原生 rollback 替换冲突的旧任务。
接入桥只接受静态 schema，仅委派 request 作为 HumanMessage，经 Agent Server 的身份边界进入业务图，强制开启原生 HITL。
不复制 Voice 的 ASR 历史、工具轨迹或 prompt；业务自行运行其原生 Agent loop。
语音打断不触发任务取消；仅明确取消工具请求使用 run cancel(action=interrupt)。
启用 Memory backend 时原生 Voice 增加 `recall_memory(request)` 只读工具，使用受信身份和同一后端，
不允许模型指定用户或写入记忆；写入由已有独立 Memory Graph 处理。

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

15 个内建业务 Tool（calendar/contacts、email、lodging 与价格订阅、shopping、图片搜索、图片生成、
图生 3D 和四个媒体 Tool）均由官方 `@tool` factory 返回标准 `BaseTool`。handler 直接接收
`ToolRuntime[AssistantRunContext]`，它是框架注入的隐藏参数，不进入模型可见 schema；认证 identity 只从
`server_info.user.identity` 读取。handler 成功直接返回 `content_and_artifact` 的 `(content, artifact)`，预期或
清洗后的领域失败抛出 `ToolException`。Tool metadata 只保存 `source=builtin|deepagents|mcp`、availability 等观测或
条件暴露事实，不参与授权、重试或 HITL。

成功结果使用标准 `ToolMessage(content, artifact)`：`content` 是有界模型投影，`artifact` 保存结构化业务结果；
失败由 `ToolException` 或 `handle_tool_error` 转为可解释 error `ToolMessage`。Provider 原始响应、secret 和宿主路径
不进入模型上下文或 artifact。`image_generation` 成功后只把
`artifact://v1/{thread_ref}/generated/{filename}` 短引用写入文本 observation 和 artifact，不在
`ToolMessage` 中写入 Base64 或 `image` content block；主模型默认不读取生成物。需要图片实体的媒体投影、
`image_to_3d` 等消费者在自身边界按需通过统一 resolver 读取同一引用，并复核线程归属、大小、路径和媒体类型。
生成成功后还将同名图片复制到当前用户 `<cwd>/outputs/`；该用户副本不参与协议解析或 thread TTL 回收。
`image_to_3d` 未收到显式引用时，可从 checkpoint 的历史成功 `image_generation` ToolMessage 中选择最近引用，
因此支持跨对话轮次联动；显式引用始终优先，实体过期仍由 thread TTL 和 resolver 拒绝。
高德路线 MCP interceptor 在保留模型可见导航 Markdown 的同时，把同一受校验 URL 写入 namespaced
`structuredContent`；终端入口只从转换后的标准 ToolMessage artifact 确定性交付，不解析模型正文。

需要确定性交付时，Tool 在自身 artifact 中写入严格、有界的
`assistant_agent_delivery_v1={text, output_refs}`。购物、酒店和图片生成只在领域结果完成校验后写入；
全局 MCP interceptor 先删除外部结果中的同名保留字段，仅在成功校验 AMap 路线后重建并写入
`structuredContent`。这是 `ToolMessage.artifact` 的应用数据，不是自定义
message/state 协议。传输层只通用读取该 key 和标准 `type=file` block，不依赖模型复述，也不按 Tool 名解析业务 artifact。
只读 Tool 使用官方 `ToolRetryMiddleware` 有界重试；`live_view_inspect` 为避免重复
当前画面推理不进入自动 retry 清单。
`lodging_search` 仅让超时、Provider 不可用等瞬时失败进入该重试链；FlyAI
`CITY_CODE_CHECK_PARAM_ERROR` 会直接返回针对 `destination` / `nearby_poi` 的可操作错误，其他明确拒绝和响应错误也不重复提交。

`ToolContext`、`invoke_native_tool` 和以 `ToolResult` 为中心的生产兼容执行链均已删除，
`runtime/state.py` 的平行 Tool catalog/call/result ledger 也已删除。公开兼容 response 的同名字段属于
各自协议 owner，不能作为新 Tool 的输入、执行或结果边界。

`ToolProfileMiddleware` 先用当前 Graph 的真实 Tool inventory 裁剪受信静态 catalog，只保留非空 Profile 和其中
实际注册的 Tool；没有可用 Profile 时不暴露 `activate_tool_profile`。模型调用
`activate_tool_profile` 后，middleware 从私有 `active_tool_profile_ids` 缩小后续 `ModelRequest.tools`，并在执行
边界复核历史消息中的 ToolCall；激活结果的 `activated_tool_names` 返回该运行角色实际注册并开放的 Tool 名称。
`read_file` 常驻暴露，以便模型直接读取已发现的 Skill 正文；其余 filesystem Tool 仍需先激活 `filesystem` Profile。
至少一个 Profile 激活后，模型可自主调用 `deactivate_tool_profile` 精确隐藏指定 Profile 的实际 Tool 集；重复取消
幂等成功，其他 Profile 和常驻 Tool 不受影响，取消后的历史 ToolCall 仍在执行边界被拒绝。没有激活 Profile 时不向模型
暴露取消 Tool；取消只影响后续可见性，不中止已开始的 Tool，也不撤销既有副作用。
Skill、Todo、worker 文本和 artifact 都不能声明 profile 或授权 Tool。

根目录 `mcp_servers.json` 是纳入 Git 的本机部署配置，本机 `.env` 通过
`MULTIMODAL_AGENT_MCP_CONFIG_PATH=mcp_servers.json` 选择它。高德使用固定版本
`@amap/amap-maps-mcp-server@0.0.8`，凭据仅引用环境变量 `AMAP_MAPS_API_KEY`，不得写入 JSON。
该版本的 12 个只读工具进入显式 allowlist；`mcp_amap_maps_maps_weather` 不归属任何 Profile，
成功注册后默认暴露。其余 11 个高德工具归属 `travel`，通过 `activate_tool_profile(profile_id="travel")`
渐进暴露；这是预注册工具的可见性切换，不是调用时动态注册 MCP。
本机高德命令使用 `npx --offline`，避免每次 MCP 会话启动都访问 npm registry；新机器须先缓存固定包版本，
例如 `npm exec --yes --package=@amap/amap-maps-mcp-server@0.0.8 -- node --version`。

不存在的 ToolCall 由 `UnknownToolFeedbackMiddleware` 返回标准 error `ToolMessage`，保留原始
`tool_call_id`，并以 `unknown_tool` 和 `available_tool_names` 提示模型只调用已暴露工具。
该 middleware 放在模型工具过滤之后，把本次 `ModelRequest.tools` 中实际本地 Tool 名保存为私有
`exposed_tool_names`；错误反馈不使用完整注册表，不列出尚未激活或条件隐藏的 Tool，也不把
Provider-native 能力当作本地 Tool。主 Agent、通用 worker、coder 和 browser-operator 共用此实现，
同步与异步路径一致；快照不进入公开 input/output 或传给子 Agent。

Git 使用独立 `git` Tool Profile。Tool 接收 `target_path` 与不含可执行文件名的参数数组，先对目标路径执行
`git -C <path> rev-parse --show-toplevel`，再以 `shell=False` 在解析出的仓库根执行命令。main、通用 worker 与
coder 共用该 middleware；`execute` 的可见描述要求优先使用 `git` Tool，并在调用边界拒绝直接或 shell 链中的
Git CLI，返回 `use_git_tool` 错误，不启动 shell。

媒体 Tool 的条件暴露与 Skill/Profile 正交：`uploaded_media_inspect` 依赖最新用户消息中的标准上传媒体 block；
`live_view_inspect` 依赖本轮冻结 target；`visual_memory_search` 还要求当前 thread 有可检索文本；
`visual_reminder_manage` 要求连接已经收到并绑定有效视频帧。条件来自入口签发的 capability 与服务端事实，不读取
用户关键词；Tool handler 会再次校验身份、thread 和冻结边界。当前 media custom route 不注入
`source=live_camera` message block，因此不会满足这三个实时 Tool 的条件；详见视觉 authority。

Studio/LangChain 标准 `image|video` block 可直接携带 `base64 + mime_type`，图片也可使用 HTTP(S) URL；缺少 `source` 时视为
用户主动上传；兼容入口显式标记的 `source=uploaded + id`。条件 middleware 只从主模型可见 messages 中移除上传
二进制，Memory 后台抽取使用相同文本投影；原始 Graph state 保持不变，因此 Tool 仍通过 `ToolRuntime` 读取同一附件。
图片本地路径必须位于本轮 `AssistantRunContext.cwd`；上传视频当前只接受单个 MP4，同样限制 cwd、大小、时长与
解码分辨率，并在 Tool 内临时抽取最多 5 张 JPEG 后调用现有多图 VLM。Tool observation 和 artifact 不返回附件引用。
该静态抽帧不进入实时 H.264 ingestion、SigLIP2 selector、视觉时间线或 `live_view_inspect`。

## 统一 HITL 与 LocalShell 边界

所有显式列入 `interrupt_on` 的操作在 handler 前统一经过 Deep Agents `HumanInTheLoopMiddleware`：

- filesystem 的 `write_file`、`edit_file`、`delete`、`execute`；
- `git` Tool；
- composition 显式列入 `interrupt_tool_names` 的业务与 MCP Tool；
- `start_async_task`、`update_async_task` 与 `cancel_async_task`。

`AssistantRunContext.require_tool_approval` 在受信本机开发环境默认 false，通过原生 `when` 谓词跳过上述全部
interrupt，Tool 直接进入执行链，不需要自动提交 approve/resume；需要审批的 Assistant 可显式保存为 true。
未列入 `interrupt_on` 的 Tool 按 Deep Agents 原生默认直接执行。审批、interrupt、checkpoint 与 resume 使用 LangGraph/Agent Server 原生协议；外部副作用幂等
仍属于具体 Tool 或业务 API。HITL 只是治理，不提供进程隔离。批准 `execute` 等价于允许 Agent Server 的 OS identity
在当前 run cwd 下执行完整 command；该命令可能访问宿主路径、网络和 Git。filesystem Tool 与 shell 共享
Agent Server OS identity 的权限边界。当前 backend 仅适合受信本地个人 Agent，多租户或不可信
生产必须替换为 container 或 remote sandbox backend。

## 客户端执行实验与同机生产接线

`runtime/device_backend.py` 的 `DeviceBackend` 实现 Deep Agents 原生 `SandboxBackendProtocol` 的
异步文件操作与 `aexecute`；接口名不意味着本地执行具备沙箱。实验把它注入既有 `build_assistant_agent`，
不创建第二套 Agent loop、run manager 或 Graph identity，原生 ToolNode、Profile、HITL 与 checkpoint 保持原样。
生产 `AgentServerExecutionOwner.compose()` 装配的 working-directory backend 现已支持可选 `context.device_id`：
有绑定时异步文件/Shell 经 DeviceBackend 调用客户端，没有绑定时保持既有本机路径。具体同机注册限制见 Agent Server authority。

`DeviceBroker` 只负责反向 WebSocket RPC，可信装配提供 `(owner, device) -> token`，客户端主动连接。
`DeviceBackend` 从原生 `Runtime.server_info.user.identity` 获取 owner；设备名称由装配指定，不接受模型授权。
连接不允许重复接管，未授权、离线、断连均不回退服务端本机执行。token 不写入 context、checkpoint 或提示词。
实验 grant 是进程内能力凭据，不等于正式帐号系统或设备注册服务；同名设备可分属不同 owner。

本地组件入口为 `python -m assistant_agent.runtime.device_client --url ... --token-file ... --cwd ...`。
cwd 必须由本地用户指定且已存在；文件工具复用官方 `LocalShellBackend(virtual_mode=True)`，`/file.txt`
对应客户端 `<cwd>/file.txt`，Shell 相对路径以该 cwd 为起点。`--allow-shell` 默认关闭；显式开启代表接受
当前 OS 用户完整权限，不能声称被限制在 cwd。实验 Shell 只支持 POSIX，通过独立进程组处理取消、超时和输出上限。
Shell 不继承服务端/客户端应用凭据环境，使用最小环境。路径检查不是能对抗本机并发恶意修改的 OS 沙箱。

连接采用最大 1 MiB 帧，单设备最多 8 个未完成请求，客户端串行执行。单连接保留最多 10,000 个 request ID，
重复 ID 拒绝且不重新执行，达到上限关闭连接。RPC 不自动重试；断连或超时可能返回执行结果未知。
取消不回滚已完成文件副作用；已开始的同步文件操作完成后才释放客户端锁。POSIX 进程组清理不约束自行脱离进程组的程序。
文件 read/write/edit 拒绝已有特殊文件和超过 1 MiB 的文件；edit 在调用原生实现前检查替换后大小，避免内容膨胀。
这不是全部文件扫描操作的资源配额，也不能消除本机并发修改路径的竞态。
这些限制不是跨重连、checkpoint replay 或进程重启的持久幂等保证。

设备 backend 提供异步原生文件 API 与二进制 upload/download：256 KiB 分块、单文件上限 50 MiB，
上传在最终分块后原子替换，失败/断连清理临时文件。Git 共用客户端可取消 POSIX 执行；cwd Skills 和项目指令由客户端读取，
压缩历史写入执行目录 `.agent/history`。图片生成 Provider 留在服务端，生成物传客户端 `outputs`；
上传的本地媒体先从客户端下载到服务端临时目录供既有识别链处理，不改动实时视觉流水线。
安装客户端使用 `host_paths=True` 真实路径语义，Shell/Git 需显式开启；不能套用实验虚拟根规则。
同步设备 API、正式帐号认证、持久设备注册和跨平台安装包尚未提供；回环注册限制继续保留。

可重复离线实验及通过条件见 `tests/tdd/device-execution/README.md`：真实回环 socket、真实临时文件、独立客户端进程，
以及实验 backend 和生产 working-directory backend 的双身份 HITL 批准/读写。
`tests/tdd/device-production/` 验证同机注册、绑定和离线工具拦截；模型使用 scripted mock，不证明真实模型任务质量。

## MCP、Plugin 与异步任务

本地和 MCP Tool 进入同一 native inventory。外部 MCP 只通过官方 `MultiServerMCPClient`，配置必须提供显式
`allowed_tools`、`general_purpose_tools`、`interrupt_tools` 与确定性 `<namespace>_<server>_<tool>` 命名。
`general_purpose_tools` 只标记可安全自动重试的查询 Tool，不限制 worker 的 Tool surface；`interrupt_tools` 是原生 HITL
名单，两者互不推导；未列入
`interrupt_tools` 的 MCP Tool 默认执行。
生产不建立 MCP proxy、ToolSpec 镜像、plugin-private runner 或旧远端 Tool 映射。
浏览器能力由固定版本官方 Playwright MCP 提供。有 device_id 时 thread-scoped Playwright 调用经设备 RPC 执行，
客户端使用 `assistant-agent --install-browser --cwd <cwd>` 安装的 Chromium（需要 Node.js 20+ 和 npm）。
运行时放在 `<cwd>/.agent/browser-runtime`，每个 thread 的持久 profile 和输出放在 `.agent/browser/<thread-hash>`，
不需要已有 Chrome 或扩展，不继承用户 Chrome 登录态；每连接最多八个浏览器会话，断连关闭，失败不自动重放。
无 device_id 的 Studio 使用服务端 MCP 配置，官方 `--extension` 接管已有 Chrome 是显式可选方式。
其 thread session 按 `identity + thread_id + server_name` 复用，cwd 来自首次调用；call-scoped 外部 API MCP 仍在服务端执行。
`browser-operator`；是否审批由配置中的具体 Tool 名决定，
不从通用读写分类推断。配置为 `session_scope=thread` 的 server 按
`identity + thread_id + server_name` 复用官方 `ClientSession`；Playwright 进程 cwd 使用首次调用的
`AssistantRunContext.cwd`，输出目录固定为 `<cwd>/.agent/browser`。`browser-operator` 只持有原生
Playwright Tool，不装配 filesystem middleware。截图预览沿用 Playwright MCP 原生图片结果与 LangChain 标准
`image` content block；需要预览时不传 `filename`，固定文件名只用于用户明确要求的落盘场景。配置为 `call` 的 server 保持官方逐调用生命周期；空闲 thread session
按 TTL 关闭，进程关闭时统一释放剩余 session。

Deep Agents `AsyncSubAgentMiddleware` 的 `start/check/update/cancel/list_async_task(s)` 归入 `async-tasks` Profile。
start、update 与 cancel 显式进入 HITL；check/list 未列入 `interrupt_on`，按原生默认执行。start 创建 `assistant-worker-v2` thread/run 时
只保留父子 thread/run correlation。start/update 的 loopback SDK 请求还携带进程内 internal
capability；只有 auth 转换出的 worker permission 配合严格 metadata 才能创建 worker run，外部 shape-only 请求被
拒绝。该 capability 不进入 Tool 参数、state、task handle 或 thread/run metadata。worker 不能递归 delegation。

## 联网搜索与 Provider-native 能力

联网搜索使用百炼 MCP 的 `bailian_web_search`，经现有官方 adapter 装配为
`mcp_bailian_bailian_web_search`，注册后默认暴露，不归入需要激活的 Profile；主 Agent 与通用 worker 共用该 inventory。
按照[官方外部调用文档](https://help.aliyun.com/zh/model-studio/mcp-external-calls)，使用
`https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp` 的 `streamable_http` 传输。
新电脑默认模板 `config/mcp_servers.example.json` 包含百炼搜索和 Chromium 浏览器；百炼搜索默认加入
allowlist 和安全重试名单，并作为业务 Agent 的常驻核心工具暴露，不需要激活 Profile。
已有私有配置不会被安装脚本覆盖，升级时应核对正在使用的配置文件也注册了 `bailian` server。
`bearer_token_env=QWEN_API_KEY` 在建立连接时读取环境变量并生成 Authorization 头，不复制密钥到配置。
HTTP MCP 仅支持 HTTPS、call scope；缺失凭据时明确报错。百炼端需已开通该 MCP 服务。
配置文件由 `MULTIMODAL_AGENT_MCP_CONFIG_PATH` 指定，未指定时使用 `local/mcp_servers.json`；
必须显式启用 `MULTIMODAL_AGENT_MCP_ENABLED=1`。默认模板不绕过该开关或凭据检查。
提示词优先使用该搜索工具，不把模型原生搜索当作可调用 Tool，也不增加搜索 wrapper。
Voice 可直接查询公开资料与实时信息，复杂分析、用户数据和操作委派给业务 Agent；两者复用同一个搜索 Tool。
Qwen Realtime 保持 `enable_search` 关闭，因为百炼[客户端事件文档](https://help.aliyun.com/zh/model-studio/client-events)
规定原生搜索与 `tools` 不可同时启用，Voice 需要保留业务委派工具。
Qwen 主模型只使用 `DashScopeNativeChatModel`，不保留 OpenAI-compatible 分支或协议选择开关；旧 `QWEN_CHAT_API_PROTOCOL` 配置会明确拒绝。
项目的 `QWEN_CHAT_ENABLE_SEARCH` 默认 false，本机同样关闭；原生搜索仅保留显式配置能力，不作 MCP 失败回退。
仅在 real Qwen 配置完整时发送真实请求，mock 仍保持离线。启用原生搜索时设置 `enable_search=true` 和
`search_options.forced_search=true`，主 Agent 与复用模型的 worker 一致；关闭时不发送搜索参数。
官方多模态模型端点、流式要求与搜索策略见 Runtime authority。统一模型瞬时故障恢复与 custom stream
进度事件由 runtime authority 定义，Tool 层不实现第二套模型重试。

Provider 返回的 message content 是唯一正文，来源只保存在同一 `AIMessage.response_metadata`。adapter 不把来源
改写为 ToolMessage 或追加来源列表；媒体和自定义 UI 可按正文角标与结构化来源自行投影。

## 验证

```bash
python scripts/setup_local.py check
python -m compileall -q src/assistant_agent/tools src/assistant_agent/mcp
```

新电脑环境准备见 [部署说明](agent-server-architecture.md#新电脑安装与验收)。图片生成已移除旧电脑硬编码 fixture 支路，
统一由配置对应的真实或 mock adapter 处理，不依赖被删除的 `local/generated` 文件。
