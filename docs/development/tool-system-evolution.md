# 个人实时通话助理的工具系统演进设计

状态：设计文档
范围：`assistant_agent` 当前 Python 工具系统、治理链路、实时通话生命周期、Skill/MCP 接入演进
目标：低摩擦工具接入 + 强治理执行边界 + realtime-aware 的确认、打断、取消、提交、审计和恢复
约束：本设计的最终目标不是让工具系统更抽象，而是让个人实时通话助理在调用工具时做到：可判断、可确认、可取消、可追踪、可恢复、可低成本扩展。任何阶段如果只提升代码抽象性，但没有提升上述任一产品能力，就不应该做。

## 0. 结论摘要

当前项目的真实优势不是“插件市场”或“AI OS”，而是已经形成了一条较清晰的 governed tool execution 主链：

```text
ToolSpec
  -> AssistantDecision / provider-native tool call
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> ToolResult
  -> ToolObservation / Trace / Audit / RealtimeTaskState
```

这条链路应该保留，并作为 local Python tool、future inbound MCP tool、Skill-declared capability、future adapter/plugin 的统一执行入口。

当前主要问题不是缺少治理，而是治理语义分散在 `ToolSpec.side_effect`、`ActionValidator` tool_name 特判、`ToolExecutor` 风险门禁、`RealtimeTaskState` side effect 推断、`ToolObservation` 文案转换和 context catalog 中。后续应先引入 `ToolPolicyInterpreter` 作为不改变行为的 parity layer，再逐步把风险、确认、实时策略、数据策略、可见性、超时、幂等等声明性 metadata 收敛到统一解释层。

本文明确不建议做通用插件市场、不建议让 Skill 执行代码、不建议让 MCP 绕过 `ActionValidator` / `ToolExecutor`，也不建议一次性重写 `ToolSpec`、`ToolExecutor` 和 realtime lifecycle。

## 1. 当前架构事实

### 1.1 主要模块和文件路径

工具 schema 与结果模型：

- `src/assistant_agent/schemas/tools.py`
  - `ToolSpec`
  - `ToolResult`
  - `ToolSideEffectPolicy`
  - `ToolCallRecord`
  - `ToolSideEffectLevel`
- `src/assistant_agent/schemas/tool_observation.py`
  - `ToolObservation`
  - `observation_from_tool_result`
- `src/assistant_agent/schemas/tool_spec_adapters.py`
  - `tool_spec_to_json_schema`
  - `tool_spec_to_openai_tool`
  - `tool_spec_to_mcp_tool`

工具注册与工具实现：

- `src/assistant_agent/tools/base.py`
  - `ToolContext`
  - `BaseTool`
  - `MockTool`
- `src/assistant_agent/tools/registry.py`
  - `ToolRegistry`
  - `create_default_registry`
  - `_ACTION_USAGE`
  - `_hide_runtime_identity_field`
- `src/assistant_agent/tools/memory_tool.py`
- `src/assistant_agent/tools/memory_media_tool.py`
- `src/assistant_agent/tools/agent_tools.py`

验证、执行、风险门禁与边界：

- `src/assistant_agent/agent/action_validator.py`
  - `ActionValidator`
  - `ValidationResult`
- `src/assistant_agent/agent/tool_executor.py`
  - `ToolExecutor`
  - `ProviderBudgetExceeded`
- `src/assistant_agent/services/tool_risk_gate.py`
  - `ToolRiskDecision`
  - `ToolIdempotencyRecord`
  - `InMemoryToolIdempotencyLedger`
  - `evaluate_tool_risk`
  - `risk_gate_level_for_policy`
  - `confirmation_required_result`
- `src/assistant_agent/services/tool_call_boundary.py`
  - `build_pre_tool_call_summary`
  - `build_post_tool_call_summary`
- `src/assistant_agent/services/tool_history.py`
  - `ToolCallHistoryStore`
  - `ToolCallHistoryRecord`

Agent runtime 与 tool call 入口：

- `src/assistant_agent/agent/runtime.py`
  - `AgentGraphRuntime`
  - provider-native tool call handoff
- `src/assistant_agent/agent/assistant_loop_nodes.py`
  - mock/offline assistant loop 中的 `execute_requested_tool_node`
- `src/assistant_agent/agent/state.py`
  - `AgentState`
  - `add_tool_call`
  - `complete_tool_call`
  - `fail_tool_call`

Realtime gateway / session / cancel / interrupt：

- `src/assistant_agent/gateway/session.py`
  - `GatewaySession`
  - `CancelToken`
  - interrupt/cancel/hangup handling
- `src/assistant_agent/realtime/agent_graph_backend.py`
  - `AgentGraphRealtimeBackend`
- `src/assistant_agent/services/assistant_run_service.py`
  - `run_assistant_request`
  - `_RealtimeTaskStateTrackingEventSink`
- `src/assistant_agent/services/realtime_task_state.py`
  - `RealtimeTaskState`
  - `PendingToolState`
  - `SideEffectRecord`
  - `RealtimeTaskStateReducer`
- `src/assistant_agent/gateway/event_mapping.py`
  - realtime event 到 gateway event 的映射
- `src/assistant_agent/schemas/events.py`
  - `AgentEvent`

Trace / audit / observability：

- `src/assistant_agent/services/trace.py`
- `src/assistant_agent/services/tool_history.py`
- `src/assistant_agent/services/tool_call_boundary.py`
- `docs/observability-harness.md`
- memory 相关审计位于 `src/assistant_agent/services/memory_audit.py`、`src/assistant_agent/schemas/memory_audit.py`

Skill / Capability / MCP 雏形：

- `src/assistant_agent/services/context/skill_loader.py`
  - `SkillDescriptor`
  - `SkillLoader`
- `src/assistant_agent/services/context/capability_catalog.py`
  - `CapabilityCatalog`
  - built-in capability descriptor
- `src/assistant_agent/services/context/tool_catalog.py`
  - `select_prompt_tool_specs`
  - `prompt_tool_spec_payload`
- `src/assistant_agent/mcp/server.py`
  - `OfflineMCPServer`
  - `agent_run`
  - `tool_list`
  - `tool_run`
  - `demo_flow_run`

相关测试：

- `tests/unit/test_tool_registry.py`
- `tests/unit/test_tool_spec_adapters.py`
- `tests/test_tool_executor.py`
- `tests/test_tool_call_boundaries.py`
- `tests/test_tool_risk_gate.py`
- `tests/test_phase0_tool_governance_contracts.py`
- `tests/test_realtime_task_state.py`
- `tests/test_native_tool_call_handoff.py`
- `tests/test_mcp_server_skeleton.py`
- `tests/test_phase3_skill_system_gate.py`
- `tests/test_architecture_boundaries.py`

### 1.2 一个 tool 从注册到执行的完整调用链

普通本地工具注册链路：

```text
create_default_registry()
  -> ToolRegistry.register(tool)
  -> ToolRegistry.list_specs()
  -> ToolSpec(name, description, input_schema, required_inputs, when_to_use, when_not_to_use, runtime_constraints, side_effect)
  -> tool_spec_to_openai_tool / tool_spec_to_json_schema / tool_spec_to_mcp_tool
```

provider-native runtime 执行链路：

```text
AgentGraphRuntime._run_native_runtime()
  -> ChatRequest(tools=tool specs)
  -> provider returns tool_calls
  -> AssistantDecision(action=tool_name, action_input=arguments)
  -> ActionValidator.validate()
  -> ToolExecutor.run_tool()
  -> ToolRegistry.run()
  -> BaseTool.run(context=ToolContext, **kwargs)
  -> ToolResult
  -> observation_from_tool_result()
  -> tool role message / trace / history / realtime event
```

mock/offline assistant loop 链路：

```text
assistant_loop_nodes.execute_requested_tool_node()
  -> ActionValidator.validate()
  -> ToolExecutor.run_tool()
  -> observation_from_tool_result()
```

MCP server skeleton 当前链路：

