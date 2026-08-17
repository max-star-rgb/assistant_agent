# LangGraph Agent Server 部署架构

最后更新：2026-08-17

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant Graph 的 Agent Server 部署、身份和资源生命周期权威 |
| Owns | auth、assistant、thread、run、queue、checkpoint、Store、cancel、stream 与 custom route 装配 |
| Does not own | Assistant 节点推理、Tool schema、Memory 后端语义、Media-Agent wire 字段 |
| 源码与 schema 入口 | `langgraph.json`、`src/assistant_agent/agent_server/` |
| 验证入口 | `docs/authority.toml` 中 `agent-server.verification` |
| 相邻 authority | 媒体 wire 见 [`media-agent-service-websocket.md`](media-agent-service-websocket.md)；运行图见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md) |

## 生产 Graph 入口

`langgraph.json` 只注册当前两张原生 Graph：

```text
assistant-native-v1 -> assistant_agent.agent_server.graph:native_assistant_graph
assistant-memory-v1 -> assistant_agent.agent_server.graph:native_memory_graph
```

不注册旧 graph alias，因此新图不解释旧 thread/checkpoint。Agent Server 原生拥有 assistant、thread、run、
queue、checkpoint、interrupt/resume、cancel、stream 和 LangGraph Store。项目不再在生产入口维护第二份 run
manager、cancel token、checkpoint facade 或产品状态机。

公开 Graph 输入为严格 `AssistantRootInput`：

```json
{
  "messages": [{"role": "user", "content": "hello"}],
  "execution_mode": "fast"
}
```

`execution_mode` 只允许 `fast|planning`，省略时默认为 `fast`。Memory Graph 的严格输入只有标准
messages；它由 Assistant Graph 通过 Agent Server SDK 调度，不向普通用户入口暴露 run type。
认证用户唯一来自 Agent Server 原生
`Runtime.server_info.user.identity`；`AssistantRunContext` 不复制用户或租户身份，只保存有默认值的
`entry_profile` 与 `media_capabilities`。执行模式不放入 context。
chat 到达时冻结的 `target_sequence` 绑定在媒体入口生成的标准 video content block，模型不能提交。

## 资源模型与 composition

| 资源 | 权威 owner | 含义 |
| --- | --- | --- |
| auth principal | Agent Server auth middleware | 调用方身份与 delegation 权限 |
| assistant/thread/run/checkpoint | Agent Server | Graph 定义、多轮状态与一次执行 |
| LangGraph Store | Agent Server | 可选跨 thread 数据资源，供 LangMem 等后端使用 |
| media connection | custom route | 一次 WebSocket 传输连接，不是 thread |
| delivery ID | custom route/outbox | 媒体 ACK 关联，不是 run 或 checkpoint |
| proactive delivery Store | custom route 与显式产品 publisher | 媒体连接 presence/claim/ACK；不是 LangGraph Store |
| Visual Perception Module | Agent Server 进程资源 | VLM client、Realtime Observer、视觉语义 Store 与连接级观察句柄；不是 Graph Runtime |

Agent Server async factory 在每个 worker 进程首次取图时创建唯一 `AgentServerExecutionOwner`，持有标准
`BaseChatModel` Provider adapter、静态本地 `BaseTool`、一次发现得到的官方 MCP tools、一个
`MemoryBackend`、已编译但不绑定 checkpointer 的 `AssistantRootGraph` 与
`AssistantMemoryExtractionGraph`；后续两个 graph 的 schema、history、state 与 run 取图全部复用同一 owner，
不重复装配。LangMem 引用首次 factory 注入的进程 Store；custom-app
lifespan 在进程 shutdown 时统一关闭 owner。进程级 `VisualPerceptionModule` 独立拥有视觉
Provider、Observer 和语义 Store 生命周期；run-local Tool 只注入它的只读资源，不重复创建实时观察流水线。
`http.app` 的 FastAPI lifespan 是该模块的进程 owner：API Server、queue worker 或独立 custom app 各自在
本进程 shutdown 时关闭一次。graph factory、schema/history/state 请求和单个 run 只借用该模块，不参与
关闭；媒体 WebSocket 只关闭自己创建的 `VisualPerceptionSession`。

