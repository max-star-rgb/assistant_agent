# LangGraph Agent Server 部署架构

最后更新：2026-08-27

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant Graph 的 Agent Server 部署、身份和资源生命周期权威 |
| Owns | auth、assistant、thread、run、queue、checkpoint、Store、cancel、stream 与 custom route 装配 |
| Does not own | Assistant 节点推理、Tool schema、Memory 后端语义、Media-Agent wire 字段 |
| 源码与 schema 入口 | `langgraph.json`、`src/assistant_agent/agent_server/` |
| 验证入口 | `docs/authority.toml` 中 `agent-server.verification` |
| 相邻 authority | 媒体 wire 见 [`media-agent-service-websocket.md`](media-agent-service-websocket.md)；运行图见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；视觉流水线见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 生产 Graph 入口

Agent Server process owner 静态持有一份 `CodingWorkspaceService` 和一份官方
`AssistantCodingAgent`。本地 Studio composition 固定把当前项目登记为内部 `assistant-agent` repository，
不向 Assistant Context 暴露仓库选择。每个 `user.identity + thread_id + internal repo ID` 解析到独立临时 detached Git worktree，
workspace ref 使用服务端 HMAC 派生，metadata、锁和 TTL 位于受管 workspace root，不进入 Graph state。

Coding Agent 由 Deep Agents 0.7.8 的 `create_deep_agent` 编译。项目只实现一个
`SandboxBackendProtocol` adapter：每次 Tool 调用从 LangGraph runtime 读取 Agent Server 认证 identity、当前 thread 和
进程内部 repository ID，调用 `CodingWorkspaceService.resolve()`，再把文件与命令操作委托给以该 worktree 为根的官方
`LocalShellBackend`。文件 Tool 使用 virtual path；shell 不继承宿主环境变量。当前 adapter 适合受信本地开发，不宣称
提供容器隔离；生产不受信执行应替换为 thread-scoped container 或 remote sandbox backend。

`write_file`、`edit_file`、`delete` 和 `execute` 由 Deep Agents 官方 HITL middleware 发出 interrupt，
Agent Server 原生 checkpoint/resume。生产不装配旧 coding validation、dependency/credential/artifact、review、repair 或
Git integration service，也没有专用或无人审批的 commit、merge、push、PR 流程。批准 `execute` 仍可运行任意宿主
shell 命令，包括 Git 与网络操作；硬隔离必须由 container/remote backend 提供。workspace TTL 仍由现有 service 在 resolve 时清理；
关闭 process owner 时只关闭该 service，不维护 snapshot reaper 或第二套运行时。

`langgraph.json` 只注册三张可运行原生 Graph：

```text
assistant-native-v3 -> assistant_agent.agent_server.graph:native_assistant_graph
assistant-memory-v1 -> assistant_agent.agent_server.graph:native_memory_graph
assistant-worker-v1 -> assistant_agent.agent_server.graph:native_worker_graph
```

coding 只作为 `assistant-native-v3` 的 `coding_agent` 子图执行，不注册 inspector 或独立 coding run graph。

`assistant-worker-v1` 是与 RootGraph 平级的独立生命周期后台执行图。每次 delegation 创建独立 thread/run；auth 对
worker thread/run 继续强制相同 owner。worker 与 fast 使用同一模型、prompt、middleware 和静态配置，但只装配
`effect=read` 的业务 Tool，且不暴露异步 delegation Tool。Deep Agents 当前异步 middleware 不透传父
`ToolRuntime.server_info.user` 且不在 thread create 指定 graph，因此项目仅替换其五个 Tool 的 transport 实现：显式绑定
worker graph，并以 `X-Assistant-User` 转发当前受信 identity；Tool schema、description、prompt 和 state contract 仍由官方
middleware 所有。transport 在创建 worker thread 前生成稳定 `task_id`（同时作为 child thread ID），并把
`assistant_agent_task_id`、`assistant_agent_parent_thread_id`、`assistant_agent_parent_run_id` 写入 child thread
及其每个 run 的 metadata；缺少父 thread/run identity 时不创建 worker 资源。

