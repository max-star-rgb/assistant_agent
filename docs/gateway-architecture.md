# Gateway Architecture

Last updated: 2026-08-04

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Gateway session/run 与实时入口生命周期的当前权威 |
| Owns | session、queue、admission、run、cancel、interrupt、reconnect、frame 映射与 delivery |
| Does not own | Assistant 推理、Tool 执行、Provider stream 内部语义、Media-Agent wire 字段 |
| 源码与 schema 入口 | `src/assistant_agent/gateway/`、`src/assistant_agent/api/gateway_*.py` |
| 验证入口 | `docs/authority.toml` 中 `gateway.verification` |
| 相邻 authority | 媒体协议见 [`media-agent-service-websocket.md`](media-agent-service-websocket.md)；Runtime stream 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md) |

## 1. 文档边界

本文是 `assistant_agent.gateway` 的当前架构权威，定义 Gateway 的稳定职责、生命周期、
核心不变量，以及 Gateway 与入口层、Assistant Runtime 和相邻系统之间的契约。

本文不记录以下易变细节：

- Media-Agent 的 wire 字段、ACK、流式 packet、H.264 约束和联调示例；
- 队列容量、超时、重试、采样周期等配置默认值；
- 类、路由和入口的逐项迁移状态；
- durable task worker、视频 observer、Provider adapter 等内部算法；
- 历史开发过程、阶段计划和验收记录。

这些内容分别以专项权威文档、源码、配置和测试为准。若本文与当前源码和测试不一致，
以源码和测试为准，并在同一次变更中回补本文。

相关权威入口：

- Media-Agent WebSocket：[media-agent-service-websocket.md](media-agent-service-websocket.md)
- Assistant/runtime/provider event stream：[runtime-event-stream-architecture.md](runtime-event-stream-architecture.md)
- Tool、MCP、durable task 和 Provider 治理：[tool-calling-architecture.md](tool-calling-architecture.md)
- Trace、日志与 redaction 契约：[observability-harness.md](observability-harness.md)
- 真实运行诊断：[observability-diagnosis-runbook.md](observability-diagnosis-runbook.md)
- Multi-agent、A2A 和 delegation：[agent-communication-routing.md](agent-communication-routing.md)

## 2. 架构边界

Gateway 是产品入口与 Assistant Runtime 之间的规范化生命周期边界，不是产品入口，
也不是第二套 Agent runtime。

```text
CLI / HTTP / Web UI / app / WebSocket / realtime media transport
        |
        v
entry adapter
  auth, transport IO, product/vendor schema, media edge, UX
        |
        v
Gateway
  identity, session, turn/run lifecycle, queue, cancel, reconnect, delivery
        |
        v
RealtimeAgentRequest / RealtimeAgentEvent / RealtimeAgentResult
        |
        v
GatewayRuntimeAdapter -> shared Assistant Runtime boundary -> AgentGraphRuntime / assistant loop
        |
        v
ActionValidator -> ToolExecutor -> ToolRegistry -> tools / providers / memory
```

### 2.1 各层职责

| 层 | 拥有 | 不拥有 |
| --- | --- | --- |
| Entry adapter | 鉴权、连接、协议解析、媒体解码、产品响应格式和 UI/TTS 行为 | Agent 规划、工具选择、记忆策略、Gateway 生命周期 |
| Gateway | session/run/cancel/interrupt/reconnect、排队准入、frame 投影和交付连续性 | 业务规划、Provider 选择、工具执行、长期任务执行 |
| Runtime adapter | Gateway request/event/result 与 Assistant Runtime 的薄映射、取消转发 | 独立 agent loop、入口协议和业务策略 |
| Assistant Runtime | assistant loop、模型调用、工具编排、上下文和运行终态 | WebSocket/vendor 协议和连接租约 |
| Governed services | 工具、Provider、memory、durable task、multi-agent 的各自治理链路 | Gateway frame 和入口 UX |

`AgentGraphRuntime` 和 assistant loop 始终是主执行器。Gateway-backed 产品入口、Media-Agent、
CLI 和 demo 不得复制或绕过主循环及工具治理边界。MCP、A2A 和 eval 可以作为明确的专项适配器
或 lower-layer contract probe，但不能成为产品入口绕过 runtime/tool governance 的先例。

