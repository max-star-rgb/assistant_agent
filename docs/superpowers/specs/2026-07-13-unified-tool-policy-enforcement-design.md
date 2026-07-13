# Unified Tool Policy Enforcement Design

Date: 2026-07-13

## 目标

在不改变当前 provider-native Agent 主链路的前提下，把已经存在于 `ToolSpec` 中的风险、确认、幂等、重试、超时、结果裁剪和数据处理声明收敛成一套真正可执行的运行时策略。

本设计解决的核心问题不是“再增加一批分类字段”，而是让同一个工具策略在普通聊天、HTTP API、CLI、MCP `tool_run` 和实时通话入口下得到一致解释和执行。

必须保留的治理链路是：

```text
provider-native tool_calls
  -> AssistantDecision
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> ToolResult / ToolObservation
  -> next LLM turn
```

Gateway 继续只负责入口协议、session/run 生命周期、cancel/interrupt 和输出门控，不负责工具选择、策略解释或 Agent 闭环。

## 背景与现状问题

当前工具契约包含两层治理信息：

```text
ToolSpec.side_effect + ToolSpec.execution
ToolSpec.policy: ToolPolicyMetadata | None
```

默认内置工具主要通过 registry `_ACTION_USAGE` 生成第一层策略；repo/user-local 工具可以通过 `@tool(..., policy=...)` 声明更丰富的第二层策略。`ToolPolicyInterpreter` 已经能够把二者转换为 `ToolPolicyView`，scheduler、catalog 和部分 trace 逻辑也已开始消费该视图。

当前缺口出现在执行边界：

- `ToolPolicyView.idempotency_required` 能正确反映 `ToolPolicyMetadata.execution.idempotency="required"`，但 `ToolExecutor` 调用 risk gate 时只传递降级后的 `ToolSideEffectPolicy`。
- `ToolExecutor` 没有把 rich policy 的 `idempotency_required` 传给 `evaluate_tool_risk()`，导致确认后的 external-write 工具可以缺少 idempotency key，并且重复调用不会命中 ledger。
- `ToolPolicyMetadata.execution.retry_count`、`timeout_s` 和 `max_result_chars` 已进入 policy view，但没有成为统一执行约束。
- confirmation gate 是否生效仍受到 realtime/source metadata 影响，因此工具固有的 approval 语义没有完全做到入口无关。
- `RealtimeToolPolicy.interruptible` 和 `commit_boundary` 已可声明，但尚未进入 policy view 和动态执行生命周期。
- `DataPolicy` 当前主要用于 local-tool 静态校验；运行时 trace/output 摘要没有完整消费其语义。

这些问题已经由 `tests/test_calendar_create_event_slice.py` 中“确认后必须提供幂等 key”和“相同 key 抑制重复提交”两个失败用例验证，不是纯理论优化。

## 与其他设计的关系

本设计只负责“模型已经选择工具以后，系统怎样一致、安全地执行”。它不重新设计本轮向模型暴露哪些工具。

工具可见性和能力分发继续由以下边界负责：

- `ToolRegistry.list_specs()` 提供 registered inventory；
- `select_prompt_tool_specs()` / 后续 qualification 实现生成 `RunToolSet`；
- `PromptCompiler` 只把 exposed ToolSpec 转换成 provider schema；
- `ActionValidator` 强制执行 `RunToolSet.executable_tool_names`。

`docs/superpowers/specs/2026-07-13-tool-qualification-identity-recall-design.md` 是 capability qualification / recall 的独立设计。本设计不复制或改变其中“模型负责语义选择，系统负责确定合法行动空间”的边界。

实时工具的完整 commit/cancel lifecycle 也应作为独立后续规格。本设计只保证 policy view 能携带现有 realtime 声明，并保持取消后旧轮输出不可口播的现有契约。

## 方案选择

### 方案 A：整体替换 ToolSpec 策略结构

删除 `side_effect`、`execution` 和 `policy` 的重叠字段，设计全新的统一 `ToolPolicy`，一次性迁移所有内置和本地工具。

优点是最终模型表面最整齐；缺点是会同时影响 registry、provider schema、scheduler、catalog、local tool loader、测试和文档，迁移面远大于当前实际问题，也会破坏已有兼容路径。

本设计不采用该方案。

### 方案 B：将现有 ToolPolicyView 提升为唯一运行时解释结果

保留现有 `ToolSpec` 输入兼容性，由 `ToolPolicyInterpreter` 负责把 legacy 和 rich policy 编译成一个只读的 `ToolPolicyView`。所有执行模块只消费该视图，不再分别读取或重新推导某几个字段。

