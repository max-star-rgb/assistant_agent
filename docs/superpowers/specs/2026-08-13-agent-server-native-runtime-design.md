# Agent Server 原生运行时重构设计

日期：2026-08-13

状态：待用户审阅

## 1. 背景与目标

当前生产入口由项目自建 FastAPI application 承载，`/agent-service/v1`、HTTP 和通用 WebSocket
入口经过 `assistant_agent.gateway` 的 session、queue、admission、runtime pool、run、cancel、stream
和 delivery 生命周期，再调用同进程内 `AgentGraphRuntime`。这套结构形成了一个进程内、非持久的
简化 Agent Server。

目标是把所有生产 Graph 执行迁移到官方 Agent Server，直接采用其 auth、assistants、threads、runs、
task queue、cancel、stream resume、checkpointer 和 Store 能力。媒体服务暂时维持现有
`/agent-service/v1` WebSocket 协议，由挂载在 Agent Server 上的 custom FastAPI route 做薄协议适配。

本重构不是把现有 Gateway 改名或远程化，而是删除与 Agent Server 重复的 Memory/Execution Runtime
能力。最终每种事实只有一个权威：

```text
Agent Server：身份授权、thread、run、queue、执行终态、checkpoint、Store
Assistant Graph：assistant/tool/memory 的领域逻辑
媒体兼容适配：现有 wire 解析、原生 run 投影、媒体交付与 ACK
媒体服务：音视频采集、播放、渲染及其客户端状态
```

## 2. 非目标

- 本阶段不修改媒体服务的外部 wire schema。
- 不在兼容适配层重新实现 queue、run 状态机、checkpoint、长期记忆或 runtime pool。
- 不把 Mem0 强行包装为 `BaseStore`。
- 不要求第一阶段删除所有旧 HTTP、CLI、eval 入口；但生产 Graph 执行不得继续绕过 Agent Server。
- 不把 Agent Server 的 run 完成等同于媒体已发送、已播放或已 ACK。
- 不依赖 Agent Server 未公开的 Python 内部 API。

## 3. 目标架构

```text
Media-Agent
    │ existing /agent-service/v1 WebSocket
    ▼
Agent Server custom FastAPI route
    ├── media envelope/schema validation
    ├── trusted identity normalization
    ├── conversation/thread correlation
    ├── media input -> graph input
    ├── native stream -> media response
    └── media interrupt/ACK projection
             │ public Agent Server SDK/HTTP contract
             ▼
Agent Server
    ├── custom auth + resource authorization
    ├── assistants
    ├── threads / runs
    ├── queue / workers / multitask strategy
    ├── cancel / join / resumable stream
    ├── checkpointer
    └── BaseStore
             │ injected runtime resources
             ▼
Assistant StateGraph
    memory_recall
        ↓
    assistant / tool loop
        ↓
    publish_response
        ↓
    memory_commit
```

Agent Server 通过 `langgraph.json` 加载稳定 compiled graph 或必要的轻量 graph factory，并自动注入
checkpointer 和 Store。Graph 代码不得在生产部署中自行拥有这两个资源。

custom route 与原生 Agent Server API 位于同一 deployment，但它只是协议客户端/适配器。它调用官方
`langgraph_sdk` 或公开 HTTP endpoint 创建、跟随和取消原生 run。若使用同机 loopback client，必须通过
可删除的 deployment probe 验证，不得导入 `langgraph_api` 私有模块。

## 4. 身份与资源模型

### 4.1 标准身份

```text
authenticated principal  用户或代表用户调用的服务身份
owner / tenant            Agent Server resource authorization scope
thread_id                 持久对话与 checkpoint 容器
run_id                    一次原生 Graph invocation
media_connection_id       一次媒体 WebSocket 连接
vendor sessionId          原样回传的协议关联值
chatIndex                 一次媒体 chat 的协议关联值和幂等输入
delivery_id               一次媒体终包交付/ACK 身份
```

`connection_id`、vendor `sessionId` 和 `thread_id` 不得互相冒充。第一阶段若媒体协议没有稳定的
conversation identity，兼容 route 可以保持“一次 WebSocket 连接对应一个 thread”的现有语义；该映射必须
显式记录为兼容策略，不能假称跨连接对话恢复。后续只有媒体协议提供可信 conversation id 后，才启用跨连接
复用 thread。

### 4.2 多用户授权

Agent Server custom auth 验证媒体服务或终端用户凭证，`@auth.on` 为 thread、run、assistant 和 Store
资源写入并强制 owner/tenant filter。Graph 从受信 runtime auth context 获取 user/tenant，不从用户文本、
任意 metadata 或 vendor `number` 自动提升权限。

服务身份代用户调用时，媒体服务必须提供受签名且可验证的 subject/tenant claim；裸 `userNumber` 只能作为
业务 correlation，不能单独构成生产身份认证。

