# Proactive Wake 主动心跳设计

## 文档状态

本文档定义 `assistant_agent` 首版 Proactive Wake（主动唤醒）能力。设计已完成分段评审，当前等待用户对落盘文档做最终确认。

本文档只描述设计，不授权实现，不启用真实 Provider，也不改变当前默认 mock/local/offline 行为。

## 1. 背景与结论

当前项目已经存在 realtime progress heartbeat：当一个正在执行的 run 长时间没有新输出时，`ProgressTracker` 生成 display-only `run.progress`，告诉入口层任务仍在运行。它属于单次 run 的进度保活，不会在没有用户请求时创建新的 Agent turn。

本文所说的主动心跳是另一类能力：系统在没有用户消息时，由外部事件或低频补漏检查唤醒一次后台任务；后台任务先检查用户显式关注的事项，只有出现值得通知的新证据，并且注意力策略允许时，才通过 App/IM 给用户发送文本通知。

为避免两个 heartbeat 概念混淆，代码和协议统一使用 `Proactive Wake`：

```text
Progress Heartbeat
  = 已有 run 长时间无输出时，投影“仍在处理”的进度事件

Proactive Wake
  = 没有用户消息时，由事件或低频补漏启动后台检查
```

首版采用 Policy-first 管线，而不是直接复制 OpenClaw 的完整周期 Agent turn，也不复制 Hermes 的中央巨型 Gateway/Scheduler：

- 借鉴 OpenClaw 的静默巡检、轻量上下文、活跃时段和 `SILENT` 语义。
- 借鉴 Hermes 的统一 Agent 入口和统一投递抽象。
- 由确定性策略先过滤事件、去重和检测变化，只有语义候选才调用一次受限模型。
- 所有外部读取仍经过现有 `ActionValidator -> ToolExecutor -> ToolRegistry` 治理边界。
- Agent 不负责定时、投递、权限扩张或关注规则生成。

## 2. 已确认的产品范围

首版边界如下：

- 同时支持“唤醒 Agent”和“必要时唤醒用户”两个阶段。
- 外部事件驱动为主，低频 reconciliation heartbeat 负责补漏和状态对账。
- 后台检查只能读取，不能修改日历、发送业务消息或执行其他外部写操作。
- 用户侧只支持 App/IM 文本通知，不主动发起实时电话。
- 只执行用户显式创建的关注规则。
- 用户关注规则使用结构化模型，不执行自由格式 `HEARTBEAT.md`。
- 主动通知不能 interrupt 当前实时通话。

## 3. 非目标

首版明确不实现：

- Agent 自主创建、修改或启用关注规则。
- 自由格式 `HEARTBEAT.md` 指令执行。
- 任意 cron、shell、Python 或 URL callback。
- 主动拨打电话。
- 主动写日历、发邮件、发业务消息或修改外部状态。
- 多 Agent 协同巡检。
- 宽泛长期记忆扫描或自动写入长期记忆。
- 后台自主目标生成。
- 通用 Personal OS scheduler、复杂 job platform 或新的 Agent loop。
- 没有显式关注规则时由模型猜测用户可能关心什么。

## 4. 总体架构

```text
Provider Webhook / Low-frequency Tick
                 │
                 ▼
        WakeTriggerAdapter
        normalize to WakeSignal
                 │
                 ▼
         ProactiveWakeCoordinator
                 │
        ┌────────┴────────┐
        ▼                 ▼
  WakeRuleStore       WakeStateStore
  explicit rules      cursor/dedup/cooldown
        │                 │
        └────────┬────────┘
                 ▼
          deterministic gates
   enabled / version / budget / dedup
                 │
                 ▼
        governed evidence probe
 ActionValidator -> ToolExecutor -> ToolRegistry
                 │
                 ▼
      change detector / evidence digest
       unchanged -> record silent and stop
                 │ changed
                 ▼
         AgentGraphRuntime
      PROACTIVE_CHECK prompt profile
                 │
                 ▼
       structured WakeDecision
       silent / notify candidate
                 │
                 ▼
          AttentionPolicy
 quiet hours / cooldown / limits / user activity
                 │
                 ▼
       durable Notification Outbox
                 │
                 ▼
          App / IM adapter
```

