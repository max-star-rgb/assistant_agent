# Gateway QueuePolicy 与全局准入设计

## 文档状态

本文档定义 `assistant_agent` 的 Gateway QueuePolicy v1、queued-turn 生命周期和进程级 run 准入控制。总体架构、首版模式和协议方向已经由用户确认。

本文档只描述设计，不授权真实 Provider 调用，不改变默认 mock/local/offline 策略，也不实现 Hermes 式 durable task、OpenClaw `collect`/`steer` 或第二套 Agent loop。

## 1. 背景与结论

当前 `GatewaySessionService` 已经具备基础的同 session 串行行为：

- active run 期间收到普通 `message.user` 时，将 `endpoint + frame` 放入 `_pending_by_session`。
- active run 结束后，从 pending list 取出下一条消息。
- 显式 interrupt 会取消当前 run 并启动新 turn。
- hangup、disconnect、deadline 和 `run.cancel` 已有合作式取消语义。
- lifecycle sink 已记录 `gateway.run.queued` 和终态事件。

当前实现仍有以下缺口：

- pending list 无容量上限、等待超时和全局背压。
- queued message 在出队前没有稳定的 Gateway-owned `turn_id/run_id`。
- `run.cancel` 只能定位 active run，不能取消 queued turn。
- 没有跨 session 的进程级 backend run 并发限制。
- history、deadline 和 queued lifecycle 仍以“开始执行时才存在 run”为前提。
- 没有重试去重键；网络重发可能产生重复 turn。
- 队列状态只进入内部 lifecycle sink，客户端无法区分 session busy 与 global capacity wait。

首版采用增量式两级调度：

```text
message.user
    |
    v
GatewayQueuePolicy
identity / dedupe / capacity / timeout
    |
    v
per-session queue
one session head, FIFO followups
    |
    v
GatewayRunAdmissionController
process-wide fair active-run cap
    |
    v
GatewayAgentAdapter -> AgentGraphRuntime
```

核心结论：

- 同 session 串行与跨 session 全局并发是两个独立约束，必须分层处理。
- queued turn 在入口处获得稳定身份，排队期间也是可观测、可取消的 Gateway run。
- 默认保留现有 `followup` 行为；显式 `interrupt` 保留，但不允许造成同 session backend 重叠。
- 首版拒绝 newest overflow，不静默丢弃或总结已经接受的用户消息。
- 全部状态只保证单进程生命周期，不宣称重启恢复或跨进程一致性。

## 2. 参考项目映射

### 2.1 Hello Claw / OpenClaw

Hello Claw 第 5 章强调持续任务中的顺序、状态和上下文必须被显式维护，并区分 `collect`、`followup`、`steer` 与 `interrupt`。OpenClaw 当前 command queue 进一步采用 per-session lane 加 process-wide lane，对 queued run 保留 cancel identity，并提供 cap、drop 和 debounce。

本项目借鉴：

- per-session serialization 与 process-wide admission 分层。
- queued turn 拥有稳定取消身份。
- 队列容量、等待时长和 lifecycle 可观测。
- mode 是协作语义，不是简单的并发开关。

本项目暂不借鉴：

- `collect` 的消息折叠和 overflow summarize。
- `steer` 的运行中 prompt 注入。
- cron、heartbeat、subagent lane 一次性进入同一个通用 scheduler。
- session command 动态扩大服务端并发和容量限制。

原因是当前 runtime 尚未提供安全 steer boundary，`collect` 也会立即引入多消息身份、媒体合并和单条取消语义。首版先保证可预测和可复盘。

### 2.2 Hermes Agent

Hermes Agent 的 gateway/CLI 提供 `queue`、`interrupt` 和 `steer` busy-input mode，并对 busy input 给出即时反馈。Hermes 还提供独立 background session 和 task identity。

本项目借鉴：

- busy input 的显式状态反馈。
- queued followup 与 interrupt 的产品语义分离。
- 后台任务应有独立 session/task identity，而不是伪装成普通前台 turn。

本项目不照搬：

- Hermes 的 gateway 巨型进程和 platform-specific message delivery 结构。
- background agent 直接继承完整 toolset 的默认行为。
- 将 durable task、消息队列、Provider 路由和工具执行合并到同一对象。

### 2.3 当前项目优势

QueuePolicy 必须服从本项目已有边界：

