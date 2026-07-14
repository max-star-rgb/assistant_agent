# Realtime 语义中断仲裁设计

## 文档状态

本文档定义 `assistant_agent` 实时通话中的双通道中断产品语义、并行 LLM 仲裁控制面，以及它与 Gateway QueuePolicy、当前 `AgentGraphRuntime`、Media Relay 和 realtime task-state 的责任边界。

产品原则已经确认：

- 显式中断由媒体或用户控制信令触发，Gateway 立即、确定性执行，不调用 LLM。
- 隐式中断由独立 LLM 并行判断，不阻塞当前业务 run 的继续计算。
- 仲裁结果先交给 Gateway 控制面；v1 不把新语义实时注入正在执行的 `AgentGraphRuntime`。
- 修订或替换任务时，先取消旧 run，等旧 backend 真正退出并释放准入 permit，再启动 replacement run。
- 默认仍把未确认的插话当作新增任务，不因低置信度或仲裁失败误杀当前任务。

本文档只授权 mock/local/offline 默认路径和显式 `provider_smoke` / `pilot` 下的 LLM 仲裁适配。它不授权自动启用真实 Provider，不改变工具治理、记忆治理或外部副作用规则。

## 1. 问题定义

当前 Gateway 能识别显式 `interrupt=true`、`control=interrupt|barge_in|cancel_previous` 和 session interrupt policy。没有显式控制的 `message.user` 在 active run 存在时按 FIFO followup 排队。

实时通话还存在第二类输入：用户没有操作“打断”按钮，也没有媒体显式控制，但话语本身明显要求停止、修改或替换正在执行的任务，例如：

- “先别查了。”
- “不是北京，改成上海。”
- “这个不要了，帮我订明天的提醒。”
- “嗯，你继续。”
- “查完之后再给我总结一下。”

这些话语不能全部视为显式 interrupt，也不能全部视为普通 followup。系统需要理解新话语与 active task 的关系，并将“语义来源”和“生命周期动作”分开建模。

## 2. 产品语义

### 2.1 中断来源

`interrupt_source` 只说明为什么进入中断流程：

- `explicit_control`：媒体信令、按钮、`run.cancel`、hangup 或显式 Gateway control。
- `semantic_llm`：独立 `RealtimeTurnArbiter` 根据新话语和 prompt-safe active task snapshot 得出的语义判定。

显式来源优先级最高，永远不等待、也不被 LLM 覆盖。语义来源不能伪造成显式媒体信令。

### 2.2 仲裁处置

`disposition` 说明 Gateway 应如何处理新 turn 和 active run：

| disposition | active run | 新业务 run | 含义 |
| --- | --- | --- | --- |
| `FOLLOWUP` | 继续 | FIFO 排队 | 独立新增任务或后续任务 |
| `CANCEL_ONLY` | 取消 | 不启动 | 只要求停止当前任务 |
| `REVISE_ACTIVE` | 取消 | 旧 run 退出后启动 | 保留当前目标，追加/替换约束或澄清 |
| `REPLACE_ACTIVE` | 取消 | 旧 run 退出后启动 | 用新目标替换当前目标 |
| `ACK_NOOP` | 继续 | 不启动 | “嗯”“知道了”等不形成业务任务的反馈 |
| `UNCERTAIN` | 继续 | FIFO 排队 | 证据不足；按安全 fallback 处理 |

`UNCERTAIN` 的产品 fallback 等同 `FOLLOWUP`，但观测数据必须保留原始 disposition，以便区分“模型明确判断新增任务”和“系统因不确定而保守排队”。

`CANCEL_ONLY` 和 `ACK_NOOP` 已经是 Gateway 接受并分配身份的 turn，因此必须有确定终态：