优点是改动集中、可以增量迁移、不会更改 provider-facing tool schema，也符合当前低抽象、少量数据契约的设计原则。

本设计采用该方案。

### 方案 C：只修复 calendar 幂等参数传递

只给 `evaluate_tool_risk()` 补传 `idempotency_required`，让现有两个失败测试通过。

该方案短期成本最低，但 confirmation、retry、timeout、data policy 仍会继续各自漂移；下一个 local write tool 仍会遇到同类策略断点。

本设计不采用该方案。

## 设计原则

- `ToolSpec` 是声明契约，`ToolPolicyView` 是唯一运行时解释结果。
- `ToolExecutor` 是最终执行权威，不能信任模型或入口传入一个更宽松的 policy view。
- `ActionValidator` 继续负责本轮 allowlist、参数 schema 和工具特定语义 gate，不承担 retry、confirmation 或 idempotency ledger。
- 工具的基础风险和 approval 语义与入口无关；入口只能进一步收紧，不能静默关闭工具固有安全规则。
- 默认 read-only 工具不增加用户确认或额外 LLM 回合。
- mutating 工具只有在明确满足 confirmation 和 idempotency 规则后才能进入 `ToolRegistry.run()`。
- retry 必须同时满足“错误可重试”和“本次调用可安全重放”。
- 内部 policy metadata 不发送给 provider；provider 仍只接收输入 schema 和 prompt-safe 使用约束。
- 不引入 Gateway 工具策略、全局 registry、JSON controller、新插件框架或复杂事务调度器。

## 核心模型

### ToolSpec 保持兼容

`ToolSpec` 继续保留：

```text
side_effect: ToolSideEffectPolicy
execution: ToolExecutionPolicy
policy: ToolPolicyMetadata | None
```

其中：

- `ToolSideEffectPolicy` 保持默认工具和旧工具的兼容声明；
- `ToolExecutionPolicy` 继续表达 scheduler 所需的 dependency、resource、concurrency group、realtime safety、artifact reuse 和 progress message；
- `ToolPolicyMetadata` 表达 local/扩展工具更丰富的 risk、approval、invocation execution、data 和 visibility 规则。

本阶段不删除字段，也不要求一次性迁移默认工具到 rich policy。

### ToolPolicyView 成为运行时唯一视图

`ToolPolicyView` 应覆盖运行时实际需要的全部解释结果：

```text
identity
  tool_name

risk / approval
  risk
  side_effect_level
  risk_gate_level
  requires_confirmation
  confirmation_kind
  confirmation_owner
  auto_executable
  compensation_hint

scheduling
  dependency_mode
  concurrency_group
  resource_reads
  resource_writes
  realtime_safety
  artifact_reuse
  progress_message

invocation execution
  timeout_s
  retry_count
  idempotency
  idempotency_required
  max_result_chars

realtime declaration
  realtime_mode
  interruptible
  commit_boundary

data handling
  reads_private_data
  writes_private_data
  sends_data_external
  redact_in_trace

visibility
  toolset
  tags
  requires_env
  enabled_by_default
  skill_only
```

`ToolPolicyMetadata.execution.concurrency` 暂不成为第二套 scheduler 并发命令。调度继续只使用 `ToolExecutionPolicy.concurrency_group`、dependency、resource 和 side-effect 事实；rich `concurrency` 字段保留兼容，但在本阶段不影响调度。

### 策略优先级

当 `ToolSpec.policy` 存在时：

- `policy.risk` 和 `policy.approval` 决定 side-effect/risk gate/confirmation；
- `policy.execution` 决定 timeout、retry、idempotency 和 result limit；
- `policy.realtime`、`policy.data`、`policy.visibility` 直接进入 view；
- 顶层 `ToolSpec.execution` 继续提供 scheduler 和 progress 所需事实；
- 顶层 `ToolSpec.side_effect` 保留在原始契约中，但不覆盖 rich risk/approval 的解释结果。

当 `ToolSpec.policy` 不存在时：

- `ToolSpec.side_effect` 决定 side-effect、risk gate 和 confirmation；
- `ToolSpec.execution` 决定 scheduling/realtime safety/artifact/progress；
- invocation retry 保持现有 global provider policy 行为；
- 未声明的 rich 字段使用保守或空值，不伪造能力。

未知工具继续使用 registry 现有的保守默认：confirmation-sensitive、requires-prior-observation、needs-confirmation。