- Gateway 只管理消息、session、run、cancel、interrupt 和 stream lifecycle。
- `AgentGraphRuntime` / assistant loop 仍是唯一主执行器。
- 工具调用继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry`。
- Gateway cancel 不回滚已经提交的工具副作用，也不自行判断 artifact 可复用性。
- 多 Agent 仍通过 `delegate_to_agent`、AgentCommunicationService 和 policy/transport 边界。
- Proactive Wake 继续拥有独立 coordinator 和预算，不把 scheduler 放入 Gateway。

## 3. 目标与非目标

### 3.1 目标

- 保证同一 session 任意时刻最多一个 backend run 正在执行。
- 为所有接受的 turn 在入口处生成稳定 `turn_id/run_id`。
- 支持 queued turn 的定向取消、超时和结构化终态。
- 限制每 session pending 数量、全进程 queued 数量和 active run 数量。
- 在 session 之间提供 FIFO 公平的进程级准入。
- 提供 prompt-safe 的 queue depth、queue reason 和 wait duration 观测。
- 保持现有 Gateway wire frame、Realtime backend contract 和入口收敛路径兼容。
- 为未来 `collect`、lane 和 durable task 保留接口边界，但不提前实现。

### 3.2 非目标

- 跨进程或重启后的 queued turn 恢复。
- Redis、Kafka、Celery 或通用 job platform。
- OpenClaw `collect`、`steer`、`steer-backlog` 或 overflow summarize。
- Hermes background task/Kanban 状态机。
- Gateway 内的 cron、Proactive Wake tick 或 Provider scheduler。
- active run 的强制线程终止或工具副作用回滚。
- 改变 `AgentGraphRuntime`、tool scheduler、Memory 或 AgentRouter 的核心行为。
- 为 `/agents/run`、A2A 或 MCP `tool_run` 自动套用 Gateway permit。

## 4. 方案选择

### 4.1 采用：现有 Gateway 上的两级调度

在 `GatewaySessionService` 保留 session lifecycle 编排，引入独立纯策略/状态组件和 manager-owned 全局准入控制器。

优点：

- 改动集中在现有 Gateway 边界。
- 可以复用当前 cancel token、event mapping、history 和 facade。
- 单元测试可以独立覆盖 policy/controller，再做 session 集成测试。
- 不引入外部依赖。

代价：

- 仍是单进程内存状态。
- `GatewaySessionService` 需要做一次有针对性的 queued-turn 重构。

### 4.2 不采用：每 session Actor/Mailbox 重写

Actor 模型长期边界清晰，但会同时改写 session service、bridge、history、interrupt 和 reconnect，迁移面过大，不适合作为当前增量。

### 4.3 不采用：durable broker 优先

持久 broker 能提供恢复和多进程 worker，但实时会话 turn 与长期任务具有不同的取消、延迟和上下文语义。当前先引入 durable broker 会把 Hermes task 问题错误地前移到 Gateway。

## 5. 总体架构

```text
GatewaySessionManager
  owns one shared GatewayRunAdmissionController
            |
            +---------------------+
            |                     |
            v                     v
 GatewaySessionService(u1)  GatewaySessionService(u2)
  session s1 queue           session s2 queue
  session s3 queue           session s4 queue
            |                     |
            +----------+----------+
                       v
       process-wide fair admission waiters
                       |
             max_active_runs permits
                       |
                       v
       GatewayAgentAdapter / AgentGraphRuntime
```

每个 session 只向全局准入控制器提交自己的 head turn。一个 session 的 run 结束并释放 permit 后，该 session 的下一个 turn 才能进入全局 waiter 尾部。这样可以避免单个高频 session 连续占满全部 permit。

`GatewayRunAdmissionController` 同时管理：

- 全局 queued reservation 上限。
- 等待 permit 的 FIFO ticket。
- active permit 计数。
- close/cancel 时的 ticket 清理。
- prompt-safe snapshot，用于 lifecycle 和测试。

它不读取消息正文、不构建 prompt、不调用 backend。

## 6. 核心模型

### 6.1 GatewayQueuePolicy

```python
@dataclass(frozen=True)
class GatewayQueuePolicy:
    mode: Literal["followup", "interrupt"] = "followup"
    max_pending_per_session: int = 8
    max_queued_turns_global: int = 64
    max_active_runs: int = 4
    queue_wait_timeout_ms: int = 120_000
    dedupe_ttl_s: float = 300.0
    dedupe_max_entries_per_user: int = 1024
    overflow_policy: Literal["reject_newest"] = "reject_newest"