```text
OfflineMCPServer.tool_run()
  -> AssistantDecision(action=tool_name, action_input=arguments)
  -> ActionValidator.validate()
  -> ToolExecutor.run_tool()
```

重要事实：当前 `src/assistant_agent/mcp/server.py` 是把本项目内部工具暴露为 offline MCP-like server，其中 `tool_run` 仍经过 validator/executor。它不是 inbound MCP 外部工具适配器。

### 1.3 ToolSpec 当前职责

`ToolSpec` 位于 `src/assistant_agent/schemas/tools.py`，当前字段主要是：

- `name`
- `description`
- `input_schema`
- `required_inputs`
- `when_to_use`
- `when_not_to_use`
- `runtime_constraints`
- `side_effect`

其中 `side_effect` 是 `ToolSideEffectPolicy`，当前字段包括：

- `level`
- `requires_confirmation`
- `description`
- `confirmation_kind`
- `compensation_hint`

当前 `ToolSpec` 承担三类职责：

1. 暴露给模型或外部协议的 tool schema。
2. 给 prompt/context 渲染提供 `when_to_use` / `when_not_to_use` / `runtime_constraints`。
3. 通过 `side_effect` 提供最小治理 metadata。

当前缺口：`ToolSpec` 还没有显式表达 risk、realtime policy、approval policy、execution policy、data policy、visibility、toolset/tags、requires_env、enabled_by_default、timeout、retry、concurrency、max_result_chars 等结构化策略。

### 1.4 ToolResult 当前职责

`ToolResult` 位于 `src/assistant_agent/schemas/tools.py`，当前字段主要是：

- `tool_name`
- `success`
- `data`
- `error`
- `output_ref`
- `latency_ms`
- `contract`

当前 `ToolResult` 是工具执行后的统一结构化返回，供以下模块消费：

- `ToolExecutor` 写入 state / trace / history。
- `observation_from_tool_result` 转成模型 observation。
- `RealtimeTaskStateReducer` 从 result 中推断 side effect 和 continuation strategy。
- `tool_call_boundary` 生成 pre/post boundary summary。

当前缺口：`ToolResult` 还没有显式区分 voice 口播摘要、model observation、trace summary、audit payload、raw data 引用。现有 `data` 既承担模型输入、审计信息、realtime 恢复信息和业务 payload，后续会使 redaction、播报压缩和审计保真互相牵制。

### 1.5 ActionValidator 当前职责

`ActionValidator` 位于 `src/assistant_agent/agent/action_validator.py`。当前职责包括：

- 校验 assistant decision 类型。
- 校验 `tool_name` 是否存在于 `ToolRegistry`。
- 校验 `tool_input` 是 object。
- 生成 `pre_tool_call` boundary summary。
- 执行 tool-specific 语义校验。
- 执行 memory read intent gate。
- 执行 render / memory media 等意图 gate。
- 根据 tool 的 Pydantic args schema 执行结构校验。

当前存在大量 `tool_name` 特判，例如：

- `vision_understanding`
- `video_understanding`
- `image_generation`
- `product_search`
- `price_compare`
- `web_search`
- `memory_retrieval`
- `memory`
- `memory_save`
- `memory_ingest_status`
- `memory_media_ingest`
- `render_3d`
- `delegate_to_agent`

这些特判使新增工具时更容易触碰核心 validator 文件。

### 1.6 ToolExecutor 当前职责

`ToolExecutor` 位于 `src/assistant_agent/agent/tool_executor.py`。当前职责包括：

- 处理 pre-cancel。
- 注入 runtime identity，例如 memory tools 的 `user_id` / `session_id`。
- 读取 `ToolSpec.side_effect`。
- 调用 `evaluate_tool_risk`。
- 生成 pre tool call boundary。
- 记录 `AgentState.add_tool_call`。
- 发出 `tool_started` / `tool_finished` / `tool_failed` 等事件。
- 写 `ToolCallHistoryStore`。
- 做 provider budget 检查。
- 做 idempotency duplicate suppression。
- 对 hard gate 返回 `confirmation_required_result`。
- 调用 `ToolRegistry.run` 执行工具。
- 处理 retry / recovery。
- 处理取消与 after-tool cancel。
- 写 trace。
- 将 `ToolResult` 写回 state。

当前 `ToolExecutor` 是强治理核心，但职责偏重。它混合了调度、权限/风险、执行、结果转换、trace/history、cancel 语义和部分 runtime identity 注入。

### 1.7 ToolObservation 当前职责

`ToolObservation` 位于 `src/assistant_agent/schemas/tool_observation.py`，当前字段包括：

- `tool_name`
- `status`
- `summary`
- `output_ref`
- `structured_output`
- `error_code`
- `error_message`
- `next_step_hint`
- `redacted`

`observation_from_tool_result` 当前会针对 `web_search`、`product_search`、`price_compare`、`vision_understanding`、`video_understanding`、`image_generation`、`render_3d` 等工具生成较具体的摘要和 next-step hint。

当前问题是 observation 更偏模型可读摘要，不等同于 voice summary、audit payload 或 trace summary。后续需要 presentation split。

### 1.8 realtime task state 如何记录 tool call

`RealtimeTaskState` 位于 `src/assistant_agent/services/realtime_task_state.py`。它当前跟踪：

- `pending_tool`
- `side_effects`
- `artifacts`
- `continuation_strategy`
- TTS / interruption / barge-in 相关字段

事件映射链路：

```text
ToolExecutor emits AgentEvent(type="tool_started" / "tool_finished" / "tool_failed")
  -> assistant_run_service._RealtimeTaskStateTrackingEventSink
  -> _task_state_reducer_event_type()
  -> RealtimeTaskStateReducer.apply()
```

`RealtimeTaskStateReducer` 当前行为：

- `tool.started` 设置 `pending_tool`。
- `tool.finished` / `tool.failed` 清空 `pending_tool`。
- `run.cancel` / `call.hangup` 清空 `pending_tool`，设置 interrupted/barge-in。
- 从 `ToolResult` 和 side-effect policy 推导 `SideEffectRecord`。
- 根据 side effect 和 artifact 推导 continuation strategy，例如 `ask_confirmation`、`report_committed`、`compensate`、`resume_from_checkpoint`、`reuse_and_replan`、`restart`。

当前已有实时恢复雏形，但还不是完整 tool lifecycle 状态机。`ToolCallRecord.status` 仍只有 `pending` / `running` / `succeeded` / `failed`，`AgentEvent` 也没有 `committed`、`cancelled_before_commit`、`interrupted_after_commit`、`deferred` 等细分状态。

### 1.9 当前新增一个普通只读 tool 需要改哪些地方

以普通本地只读工具为例，当前通常需要：

1. 在 `src/assistant_agent/tools/` 新增工具类，通常实现 `BaseTool` 协议，提供：
   - `name`
   - `description`
   - `args_schema`
   - `tool_side_effect_policy`
   - `run`
2. 在 `src/assistant_agent/tools/registry.py` 的 `create_default_registry()` 显式注册。
3. 在 `src/assistant_agent/tools/registry.py` 的 `_ACTION_USAGE` 增加 `when_to_use`、`when_not_to_use`、`runtime_constraints`、`side_effect`。
4. 如果 prompt tool selection 需要按场景暴露，修改 `src/assistant_agent/services/context/tool_catalog.py`。
5. 如果 validator 有语义 gate，需要修改 `src/assistant_agent/agent/action_validator.py`。
6. 如果 observation 需要定制摘要，需要修改 `src/assistant_agent/schemas/tool_observation.py`。
7. 如果 realtime artifact / reuse / validation 类型需要处理，可能修改 `src/assistant_agent/services/realtime_task_state.py`。
8. 增加测试，例如 registry/spec、validator、executor、native handoff、realtime state。

只读工具理论上只需要工具文件 + registry 注册 + tests，但当前如果要获得较好 prompt 可见性和 observation 体验，往往会触碰多个核心路径。

### 1.10 当前新增一个有副作用 tool 需要改哪些地方

以外部写入或本地写入工具为例，除只读工具步骤外，还通常需要：