- 新控制 turn 发送 `run.end(reason=completed)`，payload 包含 `handled_by=turn_arbiter`、prompt-safe arbitration summary 和 `expects_reply=false`，但不发送 `run.started`，也不调用 backend。
- `CANCEL_ONLY` 另外使旧 active run 按既有 cancellation contract 发送自己的 `run.end(reason=cancelled)`。
- 该终态释放新 turn 的 queue reservation，并保持 dedupe record 可识别为 terminal，不能用“静默丢弃”代替。

### 2.3 revision 类型

进入 replacement run 时，仲裁器可以输出已有 `IntentRevisionType`：

- `add_constraint`
- `replace_constraint`
- `change_goal`
- `cancel_goal`
- `confirm`
- `clarify`

约束规则：

- `REVISE_ACTIVE` 默认 `add_constraint`，允许 `replace_constraint|confirm|clarify`。
- `REPLACE_ACTIVE` 固定规范化为 `change_goal`。
- `CANCEL_ONLY` 固定规范化为 `cancel_goal`，但不进入业务 runtime。
- 非法组合、未知值或缺失值必须确定性规范化，不能把未验证的模型输出直接注入 runtime metadata。

task-state 更新规则：

- `add_constraint`：保留 objective，并追加裁剪后的新话语作为约束。
- `replace_constraint`：保留 objective，用新话语替换当前约束集合。
- `confirm|clarify`：记录 revision，但不自动把整句话变成新约束。
- `change_goal`：用新话语替换 objective，清空旧 constraints，将旧 task artifact 标为 stale；side-effect records 必须保留。
- `cancel_goal`：把 task status 置为 `cancelled`，记录 revision 和已提交副作用，但不创建 replacement run。

`CANCEL_ONLY` 不进入 assistant runtime，因此必须通过 realtime task-state service 的窄接口记录 `cancel_goal`，Gateway 不直接访问或拼装 store 内部状态。

## 3. 时延定义

并行仲裁只消除对当前业务 run 计算的阻塞，不消除语义中断本身的判定时间。

```text
semantic_interrupt_apply_latency
  = stable_transcript_latency
  + arbiter_provider_latency
  + gateway_apply_latency
```

系统分别观测：

- `active_run_added_latency_ms`：当前 run 因仲裁增加的计算等待，目标为 0。
- `media_duck_latency_ms`：用户开口到 Media Relay 停止/压低 TTS 的时间；由媒体层负责。
- `arbitration_latency_ms`：Gateway 启动仲裁到得到规范化 decision 的时间。
- `interrupt_apply_latency_ms`：收到稳定文本到 Gateway 发出 active run cancel 的时间。
- `replacement_start_latency_ms`：发出 cancel 到 replacement run 获得 backend permit 的时间，包含旧 backend 退出等待。

“几乎没有时延”只适用于第一项；产品体感依靠 Media Relay 立即 audio duck，而不是假设 LLM 已经完成语义判断。

独立控制面不等于物理资源完全隔离。如果仲裁和当前 run 共享 Provider 配额、连接池、线程池或本机 CPU，它仍可能产生间接竞争；因此必须用独立并发上限和指标验证，而不能把“0 增量时延”当作未经测量的 SLA。

## 4. 分层架构

```text
realtime Media Relay / app
    |
    | transcript.final / explicit interrupt
    v
GatewaySessionService
    |
    |-- explicit control --------------------> immediate lifecycle cancel
    |
    |-- ordinary turn, no active run --------> normal admission / backend run
    |
    `-- ordinary turn, active run
          |
          | accepted as Gateway queued turn
          | no business permit consumed
          v
       RealtimeTurnArbitrationController
          | bounded control-plane concurrency
          v
       RealtimeTurnArbiter
          | deterministic fallback, or
          | LLM adapter in provider_smoke/pilot
          v
       normalized ArbitrationDecision
          |
          v
       Gateway compare-and-apply(expected_run_id)
          |
          |-- FOLLOWUP / UNCERTAIN -> FIFO remains
          |-- ACK_NOOP            -> finish control turn, no backend
          |-- CANCEL_ONLY         -> cancel active, finish control turn
          `-- REVISE / REPLACE    -> reprioritize replacement, cancel active
                                      |
                                      v
                         wait old backend exit / release permit
                                      |
                                      v
                         GatewayAgentAdapter -> AgentGraphRuntime
```