```

约束：

- 所有数量和 timeout 必须为正数。
- `max_active_runs <= max_queued_turns_global` 不是必要约束；active run 不计入 queued 数量。
- v1 只允许 `reject_newest`，不接受未知值后静默回退。
- v1 资源上限只从进程启动配置读取，不接受 session 动态修改容量或 timeout。
- 普通 `message.user.metadata` 不能改变 policy。
- 现有 `interrupt=true`、`metadata.control=interrupt` 和可信 `interrupt_policy` 继续作为显式控制语义。

mode 只决定“session 已 busy 时新消息如何处理”，不影响空闲消息。解析优先级固定为：消息上的显式 interrupt、可信 session `interrupt_policy`、应用级 `GatewayQueuePolicy.mode`。mode 从 `followup` 改为 `interrupt` 是交互语义选择，不是资源限制；无论使用哪种 mode，都不能覆盖 process 级容量上限。

### 6.2 QueuedTurn

```python
@dataclass
class QueuedTurn:
    user_id: str
    session_id: str
    turn_id: str
    run_id: str
    endpoint: Endpoint
    payload: dict[str, Any]
    user_text: str
    accepted_at_monotonic: float
    accepted_at_unix_ms: int
    queue_deadline_monotonic: float
    client_message_id: str | None
    payload_fingerprint: str
    runtime_interrupt: bool
    state: Literal[
        "received",
        "session_queued",
        "admission_queued",
        "running",
        "terminal",
    ]
    queue_reason: Literal["session_busy", "global_capacity"] | None
    reservation: QueueReservation | None
    admission_ticket: AdmissionTicket | None
    timeout_task: asyncio.Task[None] | None
    dispatch_task: asyncio.Task[None] | None
```

规则：

- `turn_id/run_id` 使用 payload 已提供值，否则在入口处生成 UUID。
- 原始消息只在受控 Gateway 内存中保存；lifecycle/trace 不记录正文。
- `payload_fingerprint` 是规范化执行 payload 的 SHA-256，用于检测 identity conflict，不作为权限凭证。
- 只允许单向状态转换；终态不能重新排队。
- queued turn 不拥有 backend cancel token，进入 running 后才创建 active-run token。

### 6.3 DedupeRecord

```python
@dataclass
class DedupeRecord:
    client_message_id: str
    turn_id: str
    run_id: str
    payload_fingerprint: str
    state: str
    expires_at_monotonic: float
```

去重作用域为 `user_id + session_id + client_message_id`。`GatewaySessionService` 已按 user 管理，因此内部 key 可以是 `session_id + client_message_id`。

### 6.4 AdmissionTicket 与 RunPermit

```python
@dataclass
class AdmissionTicket:
    ticket_id: str
    user_id: str
    session_id: str
    turn_id: str
    run_id: str
    ready: asyncio.Future[RunPermit]
    enqueued_at_monotonic: float
    cancelled: bool = False

@dataclass
class RunPermit:
    permit_id: str
    run_id: str
    acquired_at_monotonic: float
    released: bool = False
```

permit 必须在 backend turn 的 `finally` 中释放。设置 cancel token 不等于释放 permit；只有 backend task 真正结束后才能释放，防止非合作式工具继续执行时超额准入。

## 7. 配置

应用级环境变量：

```text
MULTIMODAL_AGENT_GATEWAY_MAX_ACTIVE_RUNS=4
MULTIMODAL_AGENT_GATEWAY_MAX_PENDING_PER_SESSION=8
MULTIMODAL_AGENT_GATEWAY_MAX_QUEUED_TURNS=64
MULTIMODAL_AGENT_GATEWAY_QUEUE_WAIT_TIMEOUT_MS=120000
MULTIMODAL_AGENT_GATEWAY_DEDUPE_TTL_S=300
MULTIMODAL_AGENT_GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER=1024
```

配置解析位于 `api/gateway_runtime.py`，构造一个 `GatewayQueuePolicy` 并传给 `GatewaySessionManager`。无效非正数或未知 mode/drop policy 必须在 manager 创建时返回明确配置错误，不静默提升到无限值。

阶段一不新增用户可写 `/queue` 命令，也不通过 `config.update` 暴露 queue 容量或 timeout。入口层如需设置默认 interrupt，继续使用已经受信任并受 adapter 校验的 session config；全部容量和 active permit 只由进程启动配置决定。

## 8. 接收与排队流程

### 8.1 普通 followup

```text
1. Validate session_id and payload shape.
2. Resolve turn_id/run_id at ingress.
3. Build prompt-safe identity and payload fingerprint.
4. Check identity conflict and optional client_message_id dedupe.
5. Check per-session pending cap.
6. Reserve one global queued slot.
7. Register dedupe record only after reservation succeeds.
8. If session has running/admission head:
     append to session FIFO
     state=session_queued
     emit run.queued(reason=session_busy)
