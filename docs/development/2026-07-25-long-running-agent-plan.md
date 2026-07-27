# 长时 Agent 总体实施计划

状态：阶段 0—7 的 mock/local/offline 路线已完成
创建日期：2026-07-25  
适用项目：`assistant_agent`

> 本文用于保存长时 Agent 的完整实施方向、阶段边界和验收门槛，防止后续开发因上下文变化而偏离。
> 它不是当前架构事实权威。当前行为仍以源码、测试以及下列根级权威文档为准：
>
> - `docs/gateway-architecture.md`
> - `docs/runtime-event-stream-architecture.md`
> - `docs/tool-calling-architecture.md`
> - `docs/CONTEXT_ENGINEERING_STATUS.md`
> - `tests/README.md`

## 1. 为什么要做

项目已经具备邮件、文件、天气、日历、搜索、购物、视觉和 Python 等原子能力，但这些能力主要回答
“当前这一轮能调用什么工具”。长时 Agent 需要进一步回答：

- 一个目标如何跨多次执行持续推进；
- 任务暂停后如何在正确时间或正确事件发生时恢复；
- 进程重启后如何保留任务状态；
- 用户如何查询、取消和补充信息；
- 如何只在外部状态发生有意义变化时提醒用户；
- 如何实时展示一次执行的进度，又不让连接生命周期绑住持久任务；
- 如何保证长期运行仍有预算、期限、幂等、身份隔离和可解释终态。

最终目标不是制造一个永不停止的 LLM 循环，而是建立一个可恢复、可观察、可暂停、可取消、受治理的
目标执行系统。

## 2. 最终能力定义

长时 Agent 完成后，应支持以下生命周期：

```text
用户提出目标
  -> 收集缺失约束
  -> 形成有界计划
  -> 持久化任务
  -> 执行一个有限 quantum
  -> checkpoint
  -> 等待时间 / 外部事件 / 用户输入
  -> 被可靠唤醒
  -> 读取可信任务快照
  -> 执行下一个有限 quantum
  -> 必要时重新规划
  -> 完成、失败、取消或进入 outcome_unknown
```

用户至少能够：

- 查询任务当前状态和最近事件；
- 知道任务正在等待什么；
- 知道下一次检查时间；
- 补充任务输入；
- 取消任务；
- 查看最终结果和重要中间产物；
- 在断线或进程重启后继续访问同一任务。

## 3. 明确非目标

以下内容不作为第一阶段目标：

- 无限期自主运行；
- 无预算、无期限的开放式目标；
- 让一个 `AsyncGenerator`、WebSocket 或 Python 进程挂起数天；
- 分布式 exactly-once 执行；
- 自动付款、预订、发送邮件或其他尚未另立授权设计的写操作；
- 用自然语言字符串代替结构化等待条件；
- 让 LLM 决定安全、幂等、身份或 lease 规则；
- 为长时任务建立第二套 Tool 执行链路；
- 为了未来扩展提前引入 Celery、Temporal、Kafka 等基础设施；
- 同时实现旅行、采购、通勤、邮件管理等所有场景。

## 4. 当前基础与缺口

### 4.1 已有基础

当前项目已经实现：

- `DurableTaskService`、SQLite `TaskStore`、任务/计划/步骤/事件契约；
- `DurableTaskWorker` 的 lease、单 quantum 执行、checkpoint 和崩溃恢复；
- read-only 步骤的有界重试；
- 可能产生外部副作用的中断步骤进入 `outcome_unknown`；
- 用户输入、取消和 identity-scoped task API；
- `TaskEvent` 的 cursor 分页读取；
- `ProactiveWakeCoordinator`、WakeRule、WakeSignal、变化检测、注意力策略和通知 outbox；
- `AgentRunStream`、`AgentEvent`、Realtime event 和 Gateway frame 映射；
- Gateway 与 durable task 生命周期分离；
- `ActionValidator -> ToolExecutor -> ToolRegistry -> Tool` 治理链路；
- mock/real Provider 边界和离线 pytest 安全网。

### 4.2 后续生产加固

阶段 0—7 已在 mock/local/offline 边界形成闭环。尚未纳入本路线的生产工作包括：

- 选择并实现真实酒店 Provider adapter 与显式 system eval；
- 将 cursor tail/replay adapter 接到具体客户端 transport；
- 接入真实通知渠道并完成 operator 配置；
- 提供 operator 级任务积压、lease、重试和 dead-letter 汇总；
- 若未来需要外部写操作，另立新的授权协议设计，不恢复旧确认机制。