### 4.1 Media Relay

Media Relay 负责音频体感，不负责业务语义：

- 检测用户开口后可以立即暂停或压低 TTS。
- 显式按钮或媒体 control 继续发送明确 interrupt 信令。
- 对隐式话语只发送稳定 transcript，不自行判断 `FOLLOWUP`、`REVISE_ACTIVE` 或 `REPLACE_ACTIVE`。
- 仲裁为 followup/noop 时，媒体可以按自己的播放策略恢复尚未失效的语音；仲裁为 cancel/revise/replace 时，旧输出保持不可播放。

当前仓库 realtime v1 不拥有真实 TTS provider，因此这里只维护 Gateway/control contract 和 capability metadata，不在 Python Gateway 中伪造音频播放控制。

### 4.2 Gateway

Gateway 继续是唯一 run 生命周期权威：

- 为新消息分配稳定 `turn_id/run_id`，并按现有容量规则 reserve queue slot。
- 显式中断绕过仲裁。
- 仅在可信 realtime entry capability 已声明支持、session 已启用且 active run 存在时启动语义仲裁。
- 仲裁任务属于控制面，不获取 backend run permit，也不启动第二个 `AgentGraphRuntime`。
- 使用 `expected_run_id` compare-and-apply，避免迟到 decision 取消错误的新 run。
- 对 replacement turn 仍保持同 session backend 最大并发为 1。
- 对仲裁输出做 schema、枚举、置信度和长度校验；模型不能直接改写 Gateway 内部状态。

### 4.3 RealtimeTurnArbiter

仲裁器只回答“新话语与 active task 的关系”，不执行工具、不读取长期记忆、不规划业务任务：

- 输入只包含新话语、`expected_run_id`、语言和 prompt-safe realtime task-state snapshot。
- 不包含完整 conversation、长期 memory、raw tool result、provider raw response、秘密或媒体二进制。
- LLM request 不声明 tools，使用结构化 JSON response、`temperature=0` 和小型 token budget。
- 输出由本地 Pydantic contract 验证并重新绑定可信 `expected_run_id`。
- Provider error、JSON 无效、超时、低置信度或控制面饱和都返回 `UNCERTAIN` fallback。

默认 mock/local/offline profile 使用确定性 fallback，不把 mock 行为宣传成真实语义判断。只有 `provider_smoke` 或 `pilot`、非 mock chat adapter 且功能显式启用时才调用真实 LLM。

### 4.4 AgentGraphRuntime

v1 中当前 runtime 不接收新的自然语言 prompt 注入：

- `CANCEL_ONLY|REVISE_ACTIVE|REPLACE_ACTIVE` 最多向当前 runtime 传播合作式 cancel token。
- `REVISE_ACTIVE|REPLACE_ACTIVE` 的结构化 arbitration metadata 和新用户文本只进入 replacement run。
- replacement run 在入口通过现有 realtime task-state snapshot 获取旧目标、约束、可复用 artifact、pending tool 和 side-effect 摘要。
- 现有 validator、executor、tool registry、policy 和 audit 边界保持不变。

如果未来要让当前 run 原地改目标继续，必须单独设计 live steer：runtime mailbox、安全 checkpoint、context revision、tool commit barrier、输出版本和 revision ordering。该能力不属于本设计。

## 5. 数据契约

### 5.1 ArbitrationRequest