9. Else:
     make this turn the session head
     request a global admission ticket
10. If permit is immediate, enter running without run.queued.
11. If permit waits, state=admission_queued and emit
    run.queued(reason=global_capacity).
```

拒绝的 turn 不进入 dedupe cache，允许客户端在容量恢复后使用相同 `client_message_id` 重试。

### 8.2 全局准入

```text
session head
  -> controller waiter FIFO
  -> permit granted
  -> release queued reservation
  -> append user text to history exactly once
  -> create CancelToken and active-run deadline
  -> run.started
  -> backend
  -> run.end
  -> release permit
  -> promote next session turn to waiter tail
```

active-run `run_timeout_ms` 从 permit 获得、即将进入 backend 时开始计算，不包含 queue wait。`queue_wait_timeout_ms` 从消息被接受时开始，覆盖 session queue 和 admission queue 两段等待。

v1 为每个 accepted queued turn 创建一个有界 timeout task；turn 进入 running、取消、拒绝或终止时立即取消该 task。由于 global queued cap 限制 task 数量，这不会形成无界 timer 集合。timeout task 只请求 session service 完成原子状态转换，不直接修改 controller 私有状态。

`GatewayTurnFacade.timeout_s` 仍是调用方的总 wall-clock timeout，覆盖 queue wait 和 active run。Facade 超时时必须 best-effort 发送带 `run_id` 的 `run.cancel`，避免留下 orphan queued/running turn。

`GatewayTurnFacade` 不能让每个并发 HTTP 调用直接消费同一个 user endpoint。v1 在 Facade 内为每个 managed user endpoint 保留一个 reader，并按 `run_id` 把 frame 分发到各 turn inbox；inbox 必须在发送 `message.user` 前注册。这样同一 user/session 的并发 request/response turn 可以由 Gateway 排队，而不会互相抢走 `run.queued`、`stream.chunk` 或 `run.end`。

### 8.3 锁顺序

必须采用固定锁顺序：

```text
GatewaySessionService._lock
  -> GatewayRunAdmissionController._lock