`assistant-native-v1` 与 `assistant-native-v2` 都不作为指向当前图的 alias 注册：当前图不能解释或 replay
v1/v2 planning checkpoint。这是一次显式 graph ID 升级，不是 checkpoint 自动 migration。新原生 Deep Agents
planning state 也不迁移同一 v3 graph ID 下已删除的 A-lite planning state；切换后既有 planning thread 只作历史
inspection，Studio 与客户端必须新建 thread。部署 v3 前，operator 必须按 graph ID 枚举 v2 的 pending/interrupt runs，
并逐个 drain 或 cancel；v1/v2 历史 checkpoint 只读，不得从 v3 resume/replay。项目控制的可运行 thread 在创建时
同时写入 SDK 原生 `graph_id` 和稳定 metadata `assistant_graph_id`。Agent Server auth 按 create 的 graph identity
把 Studio/项目 chat thread 规范为 v3，同时允许独立 Memory/worker graph 使用自己的 identity；chat run create 与显式
metadata identity update 使用 owner + graph identity 过滤，因此旧 thread 不能靠 update 或 Studio 直连进入新图。
旧 run 的 interrupt/rollback 只按 owner 授权，仍可执行部署前的 v2 drain/cancel。`SdkAgentServerClient` 对返回的新建或
`if_exists="do_nothing"` existing thread 校验 `assistant_graph_id`，并在开始 `runs.stream` 前重新读取 thread
做同一精确校验。`assistant-native-v1`、`assistant-native-v2` 或缺失该字段的 unknown thread 在任何 v3 普通 run、resume 或 stream
开始前稳定拒绝，因此不会创建 run 或改变 checkpoint；thread/state/history 与既有 stream 的只读检查仍允许。
部署迁移所需的 v2 drain/cancel 也不受该 guard 阻止。guard 接受每次调用的 expected graph ID，不把 v3
硬编码成所有独立 Graph 的全局限制；Memory 等独立 Graph 在自己的运行边界使用自己的 graph ID。

`assistant-native-v3` graph 下保留系统创建的同名默认 assistant。Studio 可为同一 graph 创建和维护 Assistant；其公开
context schema 只暴露 `execution_mode` 与 `enable_memory`。Assistant 的 name、description、config、context、版本
与 active 状态均由 Agent Server 原生持久化，auth 不重写这些 payload。普通 API 用户创建时由 auth 追加 owner metadata，
更新和删除按 owner 过滤；LangSmith Studio 身份沿用官方 Studio auth，不附加 owner 过滤，以便 Graph 页面原生管理其创建
的无 owner Assistant。自建 Assistant 仍使用同一 graph，不建立新的 Runtime 或 checkpoint schema；Studio 选择它后，
messages-only input 在 `execution_router` 按其 context 路由。run 顶层同名 context 覆盖 Assistant context；默认
`execution_mode` 为 fast，`enable_memory` 为 true。

新 assistant 与 run 必须选择 `assistant-native-v3`，Studio 用户也必须切换到该新 graph ID。媒体确定性
thread UUID 的 seed 包含 `assistant-native-v3`，因此同一 v3 connection 重连仍稳定，但不会命中旧 v1/v2 UUID；即便
命中一个外部指定的既有 ID，中央 metadata 校验仍然生效。CLI 的新 thread 与普通 `--thread-id` run 复用同一
guard。Agent Server 原生拥有 assistant、thread、run、
queue、checkpoint、interrupt/resume、cancel、stream 和 LangGraph Store。项目不再在生产入口维护第二份 run
manager、cancel token、checkpoint facade 或产品状态机。

公开 Graph 输入为严格 `AssistantRootInput`：

```json
{
  "messages": [{"role": "user", "content": "hello"}]
}
```

不可变运行配置由 `AssistantRunContext` 承载：`execution_mode` 只允许 `fast|planning|coding`，省略时默认为
`fast`；`enable_memory` 默认为 true，并同时控制该 run 的 recall 与 delayed extraction。Assistant 保存持久 context，单次 run 可提交同名稀疏 context 覆盖它；
auth 不展开默认值，合并后的 context 由 Graph schema 统一校验。
Memory Graph 的严格输入只有标准
messages；它由 Assistant Graph 通过 Agent Server SDK 调度，不向普通用户入口暴露 run type。
认证用户唯一来自 Agent Server 原生
`Runtime.server_info.user.identity`；Studio 可编辑的 `AssistantRunContext` 不复制用户、租户身份、prompt 或仓库选择，
只保存 execution preset 与 Memory 开关。入口 profile 和媒体入口在 chat 开始时签发的 opaque 视觉
capability token 只放入 namespaced run metadata；媒体能力和实时模式由当前标准 message 的受信来源投影判定，不是
Assistant 配置。窗口内容不进入标准 messages/context，
也不由模型或普通 Graph 输入提交。middleware 和 Tool 必须以
认证身份、thread 与 token 回到进程视觉模块解析冻结投影，伪造或过期 token 均 fail closed。

## 资源模型与 composition