composition 只构造标准模型、Tool、Memory backend 与两张静态原生 Graph，不构造平行 Graph
Runtime、产品状态投影器或 Workflow host。

所有 chat 入口最终调用同一个 `assistant-native-v1`，因此 Memory debounce 不散落在 Studio、CLI、HTTP 或
WebSocket adapter：主图开头使用官方 SDK 查找并 rollback 同 thread、带专用 metadata 的旧 pending Memory
run，回答后再 enqueue 一个新的 delayed `assistant-memory-v1` run。Agent Server 继续拥有真正的 delay 与
queue；项目不创建 timer 或第二套队列。

## Auth 与身份

LangSmith Studio 携带 Agent Server 内建的 `x-auth-scheme: langsmith` 认证，先由框架校验 LangSmith 会话并
构造 `StudioUser`，不会进入项目的 service-token authenticate handler；后续 assistant/thread/run 授权仍按
该 Studio identity 执行。mock/local 的非 Studio 客户端可用 `X-Assistant-User` 构造开发 principal。

real mode 的非 Studio HTTP/media 客户端要求 `ASSISTANT_AGENT_SERVER_SERVICE_TOKEN`；除 Bearer token 外还需
提供 authenticated identity 与对应 HMAC-SHA256 签名。thread metadata 按 auth owner 限制；run 与 Store
沿用同一 principal。connection、vendor session、thread、run 与 delivery ID 始终是不同身份轴。

## `/agent-service/v1`

custom route 只负责：

- 解析、校验 vendor frame；
- 关联 connection、vendor session、native thread/run、chat 与 delivery；
- 使用公开 `langgraph_sdk` 创建 run、消费 resumable stream、join 与 cancel；
- 从 terminal values 选择最新标准 `AIMessage` 并机械投影媒体响应；
- 把解码帧提交给连接级视觉观察句柄，并在 chat 到达时冻结、promote 最后一帧；
- 按 native thread 从主动投递 Store 串行 claim，处理 ACK、lease 与重连补投；
- 承载不执行 Graph 的 callback route。

它不读取 checkpoint，不执行 Tool/Memory，不构造旧 Runtime，也不翻译项目 run/error 状态机。SDK stream 使用
messages/updates/values；短暂订阅断开后按 last event ID 调用 `threads.join_stream`。WebSocket 断开时
best-effort cancel 当前连接仍活动的 reactive runs；delivery ACK 不改变 run 或 checkpoint。

主动投递不进入 `AssistantRootGraph`；当前图没有业务生产者，因此不保留休眠的 state channel 或 dispatch
节点。显式产品 publisher 可按稳定 message ID 写入独立 Store。媒体连接启动 thread-specific pull pump；
durable 行只有匹配 ACK 才完成，断线或超时释放为
queued，ephemeral 离线时直接 skipped。当前 SQLite 实现面向单实例或共享受控卷，不宣称多主机一致性。

H.264 解码与 3D callback 属于媒体边缘资源。解码后的有界 JPEG 引用保存在 SQLite frame index，Graph State
只携带稳定引用。解码帧同时提交给 `VisualPerceptionModule` 内部的 `RealtimeVideoObserver`；后台 VLM 文本写入
视觉语义 Store。需要严格当前画面的 run 通过 video block 的可信 `target_sequence` 等待该 exact frame 的有界结果，
其他 Agent Server run 不依赖该等待。3D callback 只向当前在线连接发布中性 artifact，不启动第二次 Graph。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_gateway_contract.py \
  tests/tdd/native-agent-parent-graph/test_agent_server_factory.py \
  tests/tdd/native-agent-parent-graph/test_media_native_adapter.py \
  tests/tdd/native-proactive-delivery/test_media_delivery_pump.py
```