## 5. 三个必须分开的概念

### 5.1 DurableTask：目标执行状态

负责：

- 用户目标、约束和计划；
- 当前步骤和 artifact；
- lease、attempt、预算和 deadline；
- checkpoint、恢复、取消和输入；
- 任务终态。

DurableTask 是“任务进行到哪里”的事实权威。

### 5.2 ProactiveWake：外部信号和注意力

负责：

- 定时 reconcile 或 Provider event；
- 只读 probe；
- 新旧 evidence fingerprint 比较；
- 是否值得通知；
- quiet hours、cooldown 和每日通知上限；
- durable notification outbox 和投递重试。

ProactiveWake 是“外部发生了什么，以及是否应该打扰用户”的事实权威。

### 5.3 AgentRunStream / AsyncIterator：一次运行的实时进度

负责：

- 当前 quantum 的 started/progress/tool/final/error 等实时事件；
- 向 Realtime/Gateway 或其他在线消费者传递进度；
- 在连接存在时提供低延迟体验。

它不负责：

- 跨进程保存任务；
- 几天后唤醒；
- 作为任务终态权威；
- 让一个生成器长期挂起。

项目继续使用“事件流与终态结果分离”的约束。若为 durable quantum 增加异步流，应复用
`AgentRunStream` 的模式，而不是暴露只有事件、没有可靠 terminal result 的裸异步生成器。

## 6. 目标架构

```text
入口：CLI / HTTP / WebSocket / App
                |
                v
         Gateway ingress turn
                |
                v
      task_plan_submit / task handle
                |
                v
        DurableTaskService + SQLite
                |
       queued / waiting / terminal
                |
                +----------------------------+
                |                            |
                v                            v
       DurableTaskWorker              ProactiveWake
       claim one due task       signal/probe/change/attention
                |                            |
                v                            v
      run one bounded quantum        resume request or notification
                |                            |
                +-------------+--------------+
                              v
                         checkpoint
                              |
                +-------------+--------------+
                |                            |
                v                            v
        persisted TaskEvent          NotificationEnvelope
                |                            |
                v                            v
       polling / async tail             delivery worker
```

关键边界：

- Gateway 只负责接受任务的那一轮，不把 durable task 保持为 active Gateway run；
- DurableTaskWorker 每次只执行一个有界 quantum；
- 所有 Tool 调用继续经过统一治理链路；
- ProactiveWake 不直接修改任务内部状态，必须通过结构化 resume 接口；
- DurableTask 不直接实现渠道发送，通知必须进入 delivery outbox；
- 客户端断线不取消 durable task；
- 任务取消必须阻止未来 claim，并对当前 quantum 发出 cooperative cancel。

## 7. 目标状态模型

当前状态尽量保留，未来按实际阶段增量增加，不进行一次性全量重写。

建议的任务状态集合：

```text
queued
running
waiting_schedule
waiting_external_event
waiting_input
replanning
outcome_unknown
completed
failed
cancelled
```

核心转换：

```text
queued -> running
running -> waiting_schedule
running -> waiting_external_event
running -> waiting_input
running -> replanning
running -> completed
running -> failed
running -> outcome_unknown

waiting_schedule -> queued
waiting_external_event -> queued
waiting_input -> queued
replanning -> queued | failed

任何非终态 -> cancelled
```

建议新增结构化等待契约，具体字段在实施阶段复核：

```python
class TaskWaitState(BaseModel):
    kind: Literal[
        "schedule",
        "external_event",
        "input",
    ]
    reason_code: str
    summary: str
    next_eligible_at: datetime | None = None
    wake_rule_id: str | None = None
    expires_at: datetime | None = None
```

约束：

- `next_eligible_at` 使用 UTC 存储；
- 用户时区只用于输入解释和展示；
- wait 必须有 reason code；
- schedule/event wait 必须有过期或任务总 deadline；
- terminal task 不保留可 claim 的 wait；
- wait 信息进入公共 projection 时必须移除内部 token、binding 和敏感参数。

## 8. 事件模型

优先复用现有 `TaskEvent(event_type, status, payload, cursor)`，新增事件保持结构化、可回放：

```text
task.accepted
task.quantum_admitted
task.quantum_started
task.wait_scheduled
task.waiting_external_event
task.wake_received
task.resumed
task.input_required
task.input_received
step.started
step.succeeded
step.failed
notification.enqueued
task.completed
task.failed
task.cancelled
task.outcome_unknown
```

事件规则：