```json
{
  "schema_version": "realtime_turn_arbitration_v1",
  "decision_id": "opaque-id",
  "user_id": "bound-user",
  "session_id": "bound-session",
  "turn_id": "new-turn",
  "run_id": "new-run",
  "expected_run_id": "active-run",
  "utterance": "不是北京，改成上海",
  "language": "zh-CN",
  "task_state": {
    "objective": "查询北京周末天气并规划行程",
    "constraints": [],
    "pending_tool": null,
    "tts_state": "speaking",
    "committed_side_effect_count": 0
  }
}
```

所有文本和列表沿用 realtime task-state 的 prompt-safe 裁剪上限。`user_id/session_id/run_id` 用于本地关联，不要求模型原样回传。

### 5.2 ArbitrationDecision

```json
{
  "schema_version": "realtime_turn_arbitration_v1",
  "decision_id": "opaque-id",
  "source": "semantic_llm",
  "disposition": "REVISE_ACTIVE",
  "revision_type": "replace_constraint",
  "confidence": 0.93,
  "reason_code": "corrects_active_constraint",
  "expected_run_id": "active-run",
  "latency_ms": 184,
  "fallback_reason": null
}
```

`reason_code` 必须是短、稳定、prompt-safe 的机器码，不保存模型长篇解释。运行时 metadata 只保留上述结构化字段，不保留仲裁 prompt 或 Provider raw payload。

### 5.3 replacement metadata

`REVISE_ACTIVE|REPLACE_ACTIVE` 规范化到新 turn 的 metadata：

```json
{
  "control": "interrupt",
  "barge_in_source": "transcript",
  "realtime_turn_arbitration": {
    "schema_version": "realtime_turn_arbitration_v1",
    "decision_id": "opaque-id",
    "source": "semantic_llm",
    "disposition": "REVISE_ACTIVE",
    "revision_type": "replace_constraint",
    "confidence": 0.93,
    "reason_code": "corrects_active_constraint",
    "expected_run_id": "active-run"
  }
}
```

该 metadata 只告诉 replacement run 发生了什么；它不赋予模型绕过工具治理或复用已提交副作用的权力。

## 6. 并发、队列与竞态

### 6.1 queued turn 状态

新消息到达 active session 后仍先成为受现有上限、timeout、dedupe 和 `run.cancel` 管理的 `QueuedTurn`。仲裁期间：

- 占用 accepted queued-turn reservation，防止绕过全局队列容量。
- 不申请 active backend permit。
- 标记 `arbitration_pending=true`，不能被 session promotion 提前启动。
- 仲裁 task 需要在 turn cancel、queue timeout、session close 和 hangup 时清理或失效。

### 6.2 decision compare-and-apply

应用 decision 前必须在同一 Gateway lock 内验证：

- turn 尚未 terminal；
- turn 仍属于原 session；
- `expected_run_id` 仍然是该 session 的 current/active run；
- decision sequence 仍是该 turn 的最新 sequence。

验证失败时不得取消当前新 run：

- 若旧 run 已结束但新 turn 尚未启动，将 decision 规范化为 `FOLLOWUP` 并按 FIFO 继续。
- 若 turn 已取消/超时，丢弃 decision。
- 若 session 已 hangup/close，丢弃 decision 并只记录 prompt-safe stale outcome。

### 6.3 多次连续插话

同一 active run 可能同时存在多个待仲裁 turn。v1 规则：

- 每个 accepted turn 有独立 `decision_id` 和 timeout。
- 第一条成功 compare-and-apply 的 cancel/revise/replace decision 赢得 active run 的中断权。
- 后续针对旧 `expected_run_id` 的 decision 不能取消 replacement run；它们保守降级为 FIFO followup。
- 显式中断到达时立即生效，并使同一旧 run 上所有隐式仲裁结果失效。

### 6.4 控制面容量

仲裁使用独立、进程级有界并发，不占业务 `max_active_runs`，也不能无限创建 Provider 请求：