请求/响应式入口可使用 `GatewayTurnFacade`：它把一次调用投影为规范化
`message.user`，等待该 run 的终态，再把 Gateway 结果交给入口层转换为自己的响应 schema。
Facade 不改变 Gateway 生命周期，也不把 provisional stream 重建成业务终态。

## 3. 生命周期模型

### 3.1 身份与对象

| 对象 | 含义 |
| --- | --- |
| `user_id` | 经可信入口绑定的用户身份，也是进程内连接 ownership 的作用域 |
| connection | 一次外部 transport 连接；可被替换或短暂断开 |
| AgentSession | Gateway 管理的逻辑会话，拥有 history、排队/活动 turn 和 session config |
| turn | 一次被 Gateway 接受的用户输入 |
| `run_id` | turn 的可取消执行生命周期及跨 Gateway/runtime/trace 的关联键 |
| durable `task_id` | Gateway run 结束后由 durable service 管理的长期任务身份，不是 `run_id` |
| durable `workflow_id` | 跨多个 work-item run 和进程重启的长阶段流程身份，不是 Gateway active run |

产品所说的 Agent instance 对应连接/用户拥有的逻辑 AgentSession，不对应一个专属
`AgentGraphRuntime` 实例。Runtime 可由应用池化并在不同 session 之间复用，但同一 session
的 backend run 不得重叠。

每个被接受的 `message.user` 在入口即获得稳定的 `turn_id` 和 `run_id`。缺失 ID 由
Gateway 生成；调用方提供的已接受 ID 是 opaque string，Gateway 不解析、重写或偷偷替换。

### 3.2 Session 生命周期

`GatewaySessionManager` 在单进程内按 `user_id` 管理有界的逻辑 session：

- acquire 已存在的 session 时复用其 endpoint、history、活动/排队 turn 和 live config，并刷新活跃状态；
- 不存在时创建并初始化新 session；首次 turn 只能在 session 初始化边界完成后 dispatch；
- 可信 config update 可更新在线 session；session 尚未创建时可延后到后续 acquire 应用；
- transport supersede 不销毁 session，真实 disconnect 在 reconnect grace 内也只改变 delivery 状态；
- idle eviction、显式 hangup、manager destroy 或应用 shutdown 才结束相应逻辑 session，并清理其资源；
- 销毁逻辑 session 不等于关闭应用拥有的 runtime pool 或其他用户的 session。

Session 的 acquire/create 和 destroy 必须产生一致、prompt-safe 的生命周期事实；config update
只承诺更新或延后保存可信配置，不要求写入 lifecycle sink。只读活动状态查询不得隐式创建 session、
刷新 idle timer 或中断活动 run。

### 3.3 Turn/run 状态

```text
accepted
   |
   v
queued ---------> cancelled / expired
   |
   v
admitted -> active -> completed / failed / cancelled
```

- `followup` 是普通 turn 的默认模式，按同 session FIFO 等待。
- `replace` 或显式 interrupt 先取消活动 run；replacement 只能在旧 backend 实际退出并释放
  准入资源后开始，不能形成同 session backend overlap。
- 排队 turn 是具名、可观察、可取消的生命周期对象，不是匿名 payload。
- Gateway 同时限制每 session pending、全进程 queued、活动 backend run 和 runtime pool；
  所有容量必须有界。
- 队列等待与 backend 执行 deadline 是不同阶段。尚未 admission 的 turn 不调用 backend，
  也不写入已执行的会话历史。
- 溢出、身份冲突、重复身份冲突和等待超时必须产生结构化结果；不得静默丢弃或合并已接受输入。

### 3.4 终态与可见性

Gateway 为每个开始的 run 发出稳定的 started/stream/progress/terminal 生命周期；一个 run
只能产生一个权威终态。

- 对未被 Gateway 控制面覆盖的普通 backend run，backend result 决定 completed 或 failed。
  已接受的 cancel 优先于晚到的 backend completed/error；Gateway control turn 也可以不调用 backend
  而直接完成。
- 单个可恢复的 `tool.failed` 不等于 run failed；若 assistant loop 消费失败 observation 后正常
  回答，Gateway 仍结束为 completed。
- `run.end` 携带规范化最终文本和可用的 prompt-safe correlation。最终文本来自 backend result，
  不从 `stream.chunk` 拼接推断。