### 4.1 组件职责

`WakeTriggerAdapter` 只把 provider event、低频 tick 和受控手工触发归一化为 `WakeSignal`，不调用 LLM，不执行工具，不发送通知。

`ProactiveWakeCoordinator` 编排单次 WakeRun 生命周期，不实现第二套 assistant loop，不拥有 provider-specific 业务逻辑。

`WakeRuleStore` 保存用户显式创建并通过验证的关注规则。规则变更属于独立的用户写操作，后台 Agent 没有修改权限。

持久规则只保存稳定身份字段 `tenant_id`、`user_id` 和可选 `project_id`；不把临时 `session_id` 或请求级 `allowed_scopes` 固化为长期授权。每次运行仍要从可信入口重新建立有效执行身份。

`WakeStateStore` 保存 cursor、signal 幂等、最近 evidence fingerprint、cooldown、通知额度和运行状态。它不是 conversation history，也不是长期记忆。

`Governed Evidence Probe` 根据规则中预先声明的只读工具和参数收集证据。规则创建和运行两个阶段都要检查 proactive allowlist、`ToolSpec.side_effect` 和参数 schema。

`AgentGraphRuntime` 只在出现新证据且规则需要语义判断时执行一次 one-shot `PROACTIVE_CHECK`。它不负责定时、重复投递或规则管理。

`AttentionPolicy` 对模型产生的 notify candidate 做最终确定性判定。模型输出 `notify` 不代表一定发送。

`Notification Outbox` 保存可靠投递状态、lease、重试和幂等键。App/IM adapter 只实现 transport，不参与语义判断。

`UserActivityReader` 是只读协议，用于判断用户是否处于活跃 realtime run。首版提供进程内 Gateway adapter；Proactive Wake 不直接读取 `GatewaySessionService` 私有字典，也不因此把 scheduler 放入 Gateway。

## 5. 核心数据模型

### 5.1 WakeOwner

```python
WakeOwner:
    tenant_id: str | None
    user_id: str
    project_id: str | None
```

`WakeOwner` 只保存稳定身份字段。运行时根据可信入口和 proactive scope 配置重建 `RequestIdentity`；不从数据库恢复旧 `session_id` 或请求级 `allowed_scopes`。

### 5.2 WakeRule

```python
WakeRule:
    rule_id: str
    owner: WakeOwner
    name: str
    enabled: bool
    version: int

    trigger:
        event_sources: list[str]
        event_types: list[str]
        reconcile_interval_s: int

    probe:
        tool_name: str
        arguments: dict

    condition:
        mode: "changed" | "semantic"
        notify_when: str
        notify_on_initial: bool

    attention:
        channel: str
        quiet_hours: QuietHours
        cooldown_s: int
        daily_notification_limit: int
        minimum_severity: str

    created_at: datetime
    updated_at: datetime
```

约束：

- `probe.tool_name` 必须属于 proactive read-only allowlist。
- 对应 `ToolSpec.side_effect` 必须明确为只读，而不是未知。
- 参数在规则创建时完成 schema 校验，运行时不能由模型扩展。
- Probe 超时和重试服从工具声明及现有 `ProviderExecutionPolicy`；规则不能覆盖执行器策略。
- `mode="changed"` 由本地 evaluator 根据 evidence fingerprint 判断；阶段一只实现这个模式。
- 首次成功观测默认只建立 baseline，`notify_on_initial=false` 时不通知。
- `mode="semantic"` 的 `notify_when` 是判断条件数据，不是可执行 prompt，并留到阶段二实现。
- Agent 不能创建、修改、启用或删除规则。
- 首版不允许规则携带 shell、Python、任意 callback 或自由格式工具链。

示例：

```json
{
  "name": "近期会议提醒",
  "trigger": {
    "event_sources": ["calendar"],
    "event_types": ["calendar.event.created", "calendar.event.updated"],
    "reconcile_interval_s": 3600
  },
  "probe": {
    "tool_name": "calendar_events_list",
    "arguments": {"window_hours": 2}
  },
  "condition": {
    "mode": "changed",
    "notify_when": "未来两小时内出现尚未提醒的新会议",
    "notify_on_initial": false
  },
  "attention": {
    "channel": "app",
    "cooldown_s": 1800,
    "daily_notification_limit": 6,
    "minimum_severity": "normal"
  }
}
```