| 资源 | 权威 owner | 含义 |
| --- | --- | --- |
| auth principal | Agent Server auth middleware | 调用方身份与 delegation 权限 |
| assistant/thread/run/checkpoint | Agent Server | Graph 定义、多轮状态与一次执行 |
| LangGraph Store | Agent Server | 可选跨 thread 数据资源，供 LangMem 等后端使用 |
| media connection | custom route | 一次 WebSocket 传输连接，不是 thread |
| delivery ID | custom route/outbox | 媒体 ACK 关联，不是 run 或 checkpoint |
| proactive delivery Store | custom route 与显式产品 publisher | 媒体连接 presence/claim/ACK；不是 LangGraph Store |
| Visual Perception Module | Agent Server 进程资源 | 视觉 authority 的进程级 owner，包含共享 embedding coordinator 与连接级视觉提醒 registry；不是 Graph Runtime |
| remote video archive | custom-app lifespan | 连接级 H.264 顺序归档、30 秒 MP4 切片、待上传 manifest 与临时下载 capability；不是 Graph Runtime |

Agent Server async factory 在每个 worker 进程首次取图时创建唯一 `AgentServerExecutionOwner`，持有标准
`BaseChatModel` Provider adapter、静态本地 `BaseTool`、一次发现得到的官方 MCP tools、一个
`MemoryBackend`、已编译但不绑定 checkpointer 的 `AssistantRootGraph`、`AssistantBackgroundWorker` 与
`AssistantMemoryExtractionGraph`；后三个 graph 的 schema、history、state 与 run 取图全部复用同一 owner，
不重复装配。LangMem 引用首次 factory 注入的进程 Store；custom-app
lifespan 在进程 shutdown 时统一关闭 owner。进程级 `VisualPerceptionModule` 的内部算法和资源边界由视觉
authority 定义；run-local Tool 只借用其窄消费接口，不创建第二套视觉流水线。
custom-app lifespan 启动后先通过同进程本机 Agent Server 的只读 graph endpoint 完成 Provider、MCP discovery
与静态 graph composition，再通过官方 SDK 发起 `thread_id=None`、`interrupt_before="*"` 的临时原生 run，
使首次 invocation 冷路径也在媒体 WebSocket 握手完成前结束。该 probe 不进入任何节点，不调用 Memory、模型或
Tool，也不创建业务 thread；整个预热总时长有界，失败只记录安全告警，后续正式 graph 访问仍可按原生路径完成。
`http.app` 的 FastAPI lifespan 是该模块的进程 owner：API Server、queue worker 或独立 custom app 各自在
本进程 shutdown 时关闭一次。graph factory、schema/history/state 请求和单个 run 只借用该模块，不参与
关闭；媒体 WebSocket 只关闭自己创建的 `VisualPerceptionSession`。

composition 在构造 Tool inventory 前只加载一次 repo Skill catalog，并把同一实例显式注入 inventory 的
Skill loading plugin 与 fast agent；planning coordinator 通过 Deep Agents `CompiledSubAgent` 直接引用同一个 fast agent，
不再次读取 catalog。composition 构造标准模型、该 Tool inventory、Memory backend 与三张静态原生 Graph，不构造
平行 Graph Runtime、产品状态投影器或 Workflow host。

## 本地部署与持久化

`scripts/run_server.py` 提供两个显式 backend：

- `dev` 使用 `langgraph dev`，checkpoint 与 Store pickle 到仓库 `.langgraph_api/`。该目录由整个工作目录共享，
  只允许同时运行一个 dev server；多个端口并行运行会竞争同一份退出落盘状态，不作为可靠持久化方案。dev bootstrap
  在 WatchFiles 扫描阶段排除该运行时目录，避免 pickle 定时原子写入形成无效 change 日志，但仍保留 Python 源码 hot reload。
- `postgres` 使用 `langgraph build` 生成 `assistant-agent/langgraph-api:local`，再由
  `deploy/agent_server/compose.yaml` 启动 Agent Server、PostgreSQL 16/pgvector 与 Redis 6。Agent Server
  通过 `POSTGRES_URI` 持久化 assistant、thread、run、checkpoint 与 LangGraph Store，Redis 只承担运行时
  stream/queue 协调。LangMem 仍使用 factory 注入的 Agent Server Store，不引入项目自有 PostgreSQL adapter。
  `langgraph.json` 同时声明运行镜像所需的 LangMem optional runtime 依赖，不能只依赖宿主 Python 环境已安装的 extra。