```

controller 不得在持有自身锁时回调 session service。permit future 必须在锁外唤醒，避免 re-entrant deadlock。

## 9. Interrupt 语义

首版 interrupt 是 Gateway lifecycle control，不是 steer：

```text
1. Validate and reserve capacity for the new turn.
2. If reservation fails, reject the new interrupt and keep old run alive.
3. If current head is admission_queued, cancel that queued head directly.
4. If current head is running, signal its CancelToken.
5. Insert new turn before ordinary followups.
6. Wait for old backend task to terminate and release its permit.
7. Submit the new turn for global admission.
```

约束：

- 同一 session 不并行运行旧 backend 和新 backend。
- repeated interrupt 仍服从 queue cap，不静默丢弃已经接受的 turn。
- interrupt 不回滚已提交副作用。
- 旧 run 的任何晚到 stream/tool output 继续被 stale-output gate 抑制。
- 新 turn 的 metadata 保留 `control=interrupt`，供 runtime 理解这是显式用户纠偏。
- 当前 pending followups 默认保留在新 interrupt 后面。

## 10. 取消、超时与终态

### 10.1 定向 run.cancel

`run.cancel(run_id=...)` 按顺序查找：

1. 当前 session 的 queued/admission turn。
2. 当前 active run。
3. 省略 session_id 时，在当前 user-owned service 内按 run_id 搜索。

找到 queued turn 时：

- 从 session queue 或 controller waiter 中移除。
- 释放 queued reservation。
- 不创建 backend cancel token，不调用 backend。
- 发送 `run.end(reason="cancelled")`。
- cancel contract 使用 `cancelled_by=run.cancel`、`phase=before_llm`、`speakable=false`。

### 10.2 session cancel、hangup 和 disconnect

- 普通 `run.cancel` 只给 session_id、不带 run_id：保持当前兼容语义，只取消 active run。
- hangup/disconnect：先阻止该 session 继续 promotion，再取消所有 queued/admission turns，最后取消 active run。
- queued turn 各自产生自己的 terminal `run.end`，便于 facade/client 收敛；入口 adapter 可以在连接已关闭时只保留 lifecycle 记录。
- session destroy/manager close 同样释放所有 reservation、ticket 和 permit。

### 10.3 Queue timeout

queued turn 到达 `queue_deadline_monotonic` 时：

- 原子地从当前位置移除。
- 发送 `run.end(reason="cancelled")`。
- cancel source 为 `queue_timeout`，phase 为 `before_llm`。
- 不进入 history，不启动 active-run deadline，不调用 backend。
- 后续 turn 可以继续 promotion，不因前一条过期而阻塞。

## 11. History 与上下文

- user text 只在 permit 获取后、`run.started` 前追加一次。
- `run.queued`、queue error、queue wait 和 queue metrics 不进入 conversation history。
- queued cancel/expire/reject 不进入 history、session summary 或长期记忆。
- provider/tool progress 语义保持不变。
- followup 获得的 history snapshot 包含此前真正开始过的 user turn；不包含仍在它后面的 queued turn。
- error/cancelled active turn 的 user message继续保留在 history，与当前行为兼容。
- queue policy 不读取 Memory，不注入 prompt，也不改变 context budget。

## 12. Wire protocol

### 12.1 run.queued

新增 additive v1 frame：

```json
{
  "v": 1,
  "type": "run.queued",
  "session_id": "s1",
  "turn_id": "t2",
  "run_id": "r2",
  "payload": {
    "reason": "session_busy",
    "queue_depth": 1,
    "global_queue_depth": 3,
    "queued_at_ms": 1783939200000
  }
}
```

规则：

- `reason` 只允许 `session_busy` 或 `global_capacity`。
- depth 是发送时快照，不是稳定 position 或 SLA。
- 不发送消息正文、provider、tool 参数或其他用户 queue 详情。
- 一个 run 可先因 session busy 收到一次 `run.queued`，promotion 后又因 global capacity 收到一次；客户端应按 `run_id` 更新状态。
- 立即获得 permit 的 turn 保持现有 `run.started -> ... -> run.end` 序列，不强制额外 ack。

### 12.2 Queued terminal

queued turn 可能在没有 `run.started` 的情况下直接：

```text
run.queued -> run.end(reason=cancelled)
```

因此 Gateway lifecycle 合约更新为：

```text
每个 run.end 之前必须出现同 run_id 的 run.queued 或 run.started。
run.started 仍然最多一次。
completed/error 终态必须有 run.started。
未 started 的 run.end 只能是 phase=before_llm 的 cancelled。
```

该约束由 `tests/test_gateway_session.py` 的 Gateway frame/lifecycle 序列测试验证，不修改 `TraceInvariantObserver`。后者只审计已经进入 assistant runtime 的 trace；queued-before-LLM turn 不应伪造 runtime trace event。

### 12.3 结构化错误

未接受的消息只返回 `error`，不返回 `run.end`：

| code | 语义 |
| --- | --- |
| `queue_overflow` | per-session 或 global queued cap 已满；payload 包含 `scope` |
| `duplicate_message` | 相同 client_message_id 已接受；包含原 turn/run/state |
| `identity_conflict` | turn_id/run_id 或 client_message_id 被不同 payload 复用 |

错误不包含其他用户或 session 的标识和队列信息。

## 13. 去重与容量

### 13.1 去重

- 可选 `payload.client_message_id` 是客户端重发键。
- 相同 key、相同 fingerprint 不重复执行，返回 `duplicate_message` 和 canonical identity/state。
- 相同 key、不同 fingerprint 返回 `identity_conflict`。
- 没有 key 时不承诺重连幂等。
- terminal record 保留 `dedupe_ttl_s`，不缓存或重放完整 assistant response。
- LRU/TTL 清理保持每 user 记录数不超过上限。
- 这是 single-process best effort，不是 durable exactly-once。

### 13.2 容量

- `max_pending_per_session` 只计算当前 head 后面的 queued turns；running/admission head 不计入 pending。
- `max_queued_turns_global` 计算全部非 running、非 terminal 的 accepted turns，包括 session pending 和 admission wait。
- active runs 单独受 `max_active_runs` 限制。
- overflow 一律 reject newest；不丢 oldest、不生成 synthetic summary、不自动合并。
- interrupt 也必须先获得 reservation。若容量不足，返回 overflow，旧 run 不被取消。

## 14. 公平性与背压

- controller waiter 为跨 session FIFO。
- 每个 session 同时最多有一个 admission ticket。
- 一个 session 完成后，其下一条 head 重新进入 waiter 尾部。
- permit 不按 user 文本、模型、工具或估算成本加权。
- lifecycle sink 慢或失败不能阻塞准入；沿用 fail-open observer 规则。
- v1 不实现 lane priority，interactive Gateway traffic 共享一个 `main` admission pool。
- active cancel 后 permit 直到 backend task 结束才释放，确保真实 in-flight 数不超过配置。

## 15. 与其他子系统的边界

### 15.1 Tool calling

- Gateway permit 表示一个顶层 Gateway backend run，不是 tool permit。
- 同一个 turn 内的 read-only tool parallel scheduler 继续由 assistant loop/tool scheduler 管理。
- cancel 继续通过 `CancelToken -> runtime -> ToolExecutor` 合作传播。
- Gateway 不因 queue mode 绕过 validator、executor、registry、policy 或 audit。
- committed/unknown side effect 不因 interrupt 自动重试或复用。

### 15.2 Multi-agent

- 顶层 Gateway run 在委派期间继续持有自己的 permit。
- delegated child 不额外消耗 Gateway permit；它仍受现有 child budget、depth、timeout 和 loop policy。
- `/agents/run` 与 A2A 是显式 router/adapter 入口，不在 v1 自动进入 Gateway admission。
- 如未来需要统一昂贵执行资源，应设计独立 runtime capacity service，而不是让 worker agent 理解 Gateway frame。

### 15.3 Proactive Wake

- Proactive Wake coordinator、due-rule reconciler 和 outbox 不进入 Gateway。
- v1 Gateway admission 只管理 Gateway-normalized interactive turns。
- Proactive Wake 继续使用自身全局预算和 `UserActivityReader`。
- 未来如需统一资源，可新增独立 `proactive` lane；不能让主动通知挤占或 interrupt 当前 realtime turn。

### 15.4 Durable task

- `QueuedTurn` 是短生命周期、单进程、会话内对象，不升级为 `AgentTask`。
- Hermes 式 background task/Kanban 需要独立 task id、lease、heartbeat、retry、artifact 和 restart recovery 规格。
- 不复用 Gateway history 作为 durable task store。

## 16. 失败与恢复

| 失败点 | v1 行为 |
| --- | --- |
| session queue full | reject newest；旧状态不变 |
| global queue full | reject newest；不启动 backend |
| invalid config | manager 创建失败并返回明确错误 |
| queued wait timeout | cancelled before_llm；释放 reservation |
| queued run.cancel | cancelled before_llm；不调用 backend |
| active cancel 不合作 | 保持 permit，等待 backend 终止或 active deadline |
| endpoint send `run.queued` 失败 | 清理未运行 turn 和 reservation；记录 lifecycle |
| lifecycle sink 失败 | fail-open，不影响 queue state |
| controller close | 取消全部 waiter，唤醒 session cleanup |
| manager/session destroy | 取消 queued/active，释放全部 controller 资源 |
| process crash | queued state 丢失；明确属于 v1 限制 |
| duplicate after TTL | 可能作为新 turn 接受；不宣称 durable exactly-once |

任何异常路径都必须满足：reservation、ticket、permit 最多释放一次，且 terminal state 最多投影一次。

## 17. Observability

### 17.1 Lifecycle events

保留并扩展 prompt-safe Gateway lifecycle：

```text
gateway.run.queued
gateway.run.admitted
gateway.run.queue_rejected
gateway.run.queue_expired
gateway.run.cancel_requested
gateway.run.started
gateway.run.completed
gateway.run.cancelled
gateway.run.errored
```

建议 payload：

```text
queue_reason
session_queue_depth
global_queue_depth
active_runs
max_active_runs
queue_wait_ms
outcome_reason
```

禁止记录：

- 用户正文和完整 payload。
- 其他 session 的 ID 或队列内容。
- provider raw response、tool arguments、memory 内容和 hidden reasoning。

### 17.2 关键指标

```text
gateway_queue_wait_ms{reason}
gateway_queue_depth{scope}
gateway_active_runs
gateway_queue_rejections_total{scope}
gateway_queue_expirations_total
gateway_queued_cancellations_total
gateway_duplicate_messages_total
```

v1 可以先通过 lifecycle sink 和离线测试验证，不要求立即接入外部 metrics backend。

## 18. 代码落点

```text
src/assistant_agent/gateway/
├── protocol.py                 # run.queued constant/helper semantics
├── queueing.py                 # policy, QueuedTurn, dedupe, controller
├── session.py                  # session queue lifecycle integration
├── bridge.py                   # hangup/disconnect session-wide queue cleanup source
└── observability.py            # existing event shape, no raw content