1. 定义 `tool_side_effect_policy`，设置 `requires_confirmation`、`confirmation_kind`、`compensation_hint`。
2. 检查 `src/assistant_agent/services/tool_risk_gate.py` 是否能正确推导 hard gate / soft gate / idempotency。
3. 如果属于 tool-owned confirmation，例如 memory 现有逻辑，需要关注 `_TOOL_OWNED_CONFIRMATION_TOOLS`。
4. 如果需要 idempotency，需要确认 input 中是否有稳定 `idempotency_key`，或接受系统生成的 key。
5. 如果需要 realtime interrupt 后的 commit 语义，需要扩展 `RealtimeTaskStateReducer` 的 side effect 推断与 continuation strategy。
6. 如果需要恢复、补偿或审计，需要扩展 tool result `data` / `contract` 约定，以及 trace/history 测试。

当前有副作用 tool 的治理能力已经存在，但需要修改的位置多，且 policy 语义没有集中解释层。

### 1.11 Skill / MCP / Capability 是否已有雏形

已有雏形，但边界需要保留：

- Skill v1 已存在于 `src/assistant_agent/services/context/skill_loader.py`。
  - `SkillDescriptor` 支持 `governed_tools`、`permissions`、`required_inputs_by_tool`、`runtime_constraints` 等。
  - `tests/test_phase3_skill_system_gate.py` 验证 Skill 只声明 governed tools / permissions，不提供 `run_skill` 或直接执行路径。
- Capability catalog 已存在于 `src/assistant_agent/services/context/capability_catalog.py`。
  - 当前是 context rendering 层的能力描述，不是执行器。
- Tool catalog 已存在于 `src/assistant_agent/services/context/tool_catalog.py`。
  - 当前负责 prompt-visible tool selection 和 compact payload。
- MCP server skeleton 已存在于 `src/assistant_agent/mcp/server.py`。
  - 当前是将内部工具通过 MCP-like 接口暴露出去，不是接入外部 MCP server 的 inbound adapter。

### 1.12 当前 tool_name 硬编码或重复 policy 判断

已发现的硬编码和重复判断集中在：

- `src/assistant_agent/agent/action_validator.py`
  - 多个 tool_name 语义校验和 intent gate。
- `src/assistant_agent/agent/tool_executor.py`
  - `_capability_name`
  - `_bind_runtime_identity`
  - `_preserve_success_after_cancel`
- `src/assistant_agent/tools/registry.py`
  - `_ACTION_USAGE`
  - `_hide_runtime_identity_field`
  - `create_default_registry`
- `src/assistant_agent/services/tool_risk_gate.py`
  - `_TOOL_OWNED_CONFIRMATION_TOOLS`
  - side effect level 到 risk gate 的解释。
- `src/assistant_agent/services/realtime_task_state.py`
  - reusable/validation/do_not_reuse tool sets。
  - side effect level 与 continuation strategy 推断。
- `src/assistant_agent/schemas/tool_observation.py`
  - 多个工具的 observation summary / next_step_hint 特判。
- `src/assistant_agent/services/context/tool_catalog.py`
  - prompt tool selection keyword 与工具分组。

这些重复判断目前还能工作，但会放大新增工具和新增外部 adapter 的工程摩擦。

## 2. 当前问题诊断

### 2.1 新增工具是否需要修改过多核心路径

存在。

只读工具如果只是 registry 可见，修改工具类和 `create_default_registry()` 即可。但如果要具备完整用户体验，通常还要修改 `_ACTION_USAGE`、`tool_catalog`、`ActionValidator`、`ToolObservation`、tests。副作用工具还要考虑 `tool_risk_gate`、realtime state 和审计。

这说明当前系统的治理边界强，但接入体验偏“核心文件驱动”，还没有形成声明式 metadata + loader 的低摩擦路径。

### 2.2 Validator 是否混入过多 tool_name 特判

存在。

`ActionValidator` 同时做通用结构校验、registry existence 校验、Pydantic schema 校验，也做大量工具语义校验和 intent gate。典型例子包括 memory read gate、render intent gate、media ingest intent gate、search/query 文本约束等。

这些逻辑不应一次性移走，因为它们是当前治理能力的一部分。但后续应通过 `ToolPolicyInterpreter` 和 per-tool declarative validators 逐步把通用规则抽出来，减少 `ActionValidator` 对具体 tool_name 的直接依赖。

### 2.3 Executor 是否职责过重

存在。

`ToolExecutor.run_tool()` 是当前最重要的治理边界，但它承担了过多职责：

- cancel gate
- runtime identity binding
- risk gate
- confirmation short-circuit
- idempotency ledger
- provider budget
- retry/recovery
- registry.run 调用
- state mutation
- event emission
- trace/history 写入
- result post-processing

短期不建议拆掉 `ToolExecutor`，否则容易破坏治理边界。推荐先引入旁路解释层和小对象，例如 `ToolPolicyInterpreter`、`ToolExecutionPlan`、`ToolLifecycleRecorder`，让 executor 从“所有判断都在一个方法里”演进为“仍是唯一执行边界，但委托策略解释和生命周期记录”。

### 2.4 ToolSpec 是否缺少声明式治理 metadata

存在。

当前 `ToolSpec.side_effect` 只能表达最基础的副作用等级和确认需求，不能完整表达：

- risk：`pure` / `local_read` / `external_read` / `local_write` / `external_write` / `transactional` / `destructive`
- realtime policy：`inline` / `blocking` / `deferred` / `confirm_then_execute`
- approval policy：`never` / `conditional` / `always`
- execution policy：`timeout` / `retry` / `idempotency` / `concurrency` / `max_result_chars`
- data policy：`reads_private_data` / `writes_private_data` / `sends_data_external` / `redact_in_trace`
- visibility：toolset、tags、requires_env、enabled_by_default、skill visibility

`ToolSideEffectPolicy` 应兼容保留，但需要有更高层的 `ToolPolicy` 或 `ToolPolicyView` 统一解释。

### 2.5 ToolResult 是否不够适合 realtime 语音、模型 observation、trace/audit 分流

存在。

当前 `ToolResult.data` 同时承担：

- 工具业务数据。
- 模型 observation 的来源。
- trace/history 的摘要来源。
- realtime side effect 推断来源。
- 审计和恢复的部分来源。

这会造成冲突：

- voice 需要短、明确、可播报。
- model observation 需要足够结构化，帮助下一轮 reasoning。
- audit 需要完整、可追溯、可脱敏。
- trace 需要 prompt-safe 和可调试。
- raw data 可能很大，不适合塞进 observation 或语音。

后续应引入 presentation split，但应放在后期，等 policy interpreter 和 lifecycle 稳定后再做。

### 2.6 realtime tool call 是否缺少清晰状态机

存在。

当前 realtime state 已经能记录 `pending_tool`、side effects、artifacts 和 continuation strategy，但 tool lifecycle 仍主要由事件 `tool.started` / `tool.finished` / `tool.failed` 推断。缺少显式状态：

- `pending_confirmation`
- `cancelled_before_commit`
- `committed`
- `interrupted_after_commit`
- `deferred`

当前 `ToolCallRecord.status` 和 `ToolCallHistoryRecord.status` 也较粗，只能表达 running/succeeded/failed 或 started/succeeded/failed。

### 2.7 interrupt/cancel 是否有明确 commit boundary

部分存在，但不够显式。

当前 `ToolRiskGate` 能通过 side effect policy、idempotency 和 confirmation gate 降低风险。`RealtimeTaskStateReducer` 能根据 result 和 side effect 推导 `report_committed`、`ask_confirmation`、`compensate` 等策略。

但 commit boundary 不是显式事件或状态，而是由：

- `ToolSideEffectPolicy.level`
- `ToolResult.data.side_effect_level`
- `ToolRiskDecision`
- cancel 发生在工具前还是工具后
- `_preserve_success_after_cancel`
- realtime reducer 的推断

共同间接表达。后续应让工具执行生命周期显式产生 commit boundary，例如 `commit_started` / `committed` 或至少在 `ToolExecutionRecord` 中标明 `commit_status`。