### 5.3 WakeSignal

```python
WakeSignal:
    signal_id: str
    kind: "provider_event" | "reconcile_tick" | "manual"
    source: str
    event_type: str
    occurred_at: datetime
    owner: WakeOwner
    event_key: str | None
    cursor: str | None
    prompt_safe_facts: dict
```

`signal_id` 和 `event_key` 用于幂等。Signal 只表示可能需要检查，不携带工具权限，不能直接触发通知。Provider 原始 payload、凭证、完整邮件正文和私人消息全文不得直接进入模型或 audit。

### 5.4 WakeEvidence

```python
WakeEvidence:
    evidence_id: str
    rule_id: str
    observed_at: datetime
    probe_tool_name: str
    status: "succeeded" | "failed" | "timed_out"
    fingerprint: str
    previous_fingerprint: str | None
    is_initial: bool
    changed: bool
    summary: str
    prompt_safe_payload: dict
    source_refs: list[str]
```

`fingerprint` 用于变化检测和通知去重。`prompt_safe_payload` 必须经过字段白名单、长度限制和脱敏。Probe 失败是运行状态，不能交给模型猜测成业务告警。

阶段一的确定性通知文本固定使用“规则名称：evidence summary”，其中 summary 来自现有 `ToolObservation` 脱敏边界并受长度限制；不开放任意通知模板，也不把原始 `ToolResult.data` 直接发给用户。

### 5.5 WakeDecision

```python
WakeDecision:
    outcome: "silent" | "notify"
    severity: "low" | "normal" | "high"
    reason_code: str
    summary: str
    user_message: str | None
    evidence_ids: list[str]
    confidence: float | None
    expires_at: datetime | None
```

约束：

- `silent` 禁止携带 `user_message`。
- `notify` 必须引用至少一个本次 evidence。
- `user_message` 有严格长度限制，首版只生成一条文本。
- 模型声明的 severity 不能绕过 AttentionPolicy。
- 输出解析失败时 fail-closed，记录失败并保持静默。

### 5.6 AttentionDecision

```python
AttentionDecision:
    outcome: "allow" | "defer" | "suppress"
    reason_code: str
    deliver_after: datetime | None
    expires_at: datetime | None
```

稳定 reason code 至少包括：

```text
duplicate_evidence
cooldown_active
daily_limit_reached
quiet_hours
active_conversation
expired
rule_disabled
policy_denied
```

### 5.7 NotificationEnvelope

```python
NotificationEnvelope:
    delivery_id: str
    owner: WakeOwner
    channel: str
    destination_ref: str
    message: str
    idempotency_key: str
    rule_id: str
    evidence_ids: list[str]
    evidence_fingerprint: str
    deliver_after: datetime
    expires_at: datetime
    status: "queued" | "leased" | "sent" | "acknowledged" | "retry_wait" | "expired" | "dead_letter"
    attempt_count: int
    lease_until: datetime | None
    provider_message_id: str | None
    last_reason_code: str | None
```

幂等键由 `owner + rule_id + evidence_fingerprint + channel` 生成。

## 6. 触发和并发模型

外部事件与低频补漏最终都生成 `WakeSignal`：

```text
Provider event -> WakeSignal immediately

Reconciliation ticker
  -> find due rules
  -> emit WakeSignal only for due reconciliation work
```

补漏 tick 只用于检查 provider 事件是否可能丢失、cursor 是否长期未推进、规则是否到达下一次 reconciliation、上次 probe 是否失败。它不能无条件为所有规则创建完整 Agent turn。

并发和聚合规则：

- 同一 `rule_id` 的 WakeRun 串行执行。
- 阶段一只保证单进程内 per-rule 串行；跨进程 run ownership/lease 属于后续 durable runtime，不在首版伪装实现。
- 不同用户可以并行，但受全局并发预算约束。
- 同一 `event_key` 只处理一次。
- 同一 rule/source 在短窗口内的事件合并成一个 batch。
- 同一规则在通知聚合窗口内的多次变化最多生成一条通知。
- 不同规则默认不交给 LLM跨隐私域自由合并。
- 用户正在实时通话时，普通通知进入 outbox 延迟投递，不能插入或 interrupt 当前 turn。