- `max_concurrent_arbitrations` 限制实际 in-flight LLM 调用。
- `arbitration_timeout_ms` 限制用户等待 decision 的时间。
- 超时的底层同步 Provider 调用若无法硬取消，仍占用控制面 slot，直到真实调用返回；不能超时后立即释放 slot 并无限堆积后台线程。
- 控制面饱和时立即 `UNCERTAIN -> FOLLOWUP`，显式中断仍然可用。

## 7. 输出、工具和副作用

### 7.1 输出可见性

- 显式中断或语义 cancel/revise/replace 一旦 apply，现有 Gateway cancel output gate 保证旧 run 后续 chunk 不再可见/可播放。
- 仲裁尚未完成时，当前 run 可以继续内部计算；Media Relay 已因用户开口暂停/压低 TTS。
- v1 不在 Gateway 内为所有 text chunk 增加可回放缓冲区。外部 realtime Media Relay 必须在用户发言期间管理 TTS hold/resume；普通 text Gateway entry 不启用语义仲裁。

### 7.2 工具边界

- cancel 是 best effort，不能回滚已经提交的外部副作用。
- 仲裁 snapshot 只包含 `pending_tool` 的状态摘要和已提交 side-effect 计数，使模型能减少错误替换，但模型判断不能改变工具实际 commit 状态。
- replacement runtime 仍根据 `ContinuationStrategy` 决定 `restart|reuse_and_replan|resume_from_checkpoint|ask_confirmation|compensate|report_committed`。
- 已提交副作用必须报告，不得因“打断成功”而宣称已经撤销。

## 8. 配置与启用条件

建议新增严格校验的进程配置：

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_ENABLED` | `false` | 全局显式启用语义仲裁 |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_TIMEOUT_MS` | `1000` | 单次 decision 等待上限 |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MAX_CONCURRENCY` | `2` | 进程级控制面并发上限 |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MIN_CONFIDENCE` | `0.80` | 低于该值规范化为 `UNCERTAIN` |

实际调用 LLM 必须同时满足：

1. 全局配置启用；
2. entry capability 为 `supports_semantic_interrupt=true`；
3. session 当前存在 active run；
4. 新消息不是显式 interrupt/control；
5. runtime profile 是 `provider_smoke` 或 `pilot`；
6. chat adapter 不是 mock/unconfigured。

其中任一条件不满足时，普通消息继续走现有 FIFO followup；显式中断不受影响。

## 9. 可观测性

新增 lifecycle 事件：

- `gateway.turn.arbitration.started`
- `gateway.turn.arbitration.finished`
- `gateway.turn.arbitration.fallback`
- `gateway.turn.arbitration.stale`

允许记录：decision id、session/run/turn id、source、disposition、规范化 disposition、confidence bucket、reason code、latency、fallback reason、expected-run match。禁止记录：utterance、完整 task-state、prompt、memory、raw tool result、Provider raw response。

建议聚合指标：

- disposition 分布；
- false-interrupt 人工标注率；
- arbitration p50/p95 latency；
- timeout/provider error/saturation fallback 率；
- stale decision 率；
- decision 到 active cancel 的 apply latency；
- replacement 等待旧 backend 退出的时间。

## 10. 验收场景

### 10.1 必须通过

1. 无 active run 时普通消息直接启动，仲裁器调用次数为 0。
2. active run + 显式 interrupt 时立即取消，仲裁器调用次数为 0。
3. active run + `FOLLOWUP` 时旧 run 不取消，新 turn 保持 FIFO。
4. active run + `REVISE_ACTIVE` 时旧 run 被取消；replacement 只有在旧 backend 退出后才启动，metadata 带规范化 revision。
5. active run + `REPLACE_ACTIVE` 时 task-state 新 objective 为新话语，revision type 为 `change_goal`。
6. active run + `CANCEL_ONLY` 时旧 run 取消，新 turn 不调用 backend。
7. active run + `ACK_NOOP` 时旧 run 继续，新 turn 不调用 backend。
8. 超时、低置信度、Provider error、无效 JSON 和控制面饱和均不取消旧 run，并按 FIFO followup。
9. 仲裁返回前旧 run 已结束时，迟到 decision 不得取消下一个 run。
10. 多条隐式插话针对同一旧 run 时，旧 decision 不得误杀 replacement run。
11. queue timeout、queued `run.cancel`、hangup 和 session close 能清理 arbitration task，且不调用业务 backend。
12. lifecycle payload 和 runtime metadata 不包含新话语、完整 prompt 或 raw Provider payload。
13. 默认 mock/local/offline profile 不调用真实 Provider。