### 2.8 当前是否容易支持 inbound MCP tool

不够容易，但已有基础。

已有基础：

- `ToolSpec` 可转 MCP-style tool schema。
- `OfflineMCPServer.tool_run` 已证明 MCP-like 调用也能走 validator/executor。
- `ToolRegistry` 接受实现 `BaseTool` 协议的工具对象。

缺口：

- 没有 inbound `MCPToolAdapter`。
- 没有把外部 MCP tool definition normalize 成内部 `ToolSpec` 的机制。
- 没有 MCP allowlist / namespace / disabled-by-default 策略。
- 没有外部 MCP 数据策略和审计策略。
- 没有隔离 MCP tool 直接写 memory 或发 gateway frame 的明确 adapter contract。

因此 inbound MCP 应作为“工具来源”，不是执行边界。外部 MCP tool 必须被包装为内部 `BaseTool` / `ToolSpec`，仍走 `ActionValidator` 和 `ToolExecutor`。

### 2.9 当前 Skill 是否保持在 capability declaration 边界内

是。

`SkillLoader` 当前加载 repo-local skill metadata 和 governed tool declarations。`tests/test_phase3_skill_system_gate.py` 验证 disabled/manual skill、缺失 governed tools、非法 permission 等场景，并且没有 `run_skill` 直接执行路径。

后续应保持这个边界：Skill 只声明 prompt、tools、permissions、tests、visibility，不执行代码、不写 memory、不调用外部 API、不拥有独立 agent loop。

## 3. 最终目标架构

### 3.1 长期目标

所有工具来源最终都统一进入同一条治理链：

```text
local Python tool
future inbound MCP tool
skill-declared capability
future plugin / adapter
        |
        v
ToolSpec / ToolPolicy
        |
        v
Capability / Visibility
        |
        v
ActionValidator
        |
        v
ToolExecutor
        |
        v
ToolRegistry
        |
        v
ToolResult / Observation
        |
        v
Trace / Audit / RealtimeTaskState
```

### 3.2 硬约束

- 不允许任何 tool 绕过 `ActionValidator` / `ToolExecutor`。
- 不允许 Skill 变成第二套 runtime。
- 不允许 MCP tool 直接执行，或直接写 memory / 发 gateway frame。
- 不允许引入不可控的 import-time global registry。
- 不允许把确认逻辑只放在 prompt。
- 不允许为了低摩擦牺牲 trace / audit / realtime lifecycle。
- 不因为检测到 API key 就自动启用外部工具。
- 不把 OpenClaw、Hermes、LangChain 或其他系统整体搬进本项目。

### 3.3 推荐目标分层

```text
Tool Source Layer
  - local explicit loader
  - MCP inbound adapter
  - skill manifest declared tools
  - future adapter/plugin source

Tool Definition Layer
  - ToolSpec
  - ToolPolicy metadata
  - ToolPolicyView

Visibility Layer
  - tool catalog
  - capability catalog
  - skill exposure
  - user/session/profile gates

Validation Layer
  - ActionValidator
  - schema validation
  - policy-driven semantic validation
  - confirmation requirement decision

Execution Layer
  - ToolExecutor
  - ToolRegistry
  - ToolContext
  - retry / timeout / idempotency

Realtime Lifecycle Layer
  - ToolScheduler or executor-owned scheduling helper
  - started / pending_confirmation / committed / cancelled / deferred states
  - commit boundary

Result Layer
  - ToolResult
  - ToolObservation
  - voice / model / trace / audit presentation split

Observability Layer
  - AgentEvent
  - ToolCallHistory
  - Trace
  - Audit
  - RealtimeTaskState
```

### 3.4 ToolScheduler 的定位

短期不建议单独做重型 scheduler。当前 `ToolExecutor` 已经是执行边界，过早引入独立 scheduler 容易变成第二套执行控制面。

推荐方式：

1. Phase 4 前先做 `ToolPolicyInterpreter`。
2. 在 `ToolExecutor` 内部引入轻量 `ToolExecutionPlan`：
   - realtime mode：inline / blocking / deferred / confirm_then_execute
   - timeout
   - cancelability
   - commit boundary
   - confirmation requirement
3. 如果后续出现多个并发 tool、排队、长耗时后台任务，再把 plan 生成和队列执行抽成 `ToolScheduler`。

也就是说，初期 scheduler 是 executor 的内部 helper，不是新的 runtime。

## 4. 目标能力

### 4.1 ToolPolicyInterpreter / ToolPolicyView

新增集中解释层，例如：

```text
src/assistant_agent/services/tool_policy.py
```

建议模型：

- `ToolPolicyView`
  - `tool_name`
  - `side_effect`
  - `risk`
  - `requires_confirmation`
  - `approval_mode`
  - `realtime_mode`
  - `timeout_s`
  - `retry`
  - `idempotency`
  - `data_policy`
  - `visibility`
  - `legacy_source`
- `ToolPolicyInterpreter`
  - `from_spec(spec, runtime_context) -> ToolPolicyView`
  - `risk_gate_level(policy) -> auto | soft_gate | hard_gate`
  - `requires_confirmation(policy, context) -> bool`
  - `commit_boundary(policy) -> before_execution | during_execution | after_success | external`
  - `trace_redaction(policy) -> redaction rules`

Phase 1A 的关键要求是 parity：先复用现有 `ToolSideEffectPolicy` 和 `tool_risk_gate` 语义，不改变任何行为。这样后续增强 `ToolSpec` metadata、local loader、MCP adapter、Skill manifest 和 realtime lifecycle 时，都可以只面向 `ToolPolicyView` 编程，而不是在各模块重复解释 side effect。

### 4.2 ToolSpec metadata 增强

建议兼容扩展，不破坏现有字段：

```text
ToolSpec
  - side_effect: ToolSideEffectPolicy
  - policy: ToolPolicyMetadata | None
```

建议 metadata：

- `risk`
  - `pure`
  - `local_read`
  - `external_read`
  - `local_write`
  - `external_write`
  - `transactional`
  - `destructive`
- `realtime`
  - `mode`: `inline` / `blocking` / `deferred` / `confirm_then_execute`
  - `interruptible`: bool
  - `tts_blocking`: bool
  - `expected_latency_ms`
  - `commit_boundary`
- `approval`
  - `mode`: `never` / `conditional` / `always`
  - `confirmation_kind`
  - `confirmation_prompt_ref`
- `execution`
  - `timeout_s`
  - `retry_count`
  - `idempotency`: `none` / `optional` / `required` / `generated`
  - `concurrency_key`
  - `max_result_chars`
- `data`
  - `reads_private_data`
  - `writes_private_data`
  - `sends_data_external`
  - `redact_in_trace`
  - `retention`
- `visibility`
  - `toolset`
  - `tags`
  - `requires_env`
  - `enabled_by_default`
  - `skill_only`

现有 `ToolSideEffectPolicy` 不应立刻删除。它是 Phase 1 / Phase 2 兼容旧工具和旧测试的桥。

### 4.3 local tool decorator / explicit loader

目标是吸收 Hermes 的低摩擦体验，但不采用不可控的 import-time global registry。

推荐 API 形态：

```python
@tool(
    name="weather.lookup",
    description="Look up current weather for a location.",
    policy=ToolPolicyMetadata(
        risk="external_read",
        realtime={"mode": "inline", "expected_latency_ms": 800},
        approval={"mode": "never"},
        data={"sends_data_external": True, "redact_in_trace": True},
    ),
    toolset="personal.readonly",
)
def lookup_weather(location: str, units: str = "metric") -> WeatherResult:
    ...
```

约束：

- decorator 只产生 local definition，不自动注册到全局 registry。
- 启动时由 explicit loader 加载指定模块或 manifest。
- loader 产物必须 normalize 成 `BaseTool` + `ToolSpec`。
- 注册仍通过 `ToolRegistry.register`。
- 执行仍通过 `ActionValidator` / `ToolExecutor`。
- startup validation 必须能发现 schema、policy、env、name collision、visibility 冲突。

