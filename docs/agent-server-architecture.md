# LangGraph Agent Server 部署架构

最后更新：2026-09-08

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant Graph 的 Agent Server 部署、身份和资源生命周期权威 |
| Owns | auth、assistant、thread、run、queue、checkpoint、Store、cancel、stream 与 custom route 装配 |
| Does not own | Assistant 节点推理、Tool schema、Memory 后端语义、Media-Agent wire 字段 |
| 源码与 schema 入口 | `langgraph.json`、`src/assistant_agent/agent_server/` |
| 验证入口 | `docs/authority.toml` 中 `agent-server.verification` |
| 相邻 authority | 媒体 wire 见 [`media-agent-service-websocket.md`](media-agent-service-websocket.md)；运行图见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；视觉流水线见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 可运行 Graph 与 Assistant identity

`langgraph.json` 只注册三张当前可运行原生 Graph：

```text
assistant-native-v4 -> assistant_agent.agent_server.graph:native_assistant_graph
assistant-worker-v2 -> assistant_agent.agent_server.graph:native_worker_graph
assistant-memory-v1 -> assistant_agent.agent_server.graph:native_memory_graph
```

用户会话直接运行 v4 `AssistantAgent`；Memory recall 与 delayed extraction 调度由其
`MemoryLifecycleMiddleware.before_agent/after_agent` 接入。同步 task 在主 run 内调用通用 worker；异步 delegation
才创建 v2 worker thread/run。Memory 延迟提取使用 v1 Memory graph。
Agent Server 原生拥有 assistant、thread、run、queue、checkpoint、interrupt/resume、cancel、stream 和 Store；
项目不维护第二套 run manager、checkpoint facade 或产品状态机。

run auth 接收的是 Agent Server Assistant UUID，不是 graph ID 字符串。三张系统 Assistant 使用
`uuid5(langgraph_api.graph.NAMESPACE_GRAPH, graph_id)` 的确定性 UUID：

| graph | system Assistant UUID |
| --- | --- |
| `assistant-native-v4` | `8d030b92-89be-5d58-918d-ff35e996429a` |
| `assistant-worker-v2` | `ad895394-eb31-5aa1-a5ac-d24c4050ca05` |
| `assistant-memory-v1` | `b209df74-50ea-53ce-89ad-cc13d3c44e1b` |

auth 将这三个 UUID 精确映射到各自 graph。retired `assistant-native-v1`、`assistant-native-v2`、
`assistant-native-v3` 与 `assistant-worker-v1` 的系统 Assistant UUID 明确拒绝，不能借旧 identity 创建当前 run。
其他合法 UUID 只可能作为 Agent Server 已持久化的 v4 custom Assistant 进入 main graph：custom Assistant 的创建
只能选择 v4；name、description、config、context、版本和 active 状态由原生 Assistant API 持久化。read/search
对普通 API 身份按 owner filter 隔离，三个当前 system Assistant UUID 与 Studio 身份保持可见；update/delete 同样
受 owner filter 治理。run 创建时 Agent Server 会复用 Assistant read filter 校验 custom Assistant 所有权，不能在自己的
thread 上运行其他 owner 的 custom Assistant。auth 不把 custom Assistant 复制成项目配置或另一套 Runtime。

## Thread、checkpoint 与迁移

项目控制的 thread 只使用 SDK 原生 `metadata.graph_id` 作为 graph identity。thread create、run create 与 SDK stream
边界都复核 owner 和精确 graph identity，thread metadata update 禁止改写该字段。v4 custom Assistant 仍绑定 main graph identity；
worker 和 Memory 使用自己的 identity。

thread metadata 中服务端签发的 `assistant_agent_runtime` 只允许在 thread create 时写入，创建后保持冻结；任何
identity（包括 internal worker）的 thread metadata update 只要包含该保留 key 都直接拒绝。普通 label update、
不携带 metadata 的 state update 以及 owner-scoped interrupt/rollback 保持可用。

retired native v1/v2/v3、worker-v1 或缺失 identity 的 thread/checkpoint 只读，不能进入 v4
run/resume/replay/stream，也不能靠 metadata update 伪装升级。旧 run 的 interrupt/rollback 仍按 owner 开放，供部署
前 drain/cancel。迁移不转换旧 checkpoint；Studio、CLI 与媒体客户端必须新建 v4 thread。