- store transaction 成功后，事件才算发生；
- 实时推送失败不回滚已提交任务状态；
- cursor 是恢复读取的权威，不以 WebSocket sequence 代替；
- payload 只保存 prompt-safe、API-safe 数据；
- Provider 原始响应、凭据、完整邮件正文等不得写入 task event；
- terminal result 以 TaskRecord/Bundle 为权威，不能仅从事件推断。

## 9. 分阶段实施

每一阶段都必须能够独立合并、独立验收。上一阶段未通过退出门槛时，不进入下一阶段。

### 阶段 0：冻结边界与建立基线

目标：

- 确认 DurableTask、ProactiveWake、AgentRunStream、Gateway 的责任边界；
- 为当前状态机、lease、checkpoint、事件分页建立基线测试；
- 记录当前已有能力，避免重复实现。

交付：

- 本计划；
- 当前状态转换表；
- 邻近测试清单；
- 不改变生产行为。

退出门槛：

- 全量离线 pytest 通过；
- 没有引入第二套 Tool 或 Gateway 生命周期。

### 阶段 1：可恢复的定时等待

首个垂直案例：

> 创建一个本地 mock 提醒任务，在指定时间后恢复，并生成一次通知请求。

目标：

- 增加 `waiting_schedule` 和结构化 wait；
- SQLite 只 claim `next_eligible_at <= now` 的任务；
- 进程重启后仍能 claim 到期任务；
- 未到期任务不会消耗 model/tool budget；
- 到期后只恢复一个 quantum；
- 取消后永不恢复。

第一版可以完全确定性执行，不调用 LLM 或真实 Provider。

退出门槛：

- 临时 SQLite 重启恢复测试通过；
- 到期前不执行、到期后执行一次；
- 重复 worker tick 不产生重复通知；
- 取消和 deadline 生效；
- 不使用长时间 `sleep` 保持任务。

### 阶段 2：TaskEvent 实时订阅

目标：

- 保留现有 cursor HTTP 查询；
- 增加基于 cursor 的异步 tail/replay adapter；
- 在线时实时收到 task progress；
- 断线重连从最后 cursor 补读；
- 等待状态出现后结束当前订阅或进入低成本 store tail，不保持 Agent runtime。

推荐接口形态：

```python
subscription = task_events.subscribe(task_id, after=cursor)
async for event in subscription:
    consume(event)
```

该订阅是持久事件的投影视图，不拥有任务状态。

退出门槛：

- 事件顺序与 store cursor 一致；
- 慢消费者有明确上限或断开策略；
- 订阅取消不取消 durable task；
- terminal 后正常结束；
- 重连不重复业务副作用。

### 阶段 3：通知 outbox 闭环

目标：

- task quantum 只能请求通知，不能直接调用渠道；
- 通知写入 `NotificationEnvelope` outbox；
- delivery worker 负责 lease、重试、过期和 dead-letter；
- mock channel 完成端到端验证；
- task event 记录 notification enqueue/delivery 的安全引用。

退出门槛：

- 相同 idempotency key 不重复发送；
- 进程重启后未发送通知可以继续投递；
- 通知失败不伪造任务成功；
- 用户身份和 destination 不串扰；
- 默认测试不访问真实渠道。

### 阶段 4：DurableTask 与 ProactiveWake 恢复协议

目标：

- DurableTask 可以进入 `waiting_external_event`；
- wait 引用明确的 `wake_rule_id`；
- ProactiveWake 在 signal/probe/change/attention 判定后产生结构化 resume request；
- resume 必须校验 task、owner、wait version、rule 和期限；
- 重复 WakeSignal 不重复恢复同一 wait；
- 无显著变化时任务继续等待。

建议的逻辑协议：

```text
WakeDecision(notify or resume)
  -> TaskResumeRequest(
       task_id,
       expected_task_version,
       wake_rule_id,
       evidence_ids,
       evidence_fingerprint,
     )
  -> DurableTaskService.resume_wait(...)
  -> queued
```

退出门槛：

- stale signal 不能恢复新版 wait；
- owner 不匹配时 fail closed；
- 相同 evidence fingerprint 幂等；
- rule 禁用、过期或任务取消后不能恢复；
- probe 仍经过只读 Tool 治理。

### 阶段 5：第一个真实长时业务场景

推荐场景：

> 酒店价格持续监控，但第一版不预订。

需要的原子能力：