建议新增：

- `src/assistant_agent/tools/decorators.py`
- `src/assistant_agent/tools/loader.py`
- `src/assistant_agent/tools/local_manifest.py`
- `src/assistant_agent/tools/cli.py`

### 4.4 tools validate / simulate / audit CLI

建议提供：

```bash
python -m assistant_agent.tools.cli validate
python -m assistant_agent.tools.cli simulate weather.lookup --input '{"location":"Shanghai"}'
python -m assistant_agent.tools.cli audit
```

能力：

- validate
  - schema 是否可转 JSON Schema / provider-native schema。
  - required_inputs 是否一致。
  - policy 是否完整。
  - side_effect 与 risk 是否兼容。
  - requires_env 是否满足或正确 disabled。
  - name namespace 是否冲突。
- simulate
  - 构造 `AssistantDecision`。
  - 跑 `ActionValidator`。
  - 可选择 dry-run `ToolExecutor`。
  - 输出 ToolResult / Observation / Trace summary。
- audit
  - 列出所有 enabled tools。
  - 列出外部读写、私有数据读写、需要确认、缺少 timeout、缺少 redaction 的工具。

### 4.5 inbound MCPToolAdapter

MCP 只作为工具来源，不作为执行边界。

目标：

```text
MCP server tool definition
  -> MCPToolAdapter.normalize()
  -> internal ToolSpec + MCPProxyTool
  -> ToolRegistry.register()
  -> ActionValidator
  -> ToolExecutor
  -> MCP client call
  -> ToolResult
```

规则：

- 外部 MCP tool 默认 disabled。
- 必须通过 allowlist 启用。
- tool name 必须 namespace 化，例如 `mcp.<server_id>.<tool_name>`。
- adapter 必须声明默认 policy：
  - unknown external read/write 默认 `requires_confirmation=True` 或 disabled。
  - private data / external send 默认 trace redaction。
  - 未声明 timeout 的 MCP tool 使用保守 timeout。
- MCP tool 不允许直接写 memory。
- MCP tool 不允许直接发 gateway frame。
- MCP tool 不允许绕过 `ToolExecutor` 的 trace/audit/realtime lifecycle。

### 4.6 Skill manifest v1 演进

当前 Skill 已保持在 capability declaration 边界内，后续只增强 manifest，不引入执行 runtime。

建议字段：

- `name`
- `description`
- `prompt`
- `tools`
- `permissions`
- `tests`
- `visibility`
- `enabled`
- `required_inputs_by_tool`
- `runtime_constraints`

明确禁止：

- `run_skill`
- Skill 内直接执行 Python。
- Skill 直接写 memory。
- Skill 直接调用外部 API。
- Skill 拥有独立 agent loop。

Skill 的作用是影响 capability / visibility / context，不是执行工具。

### 4.7 realtime tool lifecycle

建议引入显式 lifecycle：

- `started`
- `pending_confirmation`
- `cancelled_before_commit`
- `committed`
- `succeeded`
- `failed`
- `interrupted_after_commit`
- `deferred`

推荐新增或扩展：

- `ToolExecutionState`
- `ToolCommitState`
- `ToolLifecycleEvent`
- `ToolExecutionRecord`

最低可行实现：

- 保留现有 `tool_started` / `tool_finished` / `tool_failed` 对外事件。
- 内部先在 `ToolResult.contract` 或新 execution record 中记录 commit 状态。
- `RealtimeTaskStateReducer` 优先读取显式 lifecycle/commit 字段，不再只靠 side effect 推断。
- Gateway frame 可以后续再扩展，不在第一步破坏协议。

### 4.8 ToolResult presentation split

建议后期将 `ToolResult` 扩展为：

- `voice_summary`
  - 给 TTS 的短摘要。
- `model_observation`
  - 给 LLM 下一轮 reasoning 的结构化内容。
- `trace_summary`
  - prompt-safe / redacted 调试摘要。
- `audit_payload`
  - 可审计、可脱敏、可保留的完整治理信息。
- `raw_data_ref`
  - 大对象或敏感对象引用。

兼容策略：

- 初期仍保留 `data`。
- `observation_from_tool_result` 若发现 `model_observation` 则优先使用，否则回退 `data`。
- realtime voice 若发现 `voice_summary` 则优先使用，否则回退现有 summary。
- audit 若发现 `audit_payload` 则使用，否则保存现有 trace summary。

## 5. 分阶段执行计划

### Phase 0：事实确认和基线测试

阶段目标：

- 冻结当前事实，确认现有行为。
- 建立后续重构的回归基线。

为什么必要：

- 当前治理分散在多个模块。没有基线测试时，任何抽象都容易改变风险门禁、确认、cancel 或 observation 行为。

要修改的文件：

- 不修改运行时代码。
- 可新增或更新设计文档，例如本文件。

要新增的文件：

- 无必需新增文件。

不应该修改的部分：

- `ActionValidator`
- `ToolExecutor`
- `ToolRegistry`
- `RealtimeTaskStateReducer`
- provider / gateway runtime

行为是否变化：

- 不变化。

测试方案：

- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/unit/test_tool_registry.py`
- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/unit/test_tool_spec_adapters.py`
- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_executor.py`
- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_call_boundaries.py`
- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_risk_gate.py`
- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_task_state.py`
- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_mcp_server_skeleton.py`
- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase3_skill_system_gate.py`

回滚方案：

- 文档-only 变更直接 revert 文档。

验收标准：

- 当前架构事实被文档化。
- 基线测试可运行，已知失败若存在需记录原因。

如何支撑后续阶段：

- Phase 1A 的 parity layer 必须以 Phase 0 的测试和事实为准。

### Phase 1A：ToolPolicyInterpreter parity layer，不改变行为

阶段目标：

- 新增 `ToolPolicyInterpreter` / `ToolPolicyView`。
- 只解释现有 `ToolSideEffectPolicy`、runtime context 和 risk gate 规则。
- 不替换现有行为，不改变 tool specs。

为什么必要：

- 它是后续 ToolSpec metadata、local loader、MCP adapter、Skill manifest 和 realtime lifecycle 的共同地基。
- 后续所有新来源的工具都可以先 normalize 成 `ToolPolicyView`，再进入现有治理链。

要修改的文件：

- `src/assistant_agent/services/tool_risk_gate.py`
  - 保持现有函数，允许被 interpreter 调用。
- `src/assistant_agent/services/tool_call_boundary.py`
  - 可增加可选 helper 使用 policy view，但不改默认路径。
- `src/assistant_agent/tools/registry.py`
  - 可增加获取 policy view 的只读 helper，不改变注册行为。

要新增的文件：

- `src/assistant_agent/services/tool_policy.py`
- `tests/test_tool_policy_interpreter.py`

不应该修改的部分：

- 不修改 tool 执行顺序。
- 不修改 `ToolResult` 字段。
- 不修改 gateway event 协议。
- 不修改 existing tool names。

行为是否变化：

- 不变化。

测试方案：

- 新增 parity tests：
  - 每个默认 tool 的 `risk_gate_level` 与现有 `risk_gate_level_for_policy` 一致。
  - `requires_confirmation` 与现有 `ToolSideEffectPolicy.requires_confirmation` 一致。
  - memory tool-owned confirmation 行为不变。
  - unknown realtime tool 的 hard gate 行为不变。
- 复跑：
  - `tests/test_tool_risk_gate.py`
  - `tests/test_tool_executor.py`
  - `tests/test_tool_call_boundaries.py`

回滚方案：

- 删除 `tool_policy.py` 和测试，不影响运行时代码。

验收标准：

- 新增 interpreter 只读可用。
- 所有 parity 测试通过。
- 没有任何 runtime 行为变化。

如何支撑后续阶段：

- Phase 1B 用它替换重复 policy 判断。
- Phase 2 用它解释新 metadata。
- Phase 3 local loader 产出的 policy 先进入它。
- Phase 5 MCP adapter 默认 policy 先进入它。
- Phase 6 Skill visibility 可以读取它的 tool risk / data policy。
- Phase 4 realtime lifecycle 用它判断 inline / deferred / confirm_then_execute 和 commit boundary。