## 执行数据流

每次 `ToolExecutor.run_tool()` 的目标流程为：

```text
tool_name
  -> ToolRegistry.get_spec(tool_name)
  -> ToolPolicyInterpreter.view_for_spec(spec)
  -> bind trusted runtime identity
  -> evaluate risk / confirmation / idempotency from ToolPolicyView
       ├─ duplicate committed key
       │    -> duplicate_suppressed ToolResult
       ├─ runtime confirmation required
       │    -> confirmation_required ToolResult
       ├─ policy violation
       │    -> structured rejected/failed ToolResult
       └─ executable
            -> provider budget check
            -> derive safe retry allowance
            -> ToolRegistry.run()
            -> ToolResult
            -> record idempotency / history / event / trace
            -> bounded ToolObservation
```

`ToolRegistry.get_spec(tool_name)` 是建议增加的单工具契约读取方法。它只从已注册工具生成 ToolSpec，不形成第二个 registry，也不改变 `list_specs()` 的 provider inventory 作用。

Scheduler 可以继续独立从同一个 `ToolPolicyInterpreter` 解析 policy 进行预调度；`ToolExecutor` 必须再次从 registry 解析，以防调用方传入过时或被放宽的策略。

为避免模块循环依赖，`risk_gate_level_for_policy` 和 confirmation-owner 的纯解释逻辑应归入 `tool_policy.py`。`tool_risk_gate.py` 只消费已经解析完成的 `ToolPolicyView` 并维护动态 confirmation/idempotency 决策，不再被 `tool_policy.py` 反向导入。

## Approval 与 risk gate 语义

### 基础规则

以下规则对所有进入 `ToolExecutor` 的入口一致：

- read-only 且不要求确认：自动执行；
- compensatable 且不要求确认：soft gate，必须具备幂等保护；
- `requires_confirmation=true`：进入 hard gate；
- unknown/unclassified：保守 hard gate；
- `approval.mode=always`：必须确认；
- `approval.mode=never`：除非其他更强的服务策略阻断，否则不要求 runtime confirmation；
- `approval.mode=conditional`：由 risk 映射决定。

当前 `_risk_gate_enabled()` 不再决定工具固有 confirmation 是否生效。source/channel/realtime metadata 只能增加限制，不能把 `approval=always` 变为直接执行。

### Confirmation owner

保留两类确认所有者：

- `runtime`：ToolExecutor 不调用真实工具，返回结构化 `confirmation_required` 结果；
- `tool`：允许工具进入其已有的受治理确认服务，例如 memory write policy 创建 pending confirmation，而不是直接提交敏感写入。

tool-owned confirmation 不能仅依赖散落的工具名集合长期扩展。当前 memory 兼容名单可保留，但新增工具应通过明确的 policy/service contract 声明；该声明的具体 schema 在出现第二类真实 tool-owned confirmation 服务前不新增。

### Confirmation evidence

确认事实继续来自 runtime 绑定的可信 request metadata，例如匹配工具名的 `tool_confirmation`。模型参数中的 `confirmed=true`、用户身份或 session identity 不作为确认依据。

普通聊天、HTTP、CLI chat 和 realtime turn 都使用相同确认结果。MCP `tool_run` 和 local-tool simulate 即使是显式工具入口，也不能把“调用了这个命令”自动等同于对不可逆副作用的确认；它们必须提供明确 confirmation evidence 或收到 pending confirmation 结果。

## 幂等语义

### Required idempotency

当 `ToolPolicyView.idempotency_required=true` 时：

- ToolExecutor 必须把该事实传给 risk gate；
- runtime-owned hard-gate 工具在确认后仍缺少 `idempotency_key` 时不得执行；
- 有 key 时先查询 `ToolIdempotencyLedger`；
- 已存在 committed record 时返回 `duplicate_suppressed`，不得再次调用 registry；
- 只有真实成功且不再等待确认的结果才能写入 committed record；
- failed、cancelled 和 pending-confirmation 结果不得伪装成 committed。

### Generated idempotency key

保留现有 compensatable soft-gate 工具的稳定 key 生成行为：当调用具有稳定 `step_id` 时，runtime 可以生成 key，避免 image/render/delegation 一类已经启动的工作被同一 run 重复提交。

对于需要用户确认的 external-write 工具，不自动生成模型不可见的业务幂等 key。确认后的请求必须显式携带可重放的 key，确保跨下一轮请求仍能识别同一动作。

### Ledger 边界