- cancel 一经接受，旧 run 的 user-visible/speakable output gate 立即关闭。之后到达的 Provider、
  tool 或 runtime 结果只能作为 trace 或明确允许复用的 artifact，不能重新打开旧输出。
- `call.hangup` 立即结束逻辑 session 并清理其排队/活动工作，但不关闭应用拥有的 runtime pool。

### 3.5 可信配置

身份、entry profile、system prompt profile、channel capability 和 media capability 必须来自鉴权
上下文或可信 session config。普通用户文本和任意 message metadata 不能提升权限、改变 profile，
或决定工具候选空间。

`gateway.capabilities` 只定义通用 `EntryAdapterCapabilities` 数据类型；HTTP、规范化 Gateway WebSocket
和 Agent-Service 的具体 capability 实例分别由各自 API entry adapter 定义。Gateway package 不导出
也不命名 Media-Agent profile。

Gateway 的 queue、dedupe、admission、session、connection relay 和默认 runtime pool 状态均为
进程内状态。它们不提供跨进程一致性、重启恢复或 durable delivery。

## 4. Connection ownership 与恢复

规范化 Gateway WebSocket 在单进程内对每个 `user_id` 只允许一个当前 delivery owner，
并由一个 session relay 独占读取 runtime endpoint：

```text
Gateway session endpoint -> one SessionRelay -> current ConnectionLease
                                           `-> bounded cursor outbox