### Phase 1B：逐步用 ToolPolicyInterpreter 替换重复 policy 判断

阶段目标：

- 在不改变行为的前提下，逐步减少 policy 解释散落点。

为什么必要：

- 当前 side effect / confirmation / committed / trace redaction 语义分散在 executor、risk gate、realtime reducer、boundary、observation 中。

要修改的文件：

- `src/assistant_agent/agent/tool_executor.py`
  - 用 `ToolPolicyView` 读取 risk / confirmation / idempotency hint。
- `src/assistant_agent/services/tool_call_boundary.py`
  - 用 `ToolPolicyView` 生成 side effect summary。
- `src/assistant_agent/services/realtime_task_state.py`
  - 可选读取 policy view，保持旧推断作为 fallback。
- `src/assistant_agent/services/context/tool_catalog.py`
  - 可使用 policy view 渲染 compact side_effect。

要新增的文件：

- 可新增 `tests/test_tool_policy_parity_integration.py`。

不应该修改的部分：

- 不重写 `ActionValidator`。
- 不拆 `ToolExecutor`。
- 不改变 ToolSpec 对外 JSON schema。

行为是否变化：

- 理论不变化。

测试方案：

- 回归 Phase 0 测试。
- 增加 snapshot/parity 测试：
  - 默认 registry 的 prompt tool payload 不变。
  - pre/post boundary summary 不变。
  - realtime continuation strategy 不变。

回滚方案：

- 将各调用点回退到旧函数，保留 `ToolPolicyInterpreter` 不使用。

验收标准：

- 关键重复 policy 判断减少。
- tests 证明行为不变。

如何支撑后续阶段：

- 降低 Phase 2 metadata 增强时需要修改的核心路径数量。

### Phase 2：ToolSpec metadata 增强，兼容现有 ToolSideEffectPolicy

阶段目标：

- 在 `ToolSpec` 上增加声明式治理 metadata。
- 保持 `ToolSideEffectPolicy` 兼容。

为什么必要：

- 当前 side_effect 无法表达实时策略、审批策略、执行策略、数据策略和 visibility。

要修改的文件：

- `src/assistant_agent/schemas/tools.py`
  - 新增 `ToolPolicyMetadata`、`ToolRisk`、`RealtimeToolPolicy`、`ApprovalPolicy`、`ExecutionPolicy`、`DataPolicy`、`VisibilityPolicy`。
  - `ToolSpec` 新增可选 `policy` 字段。
- `src/assistant_agent/services/tool_policy.py`
  - 优先解释 `ToolSpec.policy`，缺失时回退 `side_effect`。
- `src/assistant_agent/schemas/tool_spec_adapters.py`
  - 控制新 metadata 暴露给 provider/MCP 的范围，不能泄露敏感策略细节。
- `src/assistant_agent/tools/registry.py`
  - 支持工具对象声明新 policy。

要新增的文件：

- `tests/unit/test_tool_policy_metadata.py`

不应该修改的部分：

- 不删除 `ToolSideEffectPolicy`。
- 不要求所有现有工具立刻迁移。
- 不改变 provider-native schema 的核心格式。

行为是否变化：

- 默认不变化。
- 仅当工具显式声明新 policy 且测试覆盖时，才启用新解释。

测试方案：

- 旧工具无 `policy` 时行为不变。
- 新工具同时声明 `side_effect` 与 `policy` 时，interpreter 优先级明确。
- provider adapter 不泄露 internal-only policy。
- `tool_spec_to_mcp_tool` 保持兼容。

回滚方案：

- 保留字段但不使用，或回退 interpreter 优先级。

验收标准：

- 默认 registry 可全部生成 `ToolPolicyView`。
- 新 metadata 不破坏现有 tests。

如何支撑后续阶段：

- Phase 3 decorator/loader 可直接生成新 metadata。
- Phase 4 realtime lifecycle 可依赖 realtime policy。
- Phase 5 MCP adapter 可生成保守默认 policy。
- Phase 6 Skill manifest 可引用 visibility/data policy。

### Phase 3：local tool decorator / explicit loader / tools validate CLI

阶段目标：

- 降低本地 Python tool 接入摩擦。
- 保持显式加载和统一治理。

为什么必要：

- 当前新增工具需要修改多个核心文件。decorator + explicit loader 可以降低重复劳动，但不牺牲治理边界。

要修改的文件：

- `src/assistant_agent/tools/registry.py`
  - 支持注册 loader 输出。
  - 可将 `create_default_registry()` 拆成 core tools + configured local tools。
- `src/assistant_agent/config.py`
  - 增加本地 tool manifest/module 配置，默认关闭或只启用 repo-local allowlist。
- `src/assistant_agent/services/context/tool_catalog.py`
  - 支持 toolset/tags/visibility。

要新增的文件：

- `src/assistant_agent/tools/decorators.py`
- `src/assistant_agent/tools/loader.py`
- `src/assistant_agent/tools/local_manifest.py`
- `src/assistant_agent/tools/cli.py`
- `tests/test_local_tool_loader.py`
- `tests/test_tools_cli.py`

不应该修改的部分：

- 不引入 import-time global registry。
- 不让 decorator 自动执行注册。
- 不让 loader 绕过 `ToolRegistry.register`。
- 不让 CLI 真实调用外部 provider，除非显式 profile/flag。

行为是否变化：

- 默认不变化。
- 只有配置启用 local tools 时才新增工具。

测试方案：

- decorator 生成正确 args schema。
- loader 显式加载指定 module。
- name collision 被拒绝。
- missing requires_env 时 disabled。
- validate CLI 能发现缺少 policy / timeout / redaction 的工具。
- simulate CLI 通过 `ActionValidator`，dry-run 不绕过 executor contract。

回滚方案：

- 关闭配置开关。
- 从 `create_default_registry()` 移除 loader 调用。

验收标准：

- `weather.lookup` 这类只读工具无需修改 `ActionValidator` 和核心 registry 常量即可被加载、验证、执行。
- 所有执行仍经过 validator/executor。

如何支撑后续阶段：

- 为 MCP adapter 和 Skill manifest 提供 normalize/register 的参考实现。

### Phase 4：realtime tool lifecycle / commit boundary

阶段目标：

- 为实时通话工具调用建立显式 lifecycle 和 commit boundary。

为什么必要：

- 个人实时通话助理的关键不是“能不能调用工具”，而是工具是否会阻塞 TTS、是否能取消、打断时是否已提交、是否需要口头确认、能否恢复。

要修改的文件：

- `src/assistant_agent/schemas/tools.py`
  - 扩展 `ToolCallRecord` 或新增 `ToolExecutionRecord`。
- `src/assistant_agent/schemas/events.py`
  - 增加内部 lifecycle event 或 event payload 字段。
- `src/assistant_agent/agent/tool_executor.py`
  - 在执行前、确认前、提交后、取消时记录 lifecycle。
- `src/assistant_agent/services/realtime_task_state.py`
  - 从显式 lifecycle 更新 state，而不是只靠 `tool.finished` 推断。
- `src/assistant_agent/gateway/event_mapping.py`
  - 保持旧 frame 兼容，必要时增加 payload 字段。
- `src/assistant_agent/gateway/session.py`
  - 确保 cancel token 与 commit boundary 的语义一致。

要新增的文件：

- `src/assistant_agent/services/tool_lifecycle.py`
- `tests/test_tool_lifecycle.py`
- `tests/test_realtime_tool_commit_boundary.py`

不应该修改的部分：

- 不新建第二套 runtime。
- 不让 Gateway 直接执行或决定 tool policy。
- 不破坏现有 `tool.started` / `tool.finished` / `tool.failed` 事件。

行为是否变化：

- 对外默认尽量不变化。
- 内部 trace/audit/realtime state 更精确。
- 需要确认的工具可能从“成功返回 confirmation_required”逐步演进为显式 `pending_confirmation` 状态。