本设计继续使用现有 process-local ledger，不在本阶段增加数据库、跨进程一致性或分布式锁。持久化 ledger 应在首个需要跨进程 exactly-once 的真实外部写 provider 接入时单独设计。

## Retry、timeout 与结果大小

### Retry

重试需要同时通过两道判断：

1. provider error code 被全局 `RetryPolicy` 判定为 retryable；
2. 工具调用可以安全重放。

安全重放仅包括：

- read-only side-effect level；
- 或 `idempotency_required=true` 且本次存在 idempotency key。

对于带 rich policy 的工具：

```text
effective_max_retries = min(tool.retry_count, global.max_retries)
```

`tool.retry_count=0` 表示不重试。对于没有 rich policy 的现有默认工具，保持当前 global retry 行为，避免本阶段意外改变 read-only provider 工具的恢复能力。

mutating、non-idempotent 工具即使全局 error code 可重试也不得自动重放。

### Timeout

`timeout_s` 表达工具声明的调用 deadline，不授权 ToolExecutor 强制终止任意同步 Python 线程。

本阶段行为是：

- 把 `tool_execution.timeout_s` 和 process-local `tool_execution.deadline_monotonic_s` 写入 `ToolContext.metadata`；
- provider/service adapter 在 HTTP、SDK 或轮询边界消费 deadline；
- cooperative tool 继续检查 cancel token；
- deadline 后状态未知的 mutating 工具返回结构化 `unknown_after_timeout`/provider timeout 失败，不得声称动作未提交；
- 不增加通用线程 kill 或假取消机制。

ToolExecutor 在 post-tool trace 中记录 `timeout_s_declared` 和 `deadline_propagated=true`。只有 adapter/tool 在 `ToolResult.trace_summary.deadline_enforced=true` 时，trace 才能记录 `deadline_enforcement=adapter_reported`；未报告时记录 `deadline_enforcement=not_reported`，不能把静态声明误报为已经强制执行。

### Result size

`max_result_chars` 只约束进入下一轮 LLM 的 `ToolObservation`，不截断审计记录、稳定 `output_ref` 或外部 raw-data reference。

`observation_from_tool_result()` / context compaction 接受由 registry ToolSpec 解析出的 `max_result_chars`。ToolExecutor 不把内部 policy 写进 `ToolResult.data`，provider 结果也不能自行放宽该限制。

裁剪后 observation 应保留：

- status；
- summary；
- error code；
- output_ref；
- 关键结构化字段的有界表示；
- `truncated=true` 和原始字符规模。

工具原始大结果仍不直接写入 prompt。

## DataPolicy 与 trace

`DataPolicy` 不作为新的用户权限系统。它用于决定执行和可观测边界怎样处理工具数据：

- `redact_in_trace=true` 时，trace/history 只保存字段名、规模、状态、引用和经过批准的 summary，不保存原始私有值；
- `sends_data_external=true` 是 risk/审计事实，不自动授予外发权限；工具仍需通过 visibility、entry policy、approval 和具体 service policy；
- provider/MCP tool schema 不暴露 `reads_private_data`、`writes_private_data`、`sends_data_external` 或内部 redaction 配置；
- `ToolResult.raw_data_ref` 继续只作为引用，不进入 ToolObservation。

本设计不把 `DataPolicy` 扩展成 DLP、字段级分类或租户授权框架。

## Realtime 边界

`ToolPolicyView` 应补齐 `interruptible` 和 `commit_boundary`，让 executor、trace 和后续 realtime lifecycle 可以读取同一声明，但本设计不凭一个静态字符串推断动态动作是否已经提交。

现有 realtime 不变量保持不变：

```text
cancelled old turn
  -> stale_outputs=true
  -> can_reuse_tool_result=false
  -> speakable=false
  -> Gateway drops queued/late old-run output
```

同时保留现有原则：对已经成功产生 compensatable/committed 副作用的工具，取消不能把成功结果改写成“从未发生”。ToolExecutor 可继续记录幂等和审计事实，但“cancelled run 中的 committed outcome 如何进入下一轮 realtime task state”需要独立 lifecycle 设计。

本设计不增加 `latency_class`、`stale_result_speakable` 或第二个 progress boolean：

- 实际延迟继续由 `latency_ms` 和 provider metrics 记录；
- 是否需要等待提示继续由 `realtime_safety` 和 `progress_message` 表达；
- stale output 是否可口播继续是 run-level cancellation contract，而不是单工具可放宽的属性。

## 模块职责变化

预期实现涉及以下模块：