## 7. 主动上下文隔离

主动运行不能直接复用最近聊天会话。新增 `PROACTIVE_CHECK` system prompt profile，仍使用现有 `AgentGraphRuntime`、provider adapter、trace 和 context report，但构建独立最小上下文：

```text
system governance invariants
+ read-only WakeRule view
+ current WakeEvidence
+ AttentionPolicy summary
+ explicitly required user preferences
+ last notification time/summary
+ WakeDecision output schema
```

默认不注入：

- 最近完整对话历史。
- session summary。
- 普通 tool observations。
- 未明确相关的长期记忆。
- 全量 ToolSpec。
- `HEARTBEAT.md`。
- provider 原始 payload。

首版模型调用配置：

```text
tools = []
max_iterations = 1
output = WakeDecision
```

Evidence 内容始终按数据处理，不能覆盖 system policy。模型调用只负责语义判断和生成简短通知，不拥有工具权限。

## 8. WakeRun 执行流程

```text
1. Receive WakeSignal.
2. Resolve RequestIdentity and matching WakeRule.
3. Check signal/event idempotency key.
4. Check rule enabled state, version and budgets.
5. Coalesce same-rule event burst.
6. Run read-only probe through ActionValidator and ToolExecutor.
7. Redact, bound and fingerprint evidence.
8. Compare with previous fingerprint.
9. If unchanged, record silent and update cursor.
10. If changed:
      mode=changed  -> deterministic evaluator
      mode=semantic -> one-shot PROACTIVE_CHECK
11. Produce silent or notify candidate.
12. Apply AttentionPolicy.
13. Record suppress/defer reason when not immediately allowed.
14. Atomically enqueue allowed/deferred notification in outbox.
15. App/IM transport sends and records delivery state.
16. Update WakeState without automatic long-term memory write.
```

WakeRun 生命周期：

```text
received
  -> deduplicated / gated
  -> probing
  -> unchanged
  -> evaluating
  -> silent
  -> notify_candidate
  -> suppressed / enqueued
  -> delivered / delivery_failed
```

阶段一的确定性规则只判断归一化 evidence 是否相对 baseline 发生变化。首次观测默认建立 baseline 并静默；后续可以逐步增加经过独立设计的结构化字段比较，但不能通过解析 `notify_when` 自然语言假装确定性执行。只有“邮件是否需要今天处理”等语义条件才在阶段二进入模型。

## 9. AttentionPolicy

确定性策略顺序：

```text
rule still enabled
-> evidence not already notified
-> cooldown elapsed
-> daily notification budget available
-> user not in active realtime conversation
-> quiet-hours policy
-> notification still timely
-> allow / defer / suppress
```

默认行为：

- 安静时段延迟到结束后，不立即发送。
- 用户正在通话时延迟到通话结束，并在规则内聚合。
- 相同 evidence fingerprint 不重复提醒。
- cooldown 内的新变化进入短聚合窗口。
- 超过 `expires_at` 后抑制，不补发过期信息。
- 模型标记 `high` 不能自动突破安静时段。
- 首版不开放紧急时段绕过；未来只能由用户显式规则配置。

## 10. Durable Notification Outbox

现有 WebSocket delivery registry 主要解决连接内响应确认，不能承担离线主动通知。Proactive Wake 使用独立持久 outbox：

```text
queued
  -> leased
  -> sent
  -> acknowledged  # channel capability permitting
  -> retry_wait
  -> expired
  -> dead_letter
```

语义：

- `sent` 只表示渠道 API 接受，不代表用户已阅读。
- 只有渠道 ACK 才记录 `acknowledged`。
- 投递失败只重试 outbox，不重新 probe 或调用 LLM。
- 进程重启后，lease 超时的记录可以被重新领取。
- 超过有效期后不再重试。
- enqueue 与 WakeRun/Attention 状态更新必须位于同一事务边界，避免重复通知。

投递接口：

```python
class ProactiveNotificationTransport(Protocol):
    async def send(
        self,
        notification: NotificationEnvelope,
    ) -> DeliveryResult:
        raise NotImplementedError
```