测试方案：

- read-only inline tool：started -> succeeded。
- confirmation tool：started -> pending_confirmation，不执行外部写。
- cancellable tool before execution：cancelled_before_commit。
- cancel after commit：interrupted_after_commit，并保留 committed result。
- deferred tool：返回 deferred 状态，不阻塞 TTS。
- gateway interrupt 不应把已 committed 的工具误标为 cancelled。

回滚方案：

- 保留旧事件路径。
- Realtime reducer 回退到旧 `tool.finished` / side_effect 推断。

验收标准：

- RealtimeTaskState 能明确回答：
  - 当前是否有 pending tool。
  - 是否已 commit。
  - 是否可取消。
  - 用户打断后应该 resume、compensate、report_committed 还是 restart。

如何支撑后续阶段：

- MCP 和 Skill 引入后，也能共享相同实时生命周期。

### Phase 5：inbound MCPToolAdapter

阶段目标：

- 支持把外部 MCP server 的 tool definition 接入为内部 governed tools。

为什么必要：

- 借鉴 OpenClaw 的协议化接入能力，但不让 MCP 成为执行绕过口。

要修改的文件：

- `src/assistant_agent/tools/registry.py`
  - 支持注册 adapter-generated proxy tools。
- `src/assistant_agent/config.py`
  - 增加 MCP server allowlist / disabled-by-default 配置。
- `src/assistant_agent/services/tool_policy.py`
  - 为未知 MCP tool 生成保守默认 policy。
- `src/assistant_agent/services/context/tool_catalog.py`
  - 支持 MCP namespace visibility。

要新增的文件：

- `src/assistant_agent/mcp/adapter.py`
- `src/assistant_agent/mcp/client.py`
- `src/assistant_agent/mcp/config.py`
- `tests/test_mcp_tool_adapter.py`
- `tests/test_mcp_tool_allowlist.py`

不应该修改的部分：

- 不替换现有 `OfflineMCPServer`。
- 不让 MCP tool 直接调用 memory manager。
- 不让 MCP tool 直接发 gateway frame。
- 不默认启用所有外部 MCP tools。

行为是否变化：

- 默认不变化。
- 配置 allowlist 后新增 `mcp.<server>.<tool>` 工具。

测试方案：

- 外部 tool definition normalize 成 `ToolSpec`。
- 默认 disabled。
- allowlist 后 namespace 注册。
- name collision 被拒绝。
- unknown risk 默认需要确认或禁用。
- MCP tool run 经过 `ActionValidator` / `ToolExecutor`。
- MCP timeout / error 转为结构化 `ToolResult`。
- trace redaction 生效。

回滚方案：

- 关闭 MCP allowlist。
- 移除 adapter 注册。

验收标准：

- inbound MCP tool 与 local tool 在执行链路上无差异。

如何支撑后续阶段：

- Skill manifest 可以声明 MCP namespace tool，但仍只声明 capability。

### Phase 6：Skill manifest v1 演进

阶段目标：

- 增强 Skill manifest 的声明能力，但保持 Skill 不执行代码。

为什么必要：

- Skill 是个人助理能力组织方式，不应变成插件 runtime。

要修改的文件：

- `src/assistant_agent/services/context/skill_loader.py`
  - 支持 manifest v1 字段。
- `src/assistant_agent/services/context/capability_catalog.py`
  - 根据 skill visibility / permissions 暴露 capability。
- `src/assistant_agent/services/context/tool_catalog.py`
  - 根据 skill visibility 限制 prompt-visible tools。
- `src/assistant_agent/schemas/context.py`
  - 扩展 skill exposure report。

要新增的文件：

- `tests/test_skill_manifest_v1.py`
- 示例 skill manifest，可放在测试 fixture，不一定加入默认 skills。

不应该修改的部分：

- 不新增 `run_skill`。
- 不让 Skill 直接执行 Python。
- 不让 Skill 直接调用外部 API。
- 不让 Skill 直接写 memory。

行为是否变化：

- 默认不变化。
- 启用 manifest v1 的 skill 可影响工具可见性和 prompt。

测试方案：

- skill 声明不存在的 tool 被拒绝或 audited。
- skill 声明缺少 permission 被拒绝。
- disabled skill 不暴露 capability。
- skill visibility 能限制 prompt tool catalog。
- skill tests 字段只用于验证，不用于 runtime 执行。

回滚方案：

- loader 回退到现有 descriptor 字段。
- 忽略 v1 新字段。

验收标准：

- Skill 仍是 capability declaration。
- 没有第二套执行入口。

如何支撑后续阶段：

- 配合 ToolResult presentation split，为不同 skill 提供更合适的 response style 和 tool visibility。

### Phase 7：ToolResult presentation split

阶段目标：

- 将 tool result 的 voice、model、trace、audit、raw data 表达拆分。

为什么必要：

- 实时通话需要短口播，模型需要结构化 observation，审计需要完整可信，trace 需要脱敏可读，不能长期挤在 `data` 字段里。

要修改的文件：

- `src/assistant_agent/schemas/tools.py`
  - 扩展 `ToolResult`。
- `src/assistant_agent/schemas/tool_observation.py`
  - 优先读取 `model_observation`。
- `src/assistant_agent/services/tool_call_boundary.py`
  - 使用 `trace_summary`。
- `src/assistant_agent/services/realtime_task_state.py`
  - 使用 `voice_summary` / lifecycle metadata。
- `src/assistant_agent/services/tool_history.py`
  - 分离 audit payload 和 prompt-safe summary。

要新增的文件：

- `tests/test_tool_result_presentation.py`

不应该修改的部分：

- 不强制所有工具一次性迁移。
- 不把 raw external response 写入仓库或 trace。

行为是否变化：

- 对已迁移工具，语音和模型 observation 更稳定。
- 未迁移工具回退旧 `data` 行为。

测试方案：

- `voice_summary` 不泄露敏感字段。
- `model_observation` 结构化并可被 provider loop 消费。
- `audit_payload` 可脱敏保存。
- `raw_data_ref` 不直接进入 prompt。
- legacy `ToolResult.data` 仍兼容。

回滚方案：

- observation 和 realtime summary 回退 `data`。

验收标准：

- 同一 tool result 可同时满足 TTS、LLM、trace、audit，不互相污染。

如何支撑后续阶段：

- 为真实私人数据工具、MCP 外部工具和高风险写工具提供更清晰的数据边界。

## 6. 最小可行切片

### 6.1 切片 1：现有 read-only 工具验证 ToolPolicyInterpreter 不改变行为

选择现有只读工具，例如 `web_search` 或 `memory_retrieval` 的只读路径。

目标：

- 引入 `ToolPolicyInterpreter`。
- 对现有 `ToolSideEffectPolicy` 做 parity。
- 不改 tool 行为。

完整链路：

```text
ToolSpec(side_effect=external_read/local_read)
  -> ToolPolicyInterpreter.from_spec()
  -> ToolPolicyView
  -> ActionValidator 原行为
  -> ToolExecutor 原行为
  -> ToolResult
  -> ToolObservation
  -> Trace / RealtimeTaskState 原行为
```

验收：

- 现有 tests 全部保持。
- risk gate 输出一致。
- pre/post boundary summary 一致。

### 6.2 切片 2：weather.lookup 验证 local tool decorator / loader

选择 `weather.lookup`，因为它是外部只读、低风险、适合实时通话内 inline 返回，但又需要 data policy。

建议 policy：

```yaml
risk: external_read
realtime:
  mode: inline
  expected_latency_ms: 800
approval:
  mode: never
execution:
  timeout_s: 3
  retry_count: 0
  max_result_chars: 1200
data:
  sends_data_external: true
  redact_in_trace: true
visibility:
  toolset: personal.readonly
  enabled_by_default: false
```

完整链路：

```text
@tool weather.lookup
  -> explicit local loader
  -> ToolRegistry.register
  -> ToolSpec / ToolPolicyView
  -> Capability / Visibility
  -> ActionValidator
  -> ToolExecutor
  -> weather handler
  -> ToolResult
  -> ToolObservation
  -> Trace / Audit / RealtimeTaskState
```