- `src/assistant_agent/services/tool_policy.py`
  - 扩展 `ToolPolicyView` 的 realtime 字段；
  - 明确 legacy/rich policy 优先级；
  - 提供 risk-level、confirmation-owner、retry eligibility/effective limit 所需的纯解释事实；
  - 不再反向依赖 `tool_risk_gate.py`。
- `src/assistant_agent/tools/registry.py`
  - 增加单工具 `get_spec()`；
  - 继续由同一 ToolSpec 生成路径服务 list/get，避免契约漂移。
- `src/assistant_agent/agent/tool_executor.py`
  - 在执行开始处解析完整 policy view；
  - 把完整 risk/idempotency 事实传给 risk gate；
  - 根据 policy view 派生安全 retry、deadline metadata 和 trace redaction；
  - 移除只返回 `ToolSideEffectPolicy` 的局部降级 helper。
- `src/assistant_agent/services/tool_risk_gate.py`
  - 直接消费完整 `ToolPolicyView`；
  - 不再向 `tool_policy.py` 提供静态 policy 解释函数；
  - confirmation 不再由 realtime/source 开关决定；
  - 保留 tool-owned confirmation 和 ledger 行为。
- `src/assistant_agent/schemas/tool_observation.py` 及 context compaction
  - 接受由 registry policy view 提供的 `max_result_chars`，记录裁剪事实；
  - 不信任 `ToolResult.data` 中自报的放宽值。
- `src/assistant_agent/agent/runtime.py` 和 `agent/assistant_loop_nodes.py`
  - 在 ToolResult 转换为 ToolObservation 时提供对应 registry policy view；
  - native 和 mock/offline 路径使用同一 observation limit 语义。
- `src/assistant_agent/services/tool_call_boundary.py`
  - 使用同一 policy view 生成 pre/post summary；
  - 按 DataPolicy 控制 trace-safe 摘要。
- `src/assistant_agent/agent/tool_scheduler.py`
  - 保持现有保守并发算法；
  - 只确认继续通过同一个 interpreter 读取 scheduler facts。
- `docs/tool-calling-architecture.md`
  - 在实现完成后更新为实际行为，不能在代码落地前把本规格写成当前事实。

不需要新增第三方依赖。

## 实施分段

本设计应拆成连续但可独立验证的实现段：

### 段 1：Policy parity 与风险执行闭环

- 完整 policy view 进入 ToolExecutor；
- 修复 confirmation/idempotency 执行；
- 普通文本与 realtime 的 approval 行为一致；
- calendar external-write 用例通过。

### 段 2：安全 retry 和 deadline 传播

- rich retry_count 成为 global retry 的上限；
- non-idempotent mutation 禁止自动 retry；
- timeout/deadline 进入 ToolContext 和支持它的 adapters；
- trace 区分 `adapter_reported` 与 `not_reported`，不把传播 deadline 等同于 adapter 已强制执行。

### 段 3：Observation limit 与 DataPolicy trace

- max_result_chars 在 LLM observation 边界生效；
- private/external data trace 使用受限摘要；
- raw references 和审计边界保持可用。

每一段都必须保持主治理链路和 mock/local/offline 默认路径。不要把三个段与 capability qualification 或完整 realtime commit lifecycle 合并成一次大范围实现。

## 错误与结果语义

所有策略阻断都必须返回结构化结果或 observation，不能把未处理异常抛给模型：

| 情况 | 期望结果 |
| --- | --- |
| 未确认的 runtime-owned write | success-shaped pending confirmation result，不执行 registry tool |
| 确认后缺少 required idempotency key | confirmation/idempotency required result，不执行 registry tool |
| 重复 committed idempotency key | `duplicate_suppressed` success result，不执行 registry tool |
| provider retryable error，但工具不可安全重放 | 首次失败结果，`retry_count=0` |
| deadline 前明确失败 | structured failed ToolResult |
| mutating 调用 timeout 后提交状态未知 | structured `unknown_after_timeout`，不得声称未提交 |
| observation 超限 | 有界 observation + `truncated=true`，保留 output/raw reference |
| policy/schema 本身无效 | 工具加载或 spec 生成阶段失败，不向模型暴露半有效工具 |

pending confirmation 结果继续允许下一轮 LLM 向用户请求确认；它不是工具真实副作用已经完成的证据。

## 测试设计

实现应遵循测试先行。最小覆盖包括：