```

新的同用户连接 supersede 旧 owner 时，只转移交付租约：

- 活动 run 和 AgentSession 保持不变；
- 旧连接不得因为被替换而触发 disconnect cancellation；
- 后续 frame 由新 owner 接收；
- 不允许多个 bridge 竞争消费同一个 session endpoint。

真实的当前 owner transport disconnect 会把 delivery 置为 `DETACHED`，而不是立即取消 run。
Gateway 在有界 grace period 内保留有界、cursor-addressable outbox。`session.resume` 只恢复仍存在
的进程内 session，并重放 cursor 之后仍被保留的 frame；它不能复活已经终态的 run。

若 grace 到期仍无 owner，Gateway 取消当时仍活动的 run。显式 hangup 不使用 reconnect grace。
该机制不是 durable queue，也不承诺跨进程、跨 worker 或服务重启后的恢复。

`/agent-service/v1` 当前仍以每个 vendor WebSocket connection 创建独立内部 Gateway session；
在 vendor 协议提供稳定可信的 resume identity 和 cursor 前，不继承上述跨连接恢复语义。

## 5. Cancel、interrupt 与 arbitration

### 5.1 明确控制

`run.cancel`、hangup、显式 `replace`、媒体 interrupt 等结构化控制信号由 Gateway 立即应用，
不经过 LLM 判断。Gateway 拥有取消意图、目标 run 校验、queued/active 状态转换和 stale-output
gating；Assistant Runtime 在安全 checkpoint 协作停止 Provider/tool 执行。

取消不能回滚已经提交的外部副作用。Provider 或 tool 可能在取消请求后才返回，但其晚到结果
不得成为旧 run 的可见输出。

### 5.2 Semantic interrupt

Semantic interrupt 是可选、可信 capability gated 的控制面，用于判断新 utterance 与活动 run
的关系；它不是第二套业务 runtime，也不是 Media-Agent wire 协议的一部分。

只有 feature flag、entry capability、session config 和活动 run 同时允许时，普通 utterance 才能
进入 `RealtimeTurnArbiter`。显式 control 或显式 turn mode 始终绕过 semantic arbitration。

Arbiter 只接收有界、prompt-safe 的任务状态投影，并返回结构化 disposition。需要取消或替换活动
run 的 disposition 只有在 `expected_run_id` 仍匹配时才能改变活动 run：

- followup/uncertain：保持 FIFO，活动 run 继续；
- ack/no-op：新 turn 无 backend 完成，活动 run 继续；
- cancel-only：取消活动 run，新 turn 无 backend 完成；
- revise/replace：取消活动 run，新 turn 等待旧 backend 退出后再开始。

不匹配的 cancel/revise/replace decision 必须降级为普通 followup；ack/no-op 和保守的
followup/uncertain 不改变活动 run，不依赖该匹配条件。低置信度、超时、Provider 错误、非法结果或
控制面饱和也必须保守回退，不能取消当前 run。Gateway 不支持向正在执行的 runtime 注入新 prompt
或原地修改目标；这类 live steering 需要独立的 mailbox、checkpoint、side-effect barrier 和
output versioning 契约。

TTS pause/duck/resume 属于 media entry adapter。Gateway 只提供文本/run 的 speakable 与
stale-output 语义，不直接控制仓库外的 TTS provider。

## 6. Runtime adapter contract

Gateway 与 Assistant Runtime 之间的公共契约由以下类型组成：

- `RealtimeAgentRequest`：一次规范化 turn，携带绑定后的身份、ID、文本、媒体引用和可信 metadata；
- `RealtimeAgentEvent`：progress、tool lifecycle、response chunk 和 error 等流事件；
- `RealtimeAgentResult`：completed/cancelled/error 终态、最终文本、trace 和 output refs；
- `RealtimeAgentBackend`：由 `GatewayRuntimeAdapter` 实现的 backend protocol；
- `RealtimeCancelToken`：从 Gateway 向 runtime 传播的协作取消边界。

`GatewayRuntimeAdapter` 只做 request/event/result 映射和取消转发。它不拥有 planning、tool choice、
memory policy、Provider policy、agent routing 或 multi-agent decisions。

稳定的事件投影原则：

| Runtime 语义 | Gateway 语义 |
| --- | --- |
| response token/chunk | provisional、append-only 的 `stream.chunk` |
| user-visible progress | 可替换、非最终答案的 `event.progress` |
| tool lifecycle | 不可朗读的 `event.tool` / trace state |
| backend result | 唯一权威 `run.end` |

`run.end` supersede 同一 run 的 progress slot。Progress、tool event 和 provisional chunk 不写入
conversation final state 或长期记忆；规范化 backend result 才是最终业务状态。

Gateway 创建的 `run_id` 原样传入 Assistant Runtime，并用于 lifecycle 与 trace correlation。
Runtime-owned `trace_id` 在可用时回传；若 backend 尚未创建 trace，入口和 Gateway 必须明确报告
不可用状态，不能伪造 ID。完整 trace、日志字段、redaction 和内容访问规则以
[observability-harness.md](observability-harness.md) 为准。

## 7. 相邻系统边界

### 7.1 Media-Agent

`/agent-service/v1` 是 vendor media entry adapter。它保留 vendor envelope，处理连接、媒体校验、
ACK、流式交付和 H.264 解码，并把 chat、interrupt 和稳定媒体引用投影到 Gateway/runtime。
入口层不得直接调用视觉 Provider，也不得把 raw frame、路径、base64 或 Provider 原始响应送入
主 LLM。完整 wire contract 只维护在
[media-agent-service-websocket.md](media-agent-service-websocket.md)。

`image_generation` runtime 结果保持为受管 artifact/output ref；只有 Agent-Service entry adapter
把它投影为 `IMAGE` 并复用媒体服务已建立的 `/agent-service/v1` WebSocket 发送标准
`chatResponse`。HTTP Agent client 和通用 Gateway WebSocket 不做该媒体投影。媒体服务是
中继而非渲染服务：其 `RenderingClient` 再通过 HTTP POST 把完整响应转发到渲染服务
`/rendering/v1/torender`。Agent 与渲染服务没有任何 HTTP 或 WebSocket 直连。模型驱动的 3D 生成
仍从主 runtime 经受治理 `image_to_3d` Tool 发起；该 Tool 创建独立 3D job、把本地图片提交给 3D
服务，并返回 `job_id`。Runtime、Tool 和 job 不接收入口类型或投递策略；Gateway Core 也不认识
Media-Agent wire schema、工具名称或 3D callback payload。

Gateway 拥有媒体无关的 `GatewayArtifactDeliveryHub`：完成适配器保存中性 artifact 后，只发布
`artifact.completed(artifact_id,user_id,session_id,media_type,uri,inline_data)`。入口 adapter 自主决定
是否按 runtime session 注册 subscriber。Agent-Service 在活动媒体连接上注册 subscriber，并在订阅者
内部投影为 `TD_MODEL`、`VIDEO` 或 `IMAGE`；HTTP Agent client、CLI、UI 和通用 Gateway WebSocket
不注册媒体 subscriber，因此只通过 owner-bound
`GET /agent/image-to-3d/jobs/{job_id}` 查询结果，不要求媒体连接。当前 job registry 是单进程内存状态；
callback 不进入 Gateway run、不调用 LLM，也不复制 Agent 规划。Agent 不下载或解析模型/视频 URL
指向的产物。完整边界以
[media-agent-service-websocket.md](media-agent-service-websocket.md) 为准。

### 7.2 Durable task

Gateway 只拥有“接受并提交长期任务”的前台 ingress run。提交成功后，该 Gateway run 正常终结；
durable `task_id`、checkpoint、lease、wait、resume、notification 和 worker lifecycle 由
`DurableTaskService` 及其治理边界拥有。

Durable task 不进入 Gateway active-run map，不复用 Gateway followup queue，也不保持长期
WebSocket/coroutine。其具体契约以
[tool-calling-architecture.md](tool-calling-architecture.md)、
[runtime-event-stream-architecture.md](runtime-event-stream-architecture.md)、源码和测试为准。

通用 Durable Workflow 遵循相同 connection boundary。Gateway 只承载触发 `workflow_submit` 的
ingress run；提交成功后不把 `workflow_id` 放入 active-run map、不占用 followup queue，也不要求原
WebSocket 保持连接。后续 status/events/input/cancel 由 identity-scoped `/workflows` facade 调用
`WorkflowService`，不能直接读取 SQLite 或 artifact 文件。后台 work item 的独立 run 不重新打开已
结束 ingress run 的输出门。

### 7.3 Multi-agent 与工具

需要 delegation 时，前台 turn 仍先进入主 `AgentGraphRuntime`，再通过受治理的 agent communication
或 tool boundary 委派。Worker agent 不理解 Gateway frame、connection 或 vendor payload。
所有本地显式工具副作用仍必须经过
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。

### 7.4 Observability

Gateway 记录 prompt-safe 的 session、queue、admission、run、cancel、delivery 和 terminal 生命周期。
可读日志和 trace sink 必须 fail-open：观测写入失败不能改变 frame、排队、取消或终态行为。
用户正文、媒体 bytes、Provider 原始响应和敏感标识不得进入默认日志。

## 8. 源码导航

| 边界 | 主要源码 |
| --- | --- |
| Frame 与 transport | `src/assistant_agent/gateway/protocol.py`、`transport.py`、`ws.py` |
| Session、queue 与 admission | `session.py`、`queueing.py` |
| Connection ownership 与 replay | `bridge.py` |
| Cancel 与 semantic arbitration | `cancellation_models.py`、`turn_arbitration*.py`、`realtime_turn_arbiter.py` |
| Runtime contract 与映射 | `runtime_types.py`、`runtime_backend.py`、`runtime_adapter.py`、`*_mapping.py` |
| Delivery、progress 与 facade | `delivery.py`、`progress.py`、`turn_facade.py` |
| FastAPI entry adapters | `src/assistant_agent/api/gateway_runtime.py`、`gateway_websocket.py`、`agent_service_websocket.py`、`rendering_3d_callback.py` |

核心 Gateway 生命周期和 frame contract 由 `tests/core/contract/test_gateway_contract.py` 保护。
只有稳定 core invariant 改变或确认存在保护缺口时才扩展永久 core 测试；Media-Agent 等具体功能
测试按 [tests/README.md](../tests/README.md) 进入对应 TDD 或 eval 区域。

旧 `/home/lenovo1/pycharm_project/runTime` 只用于 frame 和生命周期兼容性参考。不得导入旧包、
复制旧 agent loop，或让旧实现覆盖本项目当前源码、测试和本文的架构决定。

## 9. 更新规则

- 只有跨入口、跨实现仍必须成立的 Gateway 职责、契约和不变量进入本文。
- Wire 字段、配置默认值、类清单、迁移状态、调试命令和验收证据放在其专项权威或源码中。
- 新能力先判断归属；不要把 Provider、tool、memory、durable task、media 或 multi-agent 内部设计
  塞入 Gateway 文档。
- 不在 `docs/development/**` 或 `docs/superpowers/**` 建立并行当前权威；这些目录只保存历史设计、
  计划或明确点名的 runbook。
- 新增、删除或重命名 Gateway 权威入口时，同步更新 `AGENTS.md`、`README.md` 和相关 specialty skill。