src/assistant_agent/api/
└── gateway_runtime.py          # env parsing and manager policy construction

src/assistant_agent/services/
└── gateway_turn_facade.py      # single endpoint reader, run_id demux, timeout cancel

tests/
├── test_gateway_queueing.py
├── test_gateway_session.py
├── test_gateway.py
├── test_gateway_api.py
├── test_gateway_turn_facade.py
└── existing realtime/Gateway compatibility tests

docs/
└── gateway-architecture.md     # implementation完成后更新权威状态
```

`queueing.py` 不依赖 FastAPI、provider、tool registry、memory 或 AgentRouter。

## 19. 分阶段实现

### Phase 1：Queue primitives 与 global admission

- `GatewayQueuePolicy` 校验。
- reservation、ticket、permit 和 FIFO controller。
- controller cancel/close/snapshot。
- 纯离线并发测试。

### Phase 2：Session queued-turn lifecycle

- `PendingUserMessage` 替换为 `QueuedTurn`。
- ingress identity、`run.queued`、history-at-start。
- queued cancel、timeout、hangup/disconnect cleanup。
- interrupt 串行化和 stale output 兼容。

### Phase 3：Dedupe、配置与入口兼容

- optional client_message_id。
- overflow 与 identity conflict。
- application env wiring。
- facade timeout cancellation。
- Gateway WebSocket、HTTP、CLI、media adapter 回归。

### Phase 4：Observability、文档与资格门禁

- lifecycle/invariant 更新。
- authority doc 更新。
- targeted/full offline tests。
- 不启用真实 Provider。

## 20. 测试矩阵

### 20.1 Policy/controller 单元测试

- 默认值与非法配置。
- max active permits 永不超限。
- 跨 session FIFO。
- 同 session 只提交一个 head ticket。
- ticket cancel、timeout、controller close。
- reservation/permit double release 幂等。
- cancellation race 不泄漏 active/queued count。

### 20.2 Session 集成测试

- 当前普通 followup 顺序保持。
- session busy 发出 `run.queued`，随后 started/end。
- global capacity wait 发出正确 reason。
- queued turn 取消不调用 backend、不写 history。
- queued timeout 后下一 turn 正常 promotion。
- interrupt 不与旧 backend overlap。
- interrupt reservation 失败时旧 run 保持运行。
- hangup/disconnect 清空 queued turns。
- active cancel 直到 backend 结束才释放 permit。
- late output 仍被抑制。

### 20.3 Dedupe/overflow

- 相同 client_message_id 只执行一次。
- 同 key 不同 payload 返回 identity conflict。
- 无 dedupe key 保持兼容。
- session/global overflow reject newest。
- rejection 不污染 dedupe，容量恢复后可重试。
- TTL/LRU 清理有界。

### 20.4 入口与协议回归

- `GatewayTurnFacade` 接受中间 `run.queued`。
- 同一 user 的并发 Facade turn 按 run_id 收到各自 frame，不互相抢读。
- facade timeout best-effort 取消 queued/active run。
- `/agent/run` schema 不变。
- `/ws/gateway` 透传 additive frame。
- realtime media interrupt/hangup 行为不退化。
- CLI/demo mock 路径不启用真实 Provider。
- Gateway lifecycle 测试接受 queued-cancelled，拒绝 queued-completed-without-start；runtime trace invariant 保持不变。

### 20.5 性质断言

```text
active_runs <= max_active_runs
queued_turns <= max_queued_turns_global
per_session_pending <= max_pending_per_session
backend_concurrency_per_session <= 1
accepted_turn_terminal_count <= 1
reservation_release_count == reservation_acquire_count
permit_release_count == permit_acquire_count
cancelled_before_llm_backend_calls == 0
```

## 21. 验收标准

首版验收结论：

> Gateway 在单进程内为每个已接受的用户 turn 保留稳定身份，以同 session 串行和进程级公平准入限制执行；queued turn 可以被定向取消或超时，容量不足时明确拒绝 newest，且不会绕过现有 assistant loop、工具治理和安全边界。

可验证要求：

- 默认普通消息行为仍为 followup。
- 同 session backend concurrency 永远不超过 1。
- 全进程 active Gateway run 永远不超过 `max_active_runs`。
- queued turn 在入口处已有 `turn_id/run_id`，可以用 run_id 取消。
- queued cancel/timeout 不调用 backend、不写 history。
- overflow 不删除已接受消息，不产生 synthetic prompt。
- interrupt 只在新 turn 成功预留后取消旧 run。
- active cancel 后不提前释放 permit。
- queue state/metrics 不泄露正文或其他用户信息。
- 所有测试保持 mock/local/offline，不安装依赖，不调用真实 Provider。

## 22. Backlog

以下能力需要独立规格，不进入本计划：

- `collect`：quiet window、message group identity、media merge、member cancel。
- `steer`：runtime injection boundary、tool-call commit barrier、context revision。
- lane：interactive、proactive、cron、subagent 的独立预算和优先级。
- durable queue：restart recovery、lease、claim、retry 和 delivery ownership。
- distributed admission：多进程/多实例共享容量。
- adaptive concurrency：基于 provider rate limit、延迟和成本自动调节。
- queued response replay 和 durable idempotency。

## 23. 自审记录

自审日期：2026-07-13。自审范围包括架构边界、状态机、并发不变量、失败恢复、协议兼容、配置权限、可观测性、隐私和计划可执行性。

自审中完成以下收敛：

- 资源上限只允许进程启动配置，session 只能沿用现有可信 interrupt 语义，避免客户端扩大服务端容量。
- queued-before-LLM turn 不伪造 assistant runtime trace；其完整证据归 Gateway frame 与 lifecycle sink。
- active permit 只在 backend 真正结束后释放；cancel request 本身不提前归还容量。
- interrupt 必须先为新 turn 完成 reservation，再请求取消旧 run；容量不足时拒绝新 turn，并保持旧 run 存活。
- admission grant 与 cancel/timeout 的竞争必须通过单次所有权转移解决，避免 permit 泄漏或双重释放。
- `GatewayTurnFacade` 对同一 user endpoint 只保留一个 reader，以 `run_id` demux 到独立 inbox，避免并发 HTTP turn 互相抢 frame。
- manager 向既有 custom `service_factory` 绑定共享 admission 时必须保留原 factory 行为；service、ticket、timer、reservation 和 permit 都要求幂等清理。
- 每个已接受 queued turn 至多一个等待 timer，并由终态或 service close 回收，避免无界后台 task。

自审结论：设计满足本项目 Gateway、assistant loop、工具治理、Memory、多 Agent 和 Proactive Wake 的既有边界；v1 风险已被明确的状态转换、资源不变量与 mock/local/offline 测试矩阵覆盖，可进入实现计划。该结论不表示代码已经实现或验证。

## 24. 参考资料

- Hello Claw，第五章“消息循环与事件驱动”：<https://datawhalechina.github.io/hello-claw/cn/build/chapter5/>
- OpenClaw Command queue：<https://docs.openclaw.ai/concepts/queue>
- OpenClaw Steer：<https://docs.openclaw.ai/tools/steer>
- Hermes Agent Messaging Gateway：<https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/index.md>
- Hermes Agent repository：<https://github.com/NousResearch/hermes-agent>
- 本项目 Gateway 权威文档：`docs/gateway-architecture.md`
- 本项目工具治理权威文档：`docs/tool-calling-architecture.md`
- 本项目 Proactive Wake 规格：`docs/superpowers/specs/2026-07-13-proactive-wake-design.md`