当前 dev backend 使用基于官方 `langgraph-runtime-inmem==0.33.0` 的最小本地 fork
`0.33.0+assistant1`，补丁与可重复构建入口分别是
`patches/langgraph-runtime-inmem/0.33.0-native-recovery.patch` 和
`scripts/install_patched_inmem_runtime.py`。该 fork 仍由 Agent Server runtime 原生拥有 queue：创建即时 pending
run，或 worker 完成并释放 thread（包括 retry run 已恢复为 pending）后，通过跨 event loop 的 generation signal
唤醒唯一 queue scheduler；scheduler 先扫描再等待，避免 lost wake-up。runtime 启动时同时恢复同一持久化目录中的
retry counter，确保 hot reload 后同一 run 从后续 attempt 继续执行，不复用首次执行的 LangSmith root identity。
补丁只属于 `langgraph dev`；postgres/Redis backend 不加载它。安装器固定校验官方 wheel SHA-256，上游内容或
接口漂移时必须先人工 rebase，禁止静默套用。

本地 dev 的唯一常驻入口由 PyCharm 管理，固定使用 `8089` 并保留 `langgraph dev` 原生 hot reload。Codex 默认
作为客户端连接该服务；修改源码后先等待 reload，只有需要完整重启时才重启同一个 `8089` 实例。dev backend
若临时使用 `8090`，仅供 PyCharm Server 已停止后的隔离诊断，诊断完成即停止；postgres backend 则把 `8088`
作为独立持久化部署的默认端口。`scripts/run_server.py` 对 dev backend 持有工作目录级
单实例锁，并在启动前要求请求端口可用，禁止框架自动漂移到随机端口；默认日志按请求端口写入
系统临时目录下的 `assistant_agent/logs/agent_server-<port>.log`，避免日志写入触发源码 watcher 后形成
自反馈 reload；dev 显式日志路径同样不得位于仓库监听树内。postgres 日志仍写入
`.data/logs/agent_server-<port>.log`。dev 启动时 wrapper 会从受版本控制的 `langgraph.json` 生成一次性配置，
只把 `env` 字段替换为 `--env-file` 指定的绝对路径；`--no-env-file` 则替换为空对象并只继承进程环境，避免
LangGraph CLI 再从仓库 `.env` 覆盖显式 mock 配置。一次性配置位于系统临时目录，进程退出后删除。
这些约束不创建项目自有 Runtime，也不把两个端口解释为两个 worker。

postgres backend 的 API 仅映射到 `127.0.0.1:${ASSISTANT_AGENT_SERVER_PORT}:8000`，默认宿主端口为 8088；
PostgreSQL 与 Redis 不映射宿主端口，也不复用旧 Langfuse 服务。PostgreSQL 数据保存在独立 named volume
`assistant-agent-langgraph-postgres-data`，普通 `restart`、`stop` 或重新构建 API 镜像不会删除数据。
`.langgraph_api/` 不迁移到 PostgreSQL。`.env` 仅作为未跟踪的容器 env file 注入，不能写入镜像或提交。