媒体确定性 thread UUID 的 seed 包含当前 v4 graph ID，因此同一 v4 connection 重连稳定，但不会命中旧版本 UUID。
`SdkAgentServerClient` 对 `if_exists="do_nothing"` 返回的 existing thread 同样复核 identity，并在 stream 前再次读取
thread；失败时不创建 run、不改变 checkpoint。

## 统一 composition、本机文件系统与 worker

每个 Agent Server worker 进程只创建一个 `AgentServerExecutionOwner`。其 `compose()` 在装配期局部加载并持有完整、
冻结的 `AppConfig`，只向下游投影所需的窄配置段；owner 本身持有一个模型、一次发现的业务/MCP Tool inventory、一个
`MemoryBackend`、一个 `ThreadResourceManager`、线程级 MCP session pool，以及编译后的 main、worker、Memory graph。
schema/history/state 请求和 run 都复用该 owner；custom-app lifespan 在进程 shutdown 时关闭一次。
`load_app_config()` 仅位于 composition/入口；Provider、Tool、Memory、Media 叶子只接收显式窄配置，不读取应用配置
环境变量。Provider SDK policy 自身的 `from_env()` 不是应用配置 loader。

main Agent 使用按当前 run 的 `AssistantRunContext.cwd` 创建的 `LocalShellBackend`，并用 Deep Agents 原生
`virtual_mode=False` 接受真实绝对路径；filesystem
将 `.` 映射为 cwd，`/`、`/.` 和其他绝对路径保持宿主 OS 语义。
不装配 `/artifacts/`、`/scratch/`、`/uploads/` 等虚拟 filesystem route。main 和 worker 复用同一个
working-directory backend、基础模型、完整业务 Tool inventory、Prompt Builder、Skills、
Tool Profile、filesystem、`execute` 与 HITL 配置。Skills discovery 仍使用独立 `FilesystemBackend`，只读取产品内建
Skill。worker 不装配同步或异步 delegation middleware，也不运行主 Agent 的 Memory 提取生命周期。
两者的官方 summarization 从同一 composition 投影的 `ChatConfig` 取得 context window、trigger/target ratio 与模型 token counter；
real DeepSeek/native compactor 缺 tokenizer 时在模型 composition 前启动失败。

生成媒体的 thread 临时资源位于 `/home/lenovo1/assistant_agent/threads/<thread_ref>/artifacts/generated/`，供协议交付与 TTL 回收；
同一图片同时复制到用户 `<cwd>/outputs/` 供本地持续使用。thread 目录不是 Agent filesystem mount、源码、安装副本或默认 cwd。`thread_ref` 由认证 identity 与 Agent Server
thread ID 的摘要确定，目录按 24 小时 TTL 回收；不存在
`workspace_id`、`workspace.json`、project registry、仓库副本、Git worktree 或 patch 回灌层。Agent 直接操作
Agent Server OS identity 有权访问的真实路径；`git` Tool 按调用提供的目标路径识别仓库根，不预注册或启动时
全盘扫描仓库；通用 `execute` 不执行直接 Git CLI。
Playwright 的 thread session 只复用浏览器进程状态，不使用上述 thread 目录；其进程 cwd 取当前
`AssistantRunContext.cwd`，输出固定写入 `<cwd>/.agent/browser`，空闲 session 独立按 TTL 回收。

`start_async_task` 只把父子 thread/run correlation 写入 task handle、child thread metadata 和 worker run metadata。
只有进程内 async adapter 会在 start/update 的 loopback SDK
请求中附加随机 internal capability；auth 将其转换为 worker-only permission，并同时严格校验 worker metadata。普通
`X-Assistant-User` caller 即使提交形状完整的 async metadata 也不能创建 worker thread/run。main v4 与 Memory 的
thread/run 明确拒绝 `entry_profile=async_worker` 和 worker-only permission；worker metadata 必须是
`entry_profile=async_worker`。
worker 与媒体 loopback 各自使用当前本地单进程部署的进程内随机 internal capability，不进入 state、thread/run metadata、
日志、`.env` 或仓库；auth 只允许持有媒体 capability 的调用提交 `entry_profile=agent_service`。多进程部署前必须升级为
共享 secret 或正式 service identity。