## 11. 会话、记忆和 Gateway 边界

- 后台 probe 和 silent decision 不进入 conversation history。
- 首版不把主动通知投影到现有 Gateway conversation history；当前 Gateway 会话历史不是可持久追加完整 assistant message 的共享时间线。首版以 outbox/delivery audit 作为发送记录。未来只有在统一 durable conversation store 成为 Gateway/runtime 的共同边界后，才允许把 transport 已接受或已 acknowledged 的通知投影为带 `source=proactive_wake`、`rule_id`、`delivery_id` metadata 的 assistant message。
- 用户回复主动通知时，才创建普通用户 turn，经 Gateway 进入主 runtime。
- `GatewaySessionService` 只通过 `UserActivityReader` adapter 提供活跃会话只读状态，不负责 tick、规则判断或 outbox。
- 主动通知不能 cancel 或 interrupt 当前 realtime run。
- WakeState 不属于 Memory Service。
- 首版不调用 `memory_save`，不自动把巡检结果写入长期记忆。
- 如未来需要少量用户偏好，只能经 MemoryReadPolicy 读取与该规则明确相关的 allowlisted profile 字段。

## 12. 持久化设计

首版使用独立 SQLite：

```text
.local/proactive_wake.sqlite3
```

建议表：

```text
wake_rules
wake_rule_state
wake_signal_dedup
wake_runs
notification_outbox
notification_attempts
```

不要复用 Memory Store 数据表。规则、运行状态和通知投递共享一个 SQLite 事务边界，但仍通过各自 repository 接口隔离职责。

这只是窄范围 due-rule reconciler，不是通用 cron service。

## 13. 安全与治理

必须同时在规则创建和运行阶段验证：

- owner、tenant、user 和目标 App/IM 身份绑定。
- Probe 工具属于 proactive allowlist。
- `ToolSpec.side_effect` 明确为只读。
- 工具执行经过 `ActionValidator -> ToolExecutor -> ToolRegistry`。
- 规则不能包含可执行代码、任意 callback 或动态工具链。
- Provider payload 先白名单、脱敏和限长，再进入模型。
- Agent 无权修改规则、attention policy、安静时段和目标渠道。
- 主动运行禁止 `memory_save`、通知类工具和其他写工具。
- Trace/audit 不保存邮件正文、私人消息全文、Token、凭证或 provider 原始响应。
- 每个用户都有 probe、LLM、WakeRun 和通知的小时/每日预算。

用户控制面必须提供：

```text
pause all proactive wake
mute until a timestamp
disable or delete one rule
inspect recent runs and suppression reasons
inspect notification source
revoke channel binding
```

## 14. 失败与恢复

| 失败点 | 首版行为 |
| --- | --- |
| 重复 webhook | 根据 signal/event key 静默丢弃 |
| 规则参数无效 | 创建/更新时拒绝；已存规则因工具目录变化而失效时，将 WakeRun 标记为 `config_error` 并阻止执行，等待用户修改，不静默改变 `enabled` |
| 只读 probe 超时 | 有限重试，不让 LLM猜测结果 |
| Provider 临时失败 | 保留业务 cursor，记录 backoff 和下次重试时间，稍后补漏 |
| Evidence 脱敏失败 | fail-closed，不调用模型 |
| LLM 超时或 schema 解析失败 | 保持静默，记录 evaluation failure |
| AttentionPolicy 异常 | fail-closed，不投递 |
| App/IM 发送失败 | outbox 指数退避重试 |
| 进程中途退出 | durable state 和 lease 恢复 |
| 通知过期 | 标记 expired，不补发 |

首版不会因为巡检失败自动通知用户。监控巡检系统本身需要另一条用户显式规则，且不能由失败处理路径隐式创建。

## 15. 代码落点

```text
src/assistant_agent/
├── schemas/
│   └── proactive_wake.py
├── services/
│   └── proactive_wake/
│       ├── coordinator.py
│       ├── rule_store.py
│       ├── state_store.py
│       ├── trigger.py
│       ├── probe.py
│       ├── change_detector.py
│       ├── evaluator.py
│       ├── attention.py
│       ├── outbox.py
│       └── observability.py
├── agent/
│   └── system_prompt_policy.py
├── services/context/
│   └── proactive_renderer.py
└── api/
    └── proactive_wake.py
```