## 5. 媒体协议到原生资源的映射

| 媒体协议行为 | Agent Server 行为 |
| --- | --- |
| `assistantControl` | 验证/建立连接上下文，按兼容策略创建或取得 thread |
| `chat` | 在该 thread 创建原生 run，输入为严格 Graph input schema |
| `stream=true` | 消费原生 run/thread stream，并投影为 `chatResponse PROCESSING/SUCCESS/FAIL` |
| 后续 `chat` | 使用原生 `multitask_strategy`，默认 `enqueue`；产品明确打断时用 `interrupt` 或 cancel |
| `interrupt` | 精确取消/中断该连接拥有且仍活动的原生 run |
| WebSocket 断开 | 不伪造 run 终态；是否 cancel 由明确媒体策略决定 |
| route 到 Agent Server stream 断开 | 通过原生 join/thread stream 和 `Last-Event-ID` 恢复 |
| `chatResponseAck` | 更新媒体 delivery 状态，不改写 run/checkpoint 状态 |
| hangup | 结束媒体连接；是否取消活动 run 由协议策略决定，不删除长期 thread/store |

兼容 route 应尽量消费稳定的 message/custom/lifecycle stream event。它不得读取完整 Graph state 来猜测
输出，也不得从 token delta 拼装权威最终业务结果；最终结果来自原生 run/thread 的持久终态或 Graph 的
稳定输出 schema。

## 6. Graph 与 Runtime 重构

### 6.1 Agent Server deployment graph

新增 Agent Server composition root，只导出可部署 Graph：

- 使用当前 `AssistantTurnState` 和真实 conditional edges；
- Provider、Tool governance 和 memory node 逻辑继续属于 Graph；
- checkpointer 与 LangMem `BaseStore` 由 Agent Server 注入；
- runtime context 只承载身份、权限、provider/tool service 等非 checkpoint 对象；
- 不创建 `RuntimeHost`、本地 saver owner、`GatewayRuntimePool` 或进程内 run claim 作为生产执行入口；
- 外部副作用幂等 ledger 继续存在，因为 checkpoint 不能提供外部 API exactly-once。

### 6.2 Memory

`memory_context` 继续是一次 logical turn 的冻结 Graph State 快照。LangMem backend 使用 Agent Server
注入的 `runtime.store`；Mem0 backend 在 memory node 内直接调用 SDK/API。是否存在 Store 是后端装配细节，
不是 Graph State 字段。

`publish_response` 仍位于 `memory_commit` 之前；用户可见答案不等待长期记忆提交。replay/fork 的 recall 与
commit 限制继续由 Graph state provenance 和节点策略约束。

### 6.3 Tool 与 Provider 治理

Agent Server 只接管执行宿主，不取代项目的领域安全链路。所有本地工具副作用继续经过：

```text
ActionValidator -> ToolExecutor -> ToolRegistry -> tool
```

custom route、Agent Server auth metadata、thread metadata 和用户文本都不得绕过该链路或扩大 Tool catalog。

## 7. Gateway 退役边界

当相应入口已切换并通过原生 deployment 验证后，下列生产能力应删除：

- `GatewayRuntimePool`；
- `GatewayRunAdmissionController` 和 Gateway backend queue；
- `GatewayRuntimeAdapter` / `RealtimeAgentBackend` 本地执行边界；
- `GatewayTurnFacade`；
- `GatewaySessionManager` 中的 Graph session/run 权威；
- 进程内 HTTP stream registry 和 response capture；
- Gateway Graph frame outbox/reconnect；
- 产品 FastAPI composition root 对 `AgentGraphRuntime` 的直接持有和调用。

下列能力按最小范围迁移到 `agent_server/media_adapter`（最终命名在实施计划中以现有包依赖最小化决定）：

- `/agent-service/v1` envelope 与 body schema；
- `assistantControl/chat/audio/video/interrupt/chatResponseAck` 协议处理；
- media input/reference 归一化；
- Agent Server stream 到媒体 frame 的机械投影；
- delivery id、已发送/已 ACK 状态；
- 视频 ingestion、渲染 callback 等非 Graph 媒体边缘逻辑。

不得把上述适配包继续命名或设计成通用 Agent Gateway。

## 8. 断线、取消和幂等

### 8.1 两个断线域

1. custom route 与 Agent Server 原生 stream 的订阅断开：run 继续由 Agent Server 执行，适配器使用
   `Last-Event-ID` 或公开 join API 恢复。
2. Media-Agent WebSocket 断开：这是媒体交付断线。第一阶段保持现有协议约定所需的 cancel 策略；不得据此
   删除 thread、checkpoint 或长期记忆。

### 8.2 幂等