1. rich `external_write + approval=always + idempotency=required` 在普通文本入口先请求确认，不执行 handler。
2. 同一工具在 realtime 入口得到相同 confirmation 决策。
3. 已确认但缺少 idempotency key 时继续阻断。
4. 已确认且带 key 时只执行一次；第二次返回 `duplicate_suppressed`。
5. pending-confirmation、failed 和 cancelled 结果不写 committed ledger。
6. compensatable 工具在稳定 step_id 下继续生成幂等 key。
7. rich read-only 工具保持自动执行，不增加确认。
8. local write tool 即使没有 realtime/source metadata，也不能绕过 approval。
9. model input 中伪造 confirmation/user/session 不影响 trusted runtime evidence。
10. rich read-only tool 的 retry 次数受 `min(tool.retry_count, global.max_retries)` 限制。
11. non-idempotent mutating tool 不自动 retry。
12. 没有 rich policy 的现有 read-only default/mock tool 保持 global retry 兼容行为。
13. timeout/deadline 以固定 metadata key 进入 ToolContext；支持 deadline 的 fake adapter 能观察并报告 `deadline_enforced=true`，未报告的 adapter 记录为 `not_reported`。
14. 超大 result 被裁剪成有界 observation，但 `output_ref`/`raw_data_ref` 边界不丢失。
15. `redact_in_trace=true` 时 trace/history 不包含私有输入和 provider raw payload。
16. provider/MCP schema 继续不暴露 internal policy metadata。
17. scheduler 的 read-only parallel、write serial、confirmation serial 行为保持不变。
18. Gateway cancel 后旧轮输出继续不可 speakable；本设计不把 policy field 变成绕过输出门控的入口。

优先更新和运行：

```text
tests/test_calendar_create_event_slice.py
tests/unit/test_tool_policy_metadata.py
tests/test_tool_policy_parity_integration.py
tests/test_tool_risk_gate.py
tests/test_retry_policy.py
tests/test_tool_executor.py
tests/test_native_tool_call_handoff.py
tests/test_realtime_turn_cancellation.py
```

所有测试使用 mock/local/scripted provider，不调用真实外部服务。

## 验收标准

- `ToolPolicyView` 是 scheduler、executor、risk gate 和 boundary summary 的唯一策略解释结果。
- rich policy 中 `approval`、`risk`、`idempotency` 和 `retry_count` 不再只用于展示或 trace。
- `AgentGraphRuntime` 的普通聊天、HTTP、CLI chat 和 realtime 路径对同一 ToolSpec 产生相同基础风险决策。
- `ActionValidator -> ToolExecutor -> ToolRegistry` 链路保持不变。
- 未确认或缺少 required idempotency key 的 mutating 工具不会进入 `ToolRegistry.run()`。
- 重复 committed key 不会重复执行 handler。
- non-idempotent mutation 不会被全局 retry policy 自动重放。
- `timeout_s` 不被实现成不可靠的同步线程强杀；trace 明确区分 deadline 已传播和 adapter 已报告强制执行。
- `max_result_chars` 只限制 LLM observation，不破坏审计引用。
- provider-facing tool schema 不暴露内部治理 metadata。
- 默认 mock/local/offline 测试不依赖外部 provider。
- capability qualification、Gateway 和 realtime commit/cancel lifecycle 的独立边界没有被合并或绕过。

## 不在本设计范围

- 重写或删除现有 `ToolSpec.side_effect` / `ToolSpec.execution`。
- 把所有默认工具一次性迁移到 rich `ToolPolicyMetadata`。
- Tool Profile、allow/deny、用户/租户授权存储或 semantic tool recall。
- 完整 realtime commit protocol、补偿事务或 cancelled-run side-effect persistence。
- 持久化或分布式 idempotency ledger。
- 通用线程 kill、异步执行器重写或资源锁/DAG scheduler。
- 通用 trusted/untrusted result taxonomy、DLP 或字段级数据分类。
- Gateway Agent loop、自定义 JSON controller 或绕过 ToolExecutor 的执行路径。
- 真实 provider 调用。

## 后续设计顺序

本规格实现并验证后，后续工作按以下顺序独立设计和实施：

1. 落地既有 tool qualification / identity recall 规格，并在真实入口需要时增加小规模 run-scoped Tool Profile / deny-wins 规则。
2. 为首个真实不可逆 external-write 工具设计 realtime commit/cancel outcome protocol，解决 cancelled run 中 committed side effect 的下一轮恢复。
3. 在 native loop 中增加基于 tool name、normalized args 和 observation digest 的 no-progress 检测。

这些后续能力都必须继续复用本设计形成的统一 policy execution 边界。