### 10.2 回归要求

- 现有 Gateway QueuePolicy、dedupe、queue timeout、global admission 和 explicit interrupt 测试保持通过。
- 现有 realtime cancellation contract、stale output suppression、task-state artifact reuse 和 side-effect 测试保持通过。
- CLI、HTTP 和普通 `/ws/gateway` 在未声明 capability 时行为不变。

## 11. 非目标

- 不实现 live prompt steer 或当前 `AgentGraphRuntime` 原地改目标。
- 不并发启动同 session 的两个业务 runtime。
- 不让仲裁器调用工具、长期记忆、外部搜索或多 agent。
- 不实现 durable/cross-worker 仲裁队列。
- 不自动撤销已提交副作用。
- 不在本仓库实现真实 Media Relay、STT 或 TTS provider。
- 不用关键词规则覆盖 LLM 的语义 disposition；本地逻辑只做 schema 规范化、安全 fallback 和显式控制优先级。

## 12. 自审记录

设计与实现完成后按产品语义、架构边界、竞态、安全降级和可观测性逐项复核，结论如下：

- 双中断原则成立：显式信令始终确定性优先；语义 LLM 只处理未带显式控制的稳定文本。
- 并行只保证不阻塞当前业务 run 的计算，不承诺语义 cancel 零延迟；Media Relay 的即时 TTS duck 仍是通话体感关键路径。
- v1 不向运行中的 `AgentGraphRuntime` 注入新 prompt。修订/替换均通过 Gateway cancel、旧 backend 退出、replacement run 串行启动实现。
- `CANCEL_ONLY` 和 `ACK_NOOP` 都有独立控制 turn 终态；`change_goal` 清约束并使旧 artifact stale，但不删除或伪装回滚已提交 side effect。
- 超时、低置信度、Provider 错误、容量饱和、非法输出和 stale decision 均不能取消 active run。

实现自审发现并修复了四项边界问题：

1. 将 disposition/revision 组合校验下沉到 Pydantic 模型，防止直接构造对象绕过规范化函数。
2. 将 capability 资格绑定到经过身份适配器验证的 `realtime_media_websocket` 来源，伪造 capability 不能启用仲裁。
3. 仲裁上下文改为固定五字段的有界投影，丢弃 artifact、raw arguments 等无关信息；极端大输入仍生成完整合法 JSON。
4. `CANCEL_ONLY` 的 cancel token 与 lifecycle reason 统一为 `semantic_cancel_only`，避免观测误分类。

自审未发现需要把 live steer、工具调用、长期记忆或第二个业务 runtime 纳入 v1 的理由。剩余风险主要是 pilot 中的真实模型误判率、Provider 共享资源竞争和 Media Relay 的实际 duck/resume 体验，应通过指标与人工标注评估，而不是继续扩大当前代码范围。

## 13. 阶段收口

v1 完成条件是：双来源 contract、独立有界 LLM 仲裁、Gateway compare-and-apply、replacement run 串行、task-state revision 适配、prompt-safe observability 和离线 scripted 测试闭环全部可验证。

只有在 pilot 数据证明大量正确修订因“取消后重启”产生不可接受的成本时，才单独评估 v2 live steer。不能仅因为仲裁与旧 run 并行，就把仲裁结果直接写进正在执行的 runtime 上下文。