- Agent Server `run_id` 是执行权威，不再生成平行 Gateway run id。
- `chatIndex` 与连接/thread scope 组成媒体请求幂等键；重复帧不得创建重复 run。
- tool/memory 外部副作用继续使用各自最小 operation/event ledger。
- delivery ACK 只影响 delivery ledger，不作为重新执行 Graph 的依据。

### 8.3 终态

```text
run terminal       Agent Server 权威
graph output       Graph output schema 权威
delivery terminal  媒体适配/媒体服务权威
```

任何一方不得用自己的局部事实推断另一方已经完成。

## 9. 错误处理与可观测性

- auth/authorization 错误在创建 thread/run 前拒绝；
- 原生 queue、run、cancel 和 stream 错误映射为稳定媒体 `FAIL/error`，同时保留 prompt-safe correlation；
- Provider/tool/memory 错误继续由 Graph 归一化，不由 custom route 解释内部语义；
- trace 以 Agent Server `thread_id/run_id` 为主关联键，媒体 `chatIndex/delivery_id/connection_id` 作为附加
  correlation；
- 日志不得记录凭证、原始 provider payload、用户媒体正文或未脱敏 WebSocket close reason；
- custom route 的观测只记录协议接入、投影和交付事实，不复制 Graph node/Tool 生命周期。

## 10. 分阶段迁移

### 阶段 0：原生能力 probe

在 mock/offline 模式建立可删除的 Agent Server deployment probe，验证：

1. `langgraph.json` 能加载项目 Graph；
2. Agent Server 自动注入 checkpointer 和 Store；
3. custom FastAPI WebSocket route 可用；
4. custom route 能通过公开 SDK/HTTP 契约在本 deployment 创建 thread/run；
5. 能流式接收 Graph message/custom/lifecycle event；
6. stream 断开能从 event id 恢复；
7. cancel 和 `enqueue/interrupt` 符合预期；
8. custom auth 和 owner filter 隔离两个测试用户；
9. 服务重启后 thread/checkpoint/store/run 状态符合官方持久语义。

probe 不通过时先修正设计，不用项目自研 runtime 填补平台能力。

### 阶段 1：Graph deployment composition

导出 Agent Server graph，消除生产 graph 对本地 saver/runtime owner 的硬绑定，接通 runtime auth context、
Tool governance、LangMem Store 和 Mem0 node。

### 阶段 2：媒体 custom route

把当前 `/agent-service/v1` 的协议 schema、媒体 handler 和输出投影迁移到 Agent Server custom app。先保持
wire fixture 完全兼容，再将内部执行替换为原生 thread/run/stream/cancel。

### 阶段 3：入口切换

媒体服务只连接 Agent Server deployment；旧 FastAPI/Gateway 路径停止生产流量。HTTP、CLI、demo 和 eval
分别切换为 Agent Server SDK 客户端或明确的离线 Graph 测试入口。

### 阶段 4：删除并收口

删除不再被任何生产入口使用的 Gateway Runtime、local composition root、重复 session/run/queue/stream
抽象；同步更新 authority、README、scripts、deploy 和测试边界。

## 11. 验证策略

验证以“原生事实是否只有一个权威”为中心：

- Graph/node 离线测试：状态 schema、Tool governance、memory snapshot/commit；
- Agent Server deployment integration：auth、threads、runs、queue、cancel、stream resume、checkpoint、Store；
- 媒体协议 contract：现有 wire fixtures、stream sequence、终包、interrupt、ACK、音视频 handler；
- 故障注入：custom route 订阅断开、媒体 WebSocket 断开、worker 重启、重复 `chatIndex`、取消竞态；
- 多用户隔离：用户 A 无法读取、搜索、取消用户 B 的 thread/run/store；
- 删除审计：生产源码不再直接调用 `AgentGraphRuntime.invoke/astream`，不再存在第二套 queue/run terminal。

真实 Provider 不是结构迁移的默认验收条件。默认全部使用
`MULTIMODAL_AGENT_PROVIDER_MODE=mock`；真实 Provider 只在用户明确授权的后续 system/release 验证中启用。

## 12. 完成标准

只有以下条件同时成立，重构才算完成：

1. 所有生产 Graph 只由 Agent Server worker 执行；
2. `/agent-service/v1` 外部协议保持兼容；
3. custom route 只通过公开 Agent Server 契约操作 thread/run/stream/cancel；
4. Agent Server 是 auth、thread、run、queue、checkpoint 和 Store 的唯一生产权威；
5. 现有 Gateway Runtime 的重复能力已从生产代码删除，而非旁路保留；
6. LangMem 使用 Agent Server 注入 Store，Mem0 继续由 memory node 直接调用；
7. Tool、Provider、Memory 的项目治理边界没有被入口绕过；
8. 多用户隔离、stream 重联、cancel、重复输入和服务重启具有 deployment-level 验证证据；
9. authority 文档、scripts、deploy 和默认测试入口与新架构一致；
10. mock/offline 核心测试和 Agent Server 集成测试通过。