- `lodging_search`；
- 确定性预算/变化计算；
- structured hotel watch goal；
- 周期检查；
- 价格快照和 evidence fingerprint；
- 达到阈值后通知；
- 到出发日期或用户取消时终止。

在没有稳定酒店 Provider 前，可先使用 mock adapter 完成全部 pytest，再通过显式 real system eval
验证真实连通性。

退出门槛：

- mock 端到端场景跨重启通过；
- 价格不变时保持静默；
- 达到阈值时只通知一次；
- Provider 超时进入可解释等待或有界失败；
- 任务到期后停止；
- 没有预订或付款写操作。

### 阶段 6：撤销旧确认机制

旧的工具级确认和 durable confirmation 模型已按架构决定删除，包括
`ToolSpec.requires_confirmation`、`TaskConfirmation`、`waiting_confirmation`、
确认 API 及其 realtime projection。长时 Agent 当前只执行只读或本地确定性 workflow；
不会借助 metadata、模型参数或兼容层执行受控写操作。

未来若引入写能力，必须另立设计门槛，先定义独立的授权主体、授权范围、幂等与
`outcome_unknown` 协议，不能恢复或换名复制旧确认机制。

退出门槛：

- 源码和稳定协议不再暴露旧确认字段、状态或 API；
- durable workflow 没有隐式 write/dangerous 执行入口；
- 外部结果不确定的既有写边界仍禁止盲目重试。

### 阶段 7：第二、第三个场景与抽象验证

只有第一场景稳定后，才增加：

- 邮件承诺与待办跟踪；
- 通勤异常监控；
- 采购价格与预算监控；
- 旅行行前动态调整。

目的不是增加 Tool 数量，而是验证长时任务内核是否能复用。若新场景要求修改核心状态机，应先判断：

- 是真正缺失的通用语义；
- 还是场景自己的 schema/adapter；
- 是否可以保留在对应 Tool Plugin；
- 是否会破坏已有任务恢复兼容性。

当前验收使用通勤异常监控和邮件承诺变化监控：二者都通过 allowlisted read Tool、
`ActionValidator -> ToolExecutor -> ToolRegistry -> Tool`、ProactiveWake change detection 与同一
notification outbox 完成“初始基线静默、变化通知、未变化继续静默”。该验证只增加场景规则和测试工具，
没有修改核心状态机或新增第二套 store。

## 10. 测试与评测策略

### 10.1 pytest

pytest 全部保持 mock/local/offline。

重点契约：

- 状态转换；
- SQLite 持久化与 restart recovery；
- lease claim/reclaim；
- due-time 判断；
- identity 隔离；
- wait version 和 wake fingerprint 幂等；
- 事件 cursor 顺序；
- 取消；
- notification outbox；
- uncertain write outcome；
- budget/deadline。

测试归属遵循 `tests/README.md`：

- 单一纯状态转换可放 `tests/unit/`；
- service/store/worker/runtime 协作放 `tests/integration/`；
- API、schema、状态和身份等稳定边界放 `tests/contract/`。

### 10.2 system eval

只有真实 Tool 连通性需要 `evals/system`，并且必须：

- operator 显式启用；
- `MULTIMODAL_AGENT_PROVIDER_MODE=real`；
- 使用本机未跟踪配置；
- 不把真实用户邮件、订单或位置数据提交到仓库；
- 最终报告真实调用范围。

### 10.3 Langfuse case eval

只有以下模型行为质量问题进入 Langfuse case eval：

- 是否正确询问长期目标缺失约束；
- 是否生成有界、可执行计划；
- 是否在无显著变化时保持克制；
- 是否给出清晰的状态解释。

确定性调度、lease、幂等和状态转换不得依赖 LLM eval 证明。

## 11. 可观测性要求

至少提供以下 prompt-safe 指标或事件摘要：

- queued/waiting/running/terminal task 数量；
- 最老 queued task age；
- overdue wait 数量；
- active/expired lease 数量；
- quantum duration；
- step attempt 和 retry 数量；
- notification queued/retry/dead-letter 数量；
- wake signal dedupe 数量；
- outcome_unknown 数量；
- cancellation latency；
- task terminal reason code。

不得记录：

- Provider credential；
- 完整邮件正文；
- 用户精确位置历史；
- 原始 Provider response；
- write Tool 的敏感完整参数；

## 12. 安全和治理不变量