统一 Agent 的全部副作用 Tool 由官方 HITL 在 handler 前 interrupt。HITL 是审批治理，不是进程隔离；批准
`execute` 等价于授权以 `lenovo1` 运行的 Agent Server 在 run cwd 下执行完整 command，可访问该 OS 用户有权
访问的宿主路径、网络和 Git。filesystem Tool 与 shell 共享这套 OS identity 权限边界。
当前 `LocalShellBackend` 只适合受信本地个人 Agent；多租户或不可信生产必须使用 container 或 remote sandbox backend。

## 公开输入、认证与媒体 custom route

公开 Graph input 使用原生 Agent input schema，只有标准 `messages`：

```json
{"messages":[{"role":"user","content":"hello"}]}
```

Studio/普通 Agent Server 客户端上传图片或视频时沿用 LangChain 标准多模态 content block，可使用
`type=image|video`、`base64`、`mime_type`，无需增加 Graph input 字段或专属上传 Graph。缺少
`source` 的标准多模态块按用户主动上传处理；`source=live_camera` 仍只属于实时视觉入口，不会误入静态上传。
上传实体保留在 Graph state 供 `uploaded_media_inspect` 读取，但在每次主模型调用的投影中移除，主模型只接收
文本和 Tool observation，Memory 后台抽取请求也只复制该文本投影，避免把历史 Base64 重复发送给 Provider。

公开 `AssistantRunContext` 包含 `persona`、`cwd`、`enable_memory`、`require_tool_approval`，以及可选的
`context_compaction_trigger_tokens` / `context_compaction_keep_tokens`；Studio 的 `cwd` 默认是
`~/assistant_cqy/studio`，且只接受 Home 内绝对路径。context coercion 不访问文件系统；工作区目录由异步入口准备。媒体握手的 `context` 复用同一 schema，cwd 默认按握手用户稳定映射到
`~/assistant_cqy/media/usr_<opaque-id>`。`persona` 允许 `general` / `call_assistant`，Studio 默认前者，媒体默认后者；只改变顶层回复策略，不授予身份或能力。两个压缩值必须同时设置且 `0 < keep < trigger`；都不设置时继续使用 Provider context window 的
75% 触发、压缩后保留 15%。`require_tool_approval` 在受信本机开发环境默认 false，保存为 true 的
Assistant 才让已配置 Tool 进入 HITL。身份、入口和视觉 capability 只在
服务端签发的 namespaced run metadata 中。认证用户唯一来自 `Runtime.server_info.user.identity`；当前 tokenless
developer hook 从 `X-Assistant-User` 取得 identity，省略时为 `local-developer`，因此端口不得暴露给不受信网络。

`/agent-service/v1` custom route 只做 vendor frame 校验、connection/session/thread/run/delivery 关联、公开 SDK
stream/cancel/join、终态 `AIMessage` 与通用 Tool delivery artifact 投影、视觉引用接入和 callback。它不读取 checkpoint，
不按 Tool 名解析业务 artifact，不执行 Tool/Memory，
不选择 Assistant 模式，也不构造第二套 Runtime。旧 coding behavior attestation route 已删除。

回答后 `MemoryLifecycleMiddleware.after_agent` 直接通过官方 `RemoteReflectionExecutor.submit()` 在确定性 companion
thread 上提交 `assistant-memory-v1` delayed run，使用官方 rollback 替换冲突的 Memory run。普通 chat 使用 Agent Server 原生 `multitask_strategy="interrupt"`；显式
checkpoint replay 使用 enqueue。主动投递使用独立 Store，不进入 `AssistantAgent` run。

media custom-app lifespan 单次加载 `AppConfig` 后，只保留 provider mode、`VisionConfig` 和 `MediaConfig`；进程级
`VisualPerceptionModule` 由该 lifespan 持有，通过 `asyncio.to_thread` 完成同步装配（含 FastEmbed/Hugging Face
依赖导入和本地模型加载），避免事件循环阻塞检查中断导入并留下不完整模块状态。Graph Tool 只消费其窄接口。媒体接入不得合并、
串行化或删除视觉 authority 定义的 SigLIP2 latest-wins 和并行关键帧 VLM 流水线。