首次构建启动：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --backend postgres --host 127.0.0.1 --port 8088 --env-file .env --rebuild
```

代码未变化时省略 `--rebuild`。此入口以前台 Compose 进程运行，Ctrl-C 停止该专用 stack，但保留 PostgreSQL
volume。删除 volume 属于显式数据销毁操作，不是正常停止流程。

operator 需要检查或恢复 conversation 时，`scripts/agent_cli.py` 直接使用公开 SDK：
`threads.get_history()` 只显示 checkpoint 元数据，`runs.wait(input=None, checkpoint_id=...)` 从历史
checkpoint 创建 replay 分支，`runs.cancel(action="rollback", wait=True)` 丢弃仍可取消的 run 及其
checkpoints。replay 与 rollback 都要求精确确认；项目不读取 saver、不维护 checkpoint facade，也不把 Graph
state 回滚描述为已完成外部 Tool 副作用的自动撤销。

普通用户 chat（媒体入口与 CLI）使用 Agent Server 原生 `multitask_strategy="interrupt"`。同一 thread
存在 pending/running/retrying 旧 run 时，由 Agent Server 中止旧 run、保留已提交 checkpoint，并把新用户输入
加入 thread 后继续；旧 run 已进入终态时，新 run 正常开始。显式 checkpoint replay 继续使用 `enqueue`。
项目不为该行为扫描旧 run、手工合并历史或实现第二套恢复状态机。

所有当前 chat 入口最终调用同一个 `assistant-native-v3`，因此 Memory debounce 不散落在 Studio、CLI、HTTP 或
WebSocket adapter：主图在回答后使用官方 SDK，在由 chat thread 确定性派生且绑定 `assistant-memory-v1` graph
identity 的 companion Memory thread 上查找并 rollback 带专用 metadata 的旧 pending Memory run，随后 enqueue
一个新的 delayed `assistant-memory-v1` run。chat thread 不保留后台 pending run；Agent Server 继续拥有真正的 delay 与
queue；项目不创建 timer 或第二套队列。若合并后的 runtime context 中 `enable_memory=false`，父图同时跳过 recall 与该调度。

## Auth 与身份

LangSmith Studio 携带 Agent Server 内建的 `x-auth-scheme: langsmith` 认证并构造 `StudioUser`；非 Studio
客户端在 mock 与 real 模式下都通过 tokenless auth hook 构造 developer principal：`X-Assistant-User` 存在时
直接作为 identity，省略时使用 `local-developer`。项目不读取 Bearer token，也不校验 delegation 签名。
thread metadata 仍按 auth owner 限制，run 与 Store 沿用同一 principal；但 identity 由客户端声明，因此该部署
不具备跨不受信网络的身份认证能力，不应把端口暴露给不受信调用方。connection、vendor session、thread、run
与 delivery ID 始终是不同身份轴。

## `/agent-service/v1`

Stage 5E behavior eval 使用同一 custom app 的 authenticated
`/internal/evaluation/coding-attestation` 只读端点取得实际进程 composition。该端点只接受 loopback developer
principal，返回非 secret、严格有界的 graph/provider/model、boot nonce、coding registry 与 repository config
digest；它不创建 thread/run，不审批 interrupt，也不暴露 repository path 或 Provider secret。评测 runner 将其
canonical digest 冻结进 evaluation checkpoint 并在 case 前后重新采样；attestation 只证明 Server composition，
不拥有 Coding Agent 决策语义或替代 checkpoint guard。

custom route 只负责：

- 解析、校验 vendor frame；
- 关联 connection、vendor session、native thread/run、chat 与 delivery；
- 使用公开 `langgraph_sdk` 创建 run、消费 resumable stream、join 与 cancel；
- 从 terminal values 选择最新标准 `AIMessage` 并机械投影媒体响应；
- 把解码帧提交给连接级视觉句柄，并把视觉模块返回的可信目标边界投影到 chat；
- 按 native thread 从主动投递 Store 串行 claim，处理 ACK、lease 与重连补投；
- 承载不执行 Graph 的 callback route。

同一 custom app 还提供只读 `/artifacts/generated/{filename}`，供受信程序消费者读取图像 Tool 落盘后的
受管图片。该路由只接受受管目录中的单层文件名，并限制文件大小和可识别图片 MIME；配置
`ARTIFACT_BASE_URL` 时，图像 Tool 会在 `ToolMessage.artifact.images[].url` 中附带客户端可访问的绝对 URL。
当前 Studio 不保证渲染 Tool artifact。

显式启用远端视觉记忆时，同一 custom app 还提供
`/internal/memory-media/{opaque-token}`。该路由只解析进程内、带 TTL 的一次任务 capability，返回已完成的
受管 MP4；不接受任意路径，也不承担视频处理或 Memory 调度。归档服务及 SQLite 待上传 manifest 由
custom-app lifespan 创建、恢复和关闭。

它不读取 checkpoint，不执行 Tool/Memory，不构造旧 Runtime，也不翻译项目 run/error 状态机。媒体 SDK stream
只订阅 messages/values：messages 投影模型正文增量，values 读取权威终态；updates/custom 仅留给显式选择这些
模式的 Agent Server 原生 SDK 或 Studio 消费者。短暂订阅断开后按 last event ID 调用
`threads.join_stream`。WebSocket 断开时
best-effort cancel 当前连接仍活动的 reactive runs；delivery ACK 不改变 run 或 checkpoint。

主动投递不进入 `AssistantRootGraph`；当前图没有业务生产者，因此不保留休眠的 state channel 或 dispatch
节点。显式产品 publisher 可按稳定 message ID 写入独立 Store。媒体连接启动 thread-specific pull pump；
durable 行只有匹配 ACK 才完成，断线或超时释放为
queued，ephemeral 离线时直接 skipped。当前 SQLite 实现面向单实例或共享受控卷，不宣称多主机一致性。

H.264 解码与 3D callback 属于媒体边缘资源。解码后的 JPEG 保存在连接级有界临时目录，最近帧引用只进入
进程级有界内存 frame index，断线时索引与临时 JPEG 一并清理；Graph State 只携带稳定引用，不为实时帧建立
SQLite 热路径。解码帧提交后的并发观察、关键帧、文本发布和目标帧等待全部由视觉 authority 负责；Agent
Server 只传递稳定引用与可信目标边界。3D callback 只向当前在线连接发布中性 artifact，不启动第二次 Graph。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_gateway_contract.py \
  tests/core/integration/test_runtime_lifecycle.py
```