- Tool 调用不绕过 `ActionValidator -> ToolExecutor -> ToolRegistry -> Tool`；
- mock 模式不调用真实 Provider；
- real 模式不回退 mock；
- read probe 与 write action 分离；
- 外部内容一律作为不可信 evidence；
- 任务身份来自可信 runtime/API context，不来自 write body；
- 每个任务有总 deadline、model/tool call budget 和 step attempt budget；
- 每个 wait 有过期或受任务总 deadline 约束；
- 当前 durable workflow 不执行尚未另立授权设计的外部写操作；
- 不确定的写结果进入 `outcome_unknown`；
- 取消不会回滚已提交外部副作用；
- Gateway 断线不取消 durable task；
- task event 和 notification 都必须可脱敏、可审计。

## 13. 演进与兼容策略

- schema 只做增量字段和显式版本迁移；
- SQLite migration 必须可从现有版本升级；
- 旧任务缺少 wait 字段时按非等待任务读取；
- 新 worker 不应错误 claim `waiting_input` 任务；
- API 继续保留现有 task/status/events/input/cancel 入口；
- 新实时订阅是增量能力，不替代 cursor 查询；
- 不为新场景建立第二个 durable task store；
- ProactiveWake 与 DurableTask 先通过显式协议协作，不直接共享私有表；
- 分布式执行只有在单机真实瓶颈出现后另立设计门槛。

## 14. 风险与应对

| 风险 | 应对 |
| --- | --- |
| 把长任务做成永久运行的 coroutine | 每个 quantum 有界，等待即 checkpoint 并释放 runtime |
| 重复唤醒造成重复副作用 | wait version、signal/evidence fingerprint、step idempotency |
| 进程重启丢任务 | SQLite 状态、lease expiry、restart integration test |
| 写操作结果不确定 | 调用前记录 attempt，非只读中断进入 `outcome_unknown` |
| 通知轰炸 | change detection、cooldown、quiet hours、daily limit |
| LLM 无限重新规划 | plan revision budget 和明确失败终态 |
| 状态流与终态不一致 | store/result 为权威，stream 只做投影 |
| Gateway 变成长任务调度器 | 保持 Gateway 与 durable task 生命周期分离 |
| 为每个场景修改核心 | 场景 schema/backend 留在 Tool Plugin，核心只接收稳定通用语义 |
| 提前引入分布式基础设施 | 单机 SQLite 通过真实验收后再评估 |

## 15. 每次继续开发时的检查清单

开始一个长时 Agent 相关任务前：

1. 读取本计划；
2. 读取对应根级权威文档；
3. 检查当前处于哪个实施阶段；
4. 只选择一个未完成阶段或一个明确子目标；
5. 搜索当前源码和邻近测试，以源码为事实权威；
6. 明确本次是否改变 schema、状态、事件、SQLite、Gateway 或 Tool 行为；
7. 明确不在本次范围内的后续阶段；
8. 完成定向测试和全量离线 pytest；
9. 更新本计划中的进度记录；
10. 行为已经成为当前事实后，再同步对应根级权威文档。

## 16. 进度记录

### 当前状态

- [x] 阶段 0：形成总体计划并明确系统边界
- [x] 阶段 1：可恢复的定时等待
- [x] 阶段 2：TaskEvent 实时订阅
- [x] 阶段 3：通知 outbox 闭环
- [x] 阶段 4：DurableTask 与 ProactiveWake 恢复协议
- [x] 阶段 5：首个真实长时业务场景
- [x] 阶段 6：删除旧确认机制并关闭 durable 隐式写入口
- [x] 阶段 7：更多场景与抽象验证

### 完成证据

- 阶段 1—3：`test_durable_task_schedule.py`、
  `test_durable_task_event_subscription.py`、
  `test_durable_task_notification_outbox.py`；
- 阶段 4：`test_durable_task_proactive_resume.py`；
- 阶段 5：`test_hotel_price_watch.py`；
- 阶段 6：旧确认字段、状态、API、Realtime/Gateway 投影及相关 eval metadata 已删除；
- 阶段 7：`test_long_running_scenario_reuse.py` 以通勤异常和邮件承诺变化两个场景验证复用。

### 下一步唯一建议

当前路线的阶段 0—7 已完成。下一次实施只做真实连通与运行加固：

> 在用户明确选择酒店 Provider、提供未跟踪本地配置并显式启用 real mode 后，增加
> `lodging_search` adapter 与 system eval；在此之前继续以 mock/local/offline 契约为权威，
> 不伪造真实价格能力。

当前仍不新增预订/付款写工具、不自动启用真实 Provider、不把 Gateway 改造成任务运行器。
新的写授权协议必须由独立设计任务明确批准，不能恢复旧确认机制。