验收：

- 不修改 `ActionValidator` tool_name 特判也能加载。
- `tools validate` 能验证 schema/policy。
- simulate 能跑通 validator/executor。
- 默认不开启真实外部调用，除非配置允许。

### 6.3 切片 3：calendar.search_events 验证外部私人数据只读、redaction、skill visibility

选择 `calendar.search_events`，因为它是外部只读，但涉及私人数据和 trace redaction。

建议 policy：

```yaml
risk: external_read
realtime:
  mode: blocking
  expected_latency_ms: 1500
approval:
  mode: conditional
execution:
  timeout_s: 5
  max_result_chars: 2000
data:
  reads_private_data: true
  sends_data_external: true
  redact_in_trace: true
visibility:
  toolset: personal.calendar
  enabled_by_default: false
  skill_only: true
```

完整链路：

```text
Skill manifest declares calendar capability
  -> SkillLoader validates governed tool + permission
  -> CapabilityCatalog exposes capability
  -> ToolCatalog exposes calendar.search_events when visible
  -> ActionValidator validates request/schema/policy
  -> ToolExecutor executes via registry
  -> ToolResult(audit redacted, model observation compact)
  -> Realtime response with short voice summary
```

验收：

- disabled skill 不暴露工具。
- missing permission 不暴露工具。
- trace 不泄露 calendar raw payload。
- model observation 可以包含必要事件摘要。

### 6.4 切片 4：calendar.create_event 验证 confirmation、idempotency、commit boundary

最后再做 `calendar.create_event`，因为它是外部写工具，涉及确认、幂等、提交边界和 interrupt。

建议 policy：

```yaml
risk: external_write
realtime:
  mode: confirm_then_execute
  interruptible: false
  commit_boundary: external_commit
approval:
  mode: always
  confirmation_kind: verbal
execution:
  timeout_s: 8
  idempotency: required
  retry_count: 0
data:
  reads_private_data: true
  writes_private_data: true
  sends_data_external: true
  redact_in_trace: true
visibility:
  toolset: personal.calendar
  enabled_by_default: false
  skill_only: true
```

完整链路：

```text
ToolSpec / ToolPolicy
  -> Capability / Visibility
  -> ActionValidator
  -> ToolExecutor
  -> ToolLifecycle: pending_confirmation
  -> user verbal confirmation
  -> ToolExecutor executes
  -> ToolLifecycle: committed
  -> ToolResult
  -> Audit with idempotency key
  -> Realtime response reports committed or interrupted_after_commit
```

验收：

- 没有确认时不执行外部写。
- 确认后执行必须有 idempotency key。
- cancel before commit 不创建事件。
- interrupt after commit 不把结果误报为 cancelled。
- audit 能说明谁确认、确认了什么、何时提交、外部结果引用是什么。

## 7. 测试矩阵

| 阶段 | registry / schema | policy interpreter | validator | executor | risk gate | realtime interrupt / cancel | commit boundary | trace / audit | MCP allowlist / namespace | skill visibility / permission | ToolResult presentation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Phase 0 | `tests/unit/test_tool_registry.py`, `tests/unit/test_tool_spec_adapters.py` | 无 | `tests/test_phase0_tool_governance_contracts.py` | `tests/test_tool_executor.py` | `tests/test_tool_risk_gate.py` | `tests/test_realtime_task_state.py` | 现有 side effect 推断 | `tests/test_tool_call_boundaries.py` | `tests/test_mcp_server_skeleton.py` | `tests/test_phase3_skill_system_gate.py` | `tests/test_native_tool_call_handoff.py` |
| Phase 1A | 默认 specs 可生成 policy view | 新增 `tests/test_tool_policy_interpreter.py` | 行为不变回归 | 行为不变回归 | parity with current risk gate | 行为不变回归 | 不新增 | boundary parity | 不涉及 | 不涉及 | 不涉及 |
| Phase 1B | prompt payload parity | integration parity | validator 不变 | executor 改用 policy view | risk gate 不变 | realtime reducer parity | 不新增 | trace summary parity | 不涉及 | tool catalog parity | observation parity |
| Phase 2 | 新 metadata schema tests | 新 metadata 优先级 tests | policy-driven validation smoke | executor 读取 policy smoke | 新 risk mapping tests | realtime policy smoke | commit policy metadata tests | redaction policy tests | 不涉及 | visibility policy tests | 不涉及 |
| Phase 3 | decorator/loader schema tests | loader policy tests | simulate CLI passes validator | simulate CLI dry-run/execution tests | side effect/risk audit tests | inline timeout/cancel smoke | 不涉及 | CLI audit tests | 不涉及 | toolset visibility tests | legacy result |
| Phase 4 | lifecycle schema tests | realtime policy -> lifecycle tests | confirmation validation tests | lifecycle recording tests | hard/soft gate lifecycle tests | cancel before/during/after tests | `tests/test_realtime_tool_commit_boundary.py` | committed/interrupted audit tests | 不涉及 | 不涉及 | voice fallback |
| Phase 5 | MCP normalized spec tests | MCP default policy tests | MCP tool validate tests | MCP proxy execution tests | unknown MCP risk tests | MCP timeout/cancel tests | MCP external commit disabled by default | MCP redaction/audit tests | `tests/test_mcp_tool_allowlist.py` | MCP tools hidden unless allowed | MCP result mapping |
| Phase 6 | skill manifest schema tests | skill permission uses policy view | missing permission rejection | no direct execution test | high-risk skill visibility tests | no runtime bypass | no direct commit | skill exposure audit | MCP namespace in skill tests | `tests/test_skill_manifest_v1.py` | no change |
| Phase 7 | result schema compatibility tests | redaction based on data policy | validator no change | executor result handling tests | no change | voice summary under interrupt tests | committed result presentation tests | audit payload tests | MCP raw_data_ref tests | skill-specific voice/model tests | `tests/test_tool_result_presentation.py` |

额外建议的回归命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py tests/test_tool_executor.py tests/test_tool_risk_gate.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_task_state.py tests/test_tool_call_boundaries.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_mcp_server_skeleton.py tests/test_phase3_skill_system_gate.py
```

## 8. 不建议做的事

- 不做插件市场。
- 不把本项目做成 AI OS。
- 不做第二套 runtime。
- 不让 Skill 执行代码。
- 不新增 `run_skill`。
- 不让 MCP 绕过 `ActionValidator` / `ToolExecutor`。
- 不让 MCP tool 直接写 memory。
- 不让 MCP tool 直接发 gateway frame。
- 不引入不可控的 import-time global registry。
- 不把 confirmation 只写在 prompt。
- 不一次性大改 `ToolSpec`、`ToolExecutor`、`RealtimeTaskState`。
- 不删除 `ToolSideEffectPolicy` 后再补兼容。
- 不为了 decorator 低摩擦牺牲 trace/audit/realtime lifecycle。
- 不因为检测到 API key 就自动启用外部工具。
- 不把 OpenClaw 的 extension runtime 或 Hermes 的全局 registry 原样搬进当前项目。
- 不让 provider-native tool call 成为绕过治理链路的快捷口。
- 不把 raw external response 直接塞进 prompt、trace 或仓库文件。

## 9. 实施原则

1. 先建解释层，再迁移判断。
2. 先做只读工具，再做外部私人数据读，再做外部写。
3. 先保持行为 parity，再新增能力。
4. 先内部 lifecycle，再扩展 Gateway 对外协议。
5. 先 explicit loader，再考虑更复杂的 adapter/plugin。
6. 所有工具来源都必须 normalize 成内部 `ToolSpec` / `ToolPolicyView`。
7. 所有工具执行都必须经过 `ActionValidator` / `ToolExecutor`。
8. 所有副作用都必须可解释、可审计、可恢复或可补偿。

最终目标不是让工具“更像插件市场”，而是让个人实时通话助理在本地优先、可治理、可审计、可中断的前提下，安全地接入更多个人能力。
