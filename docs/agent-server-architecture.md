# LangGraph Agent Server 部署架构

最后更新：2026-09-03

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
两者的官方 summarization 从同一 composition 投影的 `ChatConfig` 取得 context window、trigger/target ratio 与可选离线 token counter；
real DeepSeek/native compactor 缺 tokenizer 时在模型 composition 前启动失败。

生成媒体的 thread 临时资源位于 `/home/lenovo1/assistant_agent/threads/<thread_ref>/artifacts/generated/`；该目录是
媒体交付运行数据，不是 Agent filesystem mount、源码、安装副本或默认 cwd。`thread_ref` 由认证 identity 与 Agent Server
thread ID 的摘要确定，目录按 24 小时 TTL 回收；不存在
`workspace_id`、`workspace.json`、project registry、仓库副本、Git worktree 或 patch 回灌层。Agent 直接操作
Agent Server OS identity 有权访问的真实路径；`git` Tool 按调用提供的目标路径识别仓库根，不预注册或启动时
全盘扫描仓库；通用 `execute` 不执行直接 Git CLI。
Playwright 的 thread session 只复用浏览器进程状态，不使用上述 thread 目录；其进程 cwd 和输出目录直接取当前
`AssistantRunContext.cwd`，空闲 session 独立按 TTL 回收。

`start_async_task` 只把父子 thread/run correlation 写入 task handle、child thread metadata 和 worker run metadata。
只有进程内 async adapter 会在 start/update 的 loopback SDK
请求中附加随机 internal capability；auth 将其转换为 worker-only permission，并同时严格校验 worker metadata。普通
`X-Assistant-User` caller 即使提交形状完整的 async metadata 也不能创建 worker thread/run。main v4 与 Memory 的
thread/run 明确拒绝 `entry_profile=async_worker` 和 worker-only permission；worker metadata 必须是
`entry_profile=async_worker`。
internal capability 是当前本地单进程部署的进程内随机 secret，不进入 state、thread/run metadata、日志、`.env` 或
仓库；多进程部署前必须升级为共享 secret 或正式 service identity。

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

公开 `AssistantRunContext` 包含 `cwd`、`enable_memory`、`require_tool_approval`，以及可选的
`context_compaction_trigger_tokens` / `context_compaction_keep_tokens`；`cwd` 默认为 OS 用户 Home，且只接受
Home 内已存在目录。两个压缩值必须同时设置且 `0 < keep < trigger`；都不设置时继续使用 Provider context window 的
75% 触发、压缩后保留 15%。`require_tool_approval` 默认为 true，保存为 false 的
Assistant 会让原本受 HITL 管理的 Tool 自动执行。身份、入口和视觉 capability 只在
服务端签发的 namespaced run metadata 中。认证用户唯一来自 `Runtime.server_info.user.identity`；当前 tokenless
developer hook 从 `X-Assistant-User` 取得 identity，省略时为 `local-developer`，因此端口不得暴露给不受信网络。

`/agent-service/v1` custom route 只做 vendor frame 校验、connection/session/thread/run/delivery 关联、公开 SDK
stream/cancel/join、终态 `AIMessage` 与通用 Tool delivery artifact 投影、视觉引用接入和 callback。它不读取 checkpoint，
不按 Tool 名解析业务 artifact，不执行 Tool/Memory，
不选择 Assistant 模式，也不构造第二套 Runtime。旧 coding behavior attestation route 已删除。

回答后 `MemoryLifecycleMiddleware.after_agent` 在确定性 companion thread 上 rollback 自己标记的旧 pending Memory run，再 enqueue 新的
`assistant-memory-v1` delayed run。普通 chat 使用 Agent Server 原生 `multitask_strategy="interrupt"`；显式
checkpoint replay 使用 enqueue。主动投递使用独立 Store，不进入 `AssistantAgent` run。

media custom-app lifespan 单次加载 `AppConfig` 后，只保留 provider mode、`VisionConfig` 和 `MediaConfig`；进程级
`VisualPerceptionModule` 由该 lifespan 持有，Graph Tool 只消费其窄接口。媒体接入不得合并、
串行化或删除视觉 authority 定义的 SigLIP2 latest-wins 和并行关键帧 VLM 流水线。

## 本地部署

`scripts/run_server.py --backend dev` 使用 `langgraph dev`，checkpoint/Store 位于共享 `.langgraph_api/`。同一
worktree 只允许一个 dev server；PyCharm 管理的唯一实例固定为 `8089` 并保留原生 hot reload，Codex 只作为客户端
连接，不启动第二套服务。dev queue 使用固定校验的 `langgraph-runtime-inmem==0.33.0+assistant1` 最小 fork；补丁
只影响本地 in-memory runtime。

`--backend postgres` 使用本地 Agent Server 镜像、PostgreSQL 16/pgvector 与 Redis 6，默认绑定回环 `8088`。
PostgreSQL 持久化 assistant/thread/run/checkpoint/Store，Redis 只协调 stream/queue；`.langgraph_api/` 不迁移。
真实 `.env` 只作为未跟踪 env file 注入，不能提交或写入镜像。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_gateway_contract.py \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/unified-assistant-agent
```