集成要求：

- 身份复用 `schemas.identity.RequestIdentity`。
- 工具复用现有 validator、executor 和 registry。
- 模型复用 `AgentGraphRuntime` 和 provider adapter，新增 one-shot profile，不创建第二个 loop。
- 普通用户 turn 的 context builder 和 history 语义保持不变。
- Gateway 不新增 scheduler 职责。
- Observability 复用现有 trace/redaction 规范，增加 wake 专用事件。
- App/IM transport 是薄入口适配器。

## 16. 分阶段实现

本规格覆盖三个可独立验收的子系统，不使用一个超大实施计划一次完成：

1. 确定性 Proactive Wake 核心、SQLite 状态和 mock transport。
2. `PROACTIVE_CHECK` one-shot 语义判断与最小上下文。
3. 真实事件入口、真实只读 Provider 和 App/IM pilot transport。

第一份实施计划只覆盖阶段一。阶段二必须在阶段一的误通知、去重、恢复和预算证据通过后单独规划；阶段三必须在用户显式授权真实 Provider/channel 且使用 `provider_smoke` 或 `pilot` profile 时单独规划。

### 16.1 阶段一：确定性垂直切片

```text
structured WakeRule
+ manual/reconcile signal
+ fake/local read-only probe
+ fingerprint change detection
+ deterministic mode=changed condition
+ AttentionPolicy
+ mock App transport
+ SQLite state/outbox
```

阶段一不需要 LLM。目标是先证明不会重复提醒、不会打断通话、重启后不会丢通知。

### 16.2 阶段二：受限语义判断

```text
PROACTIVE_CHECK profile
+ minimal context renderer
+ structured WakeDecision output
+ scripted/fake real chat adapter tests
+ prompt-injection and parse-failure tests
```

### 16.3 阶段三：事件与真实渠道试点

```text
authenticated event ingest
+ one real read-only provider adapter
+ one App/IM transport
+ explicit provider_smoke/pilot validation
```

默认 profile 不自动调用真实外部服务。

## 17. 测试矩阵

### 17.1 单元测试

- Pydantic schema 和版本迁移。
- proactive tool allowlist 和 side-effect 验证。
- signal/event 幂等。
- evidence fingerprint 稳定性。
- cooldown、安静时段、每日上限。
- outbox 状态机、lease 和恢复。
- `silent` 禁止携带通知文本。

### 17.2 集成测试

- 无变化时零 LLM、零通知。
- 确定性变化时零 LLM、一条通知。
- 语义变化时最多一次 LLM、一条或零条通知。
- 相同事件重复到达最多投递一次。
- 同规则事件风暴合并为一次 probe。
- 用户正在通话时不 interrupt，通知延迟。
- enqueue 后进程退出，恢复后不重复决策。
- 投递失败只重试 outbox，不重新调用 LLM。
- write tool 在规则创建和执行阶段都被拒绝。
- 跨用户 signal/rule 被拒绝。
- Evidence 含提示词注入文本时不能改变 schema 或权限。
- Trace/audit 不包含原始私人 payload。

### 17.3 离线评测指标

```text
notification precision
suppression correctness
duplicate notification rate
LLM calls per useful notification
probe calls per rule per day
delivery latency
quiet-hour violations
cross-user isolation violations
```

## 18. 验收标准

首版总体验收标准：

> 只有用户显式关注的事项发生新变化时，系统才可能调用一次受限模型，并在注意力策略允许时最多发送一条可追溯、可去重、不会打断实时通话的文本通知。

可验证约束：

- 没有显式 WakeRule 时不会 probe、调用 LLM 或发送通知。
- 普通 reconciliation tick 不等于完整 Agent turn。
- 所有 probe 都经过工具治理边界且只读。
- 一次语义候选最多调用一次模型，模型不获得工具。
- AttentionPolicy 是最终通知裁决者。
- 相同 evidence/channel 最多成功投递一次。
- 进程重启不会导致已经入队的通知重新推理或重复 enqueue。
- 主动运行不会写长期记忆，不会打断实时通话。
- 默认测试和运行保持 mock/local/offline。