## 本地部署

### 安装客户端的同机体验

发行入口 `assistant-agent`（`assistant_agent.client`）已连接现有生产 v4 图。它通过原生 SDK 管理对话、审批和取消，
同时启动本地执行连接，不创建 Agent loop 或第二个 Agent Server。`--cwd` 选择用户目录，客户端在该目录的 `.agent/`
保存按服务地址和身份区分的会话 ID；下次启动读取服务端同一 thread，checkpoint/Memory 仍由既有服务保存。

现有 custom app 提供 `/local-devices` 注册、`/local-devices/connect` 反向 WebSocket 和注销入口。
当前只允许回环连接且拒绝带 Origin 的请求，沿用受信本机开发身份，不能当作公网帐号系统。
注册把 identity、随机 device_id 与 cwd 绑定在当前进程，配对 token 只在客户端内存及连接 header 中使用。
重载后客户端需重新启动并注册。公开 context 的可选 `device_id` 选择该绑定；run auth 校验 owner/cwd/在线状态。

默认 composition 的 `WorkingDirectoryBackend` 已支持设备分发：有 device_id 时文件、Shell、Git 和 Playwright 经客户端执行，
无 device_id 的 Studio/媒体调用保留既有本机路径。主 Agent、worker、coder 和 browser-operator 在 Tool handler 前
检查绑定设备在线；断连不会改选服务端文件 backend，已开始的操作仍可能有结果未知或不可回滚的副作用。

执行边界已补齐：cwd Skills、项目指令和压缩历史来自绑定客户端；图片生成 Provider 仍在服务端运行，
生成物分块传到客户端 outputs，上传的本地媒体先从客户端取回临时处理。浏览器使用客户端独立 Chromium。
注册仍是**同机生产接线**；正式帐号认证、TLS、设备持久注册和跨平台安装包未完成，不能宣称“另一台电脑下载安装即用”。
无设备 context 只接受服务端 Home 内路径；设备 cwd 使用注册绑定的绝对路径，不按服务端 Home 校验。
安装客户端在本机校验自己的 Home 边界，保持真实绝对路径语义，不使用实验的虚拟根模式。
客户端默认逐次 HITL；Shell 还需 `--allow-shell`，使用客户端 PATH（优先安装环境）和最小环境白名单，
拥有当前 OS 用户权限而非沙箱。完整启动与安装命令见 `scripts/README.md`。

日常开发遵循 [官方本地开发指南](https://docs.langchain.com/langsmith/local-dev-testing)，使用 `langgraph dev`。
PyCharm 的 **Agent Server (Real)** 通过 `scripts/run_server.py --port 8089 --env-file .env` 调用官方 CLI，
直接使用本机 `hello_agent` 环境和源码，默认自动重载；`--no-reload` 可关闭。Real 指 Provider 配置，不表示生产部署。
wrapper 仅负责 env/config 来源、工作目录级单实例锁、严格端口检查和仓库外日志，不修改官方 watcher 或 Runtime。
使用官方 `langgraph-runtime-inmem==0.33.0`；删除原本地 fork，队列调度和恢复行为跟随上游实现。
checkpoint/Store 保存在 `.langgraph_api/`，重载会重建服务进程并短暂断开连接，不代表运行中任务无中断更新。
`langgraph.json` 保留项目依赖入口、Graph、auth、HTTP 和 Store 配置；依赖安装在本机环境，变更后重新安装并重启，
不需要构建镜像。当前 Mem0 后端不使用 Server Store embedding；旧 LangMem 配置保存在
`langgraph.langmem.json`，入口仍为 `agent_server/embeddings.py`，不再主动维护；具体规则见 Memory authority。
不再提供项目自定义 PostgreSQL/Docker/watch 部署入口；旧 PostgreSQL 数据卷保留但不由 dev 使用，也不自动迁移。
真实 `.env` 保持未跟踪。未来需要部署验证时再按官方 `langgraph up` 流程独立配置，不混入日常开发入口。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_gateway_contract.py \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/unified-assistant-agent
```
