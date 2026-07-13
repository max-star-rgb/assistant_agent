# Tool Calling Architecture

本文件是 tool calling、ToolSpec、ActionValidator、ToolExecutor、ToolRegistry、provider-native tool calls 和 MCP `tool_run` 的当前权威入口。涉及记忆工具或 agent delegation 的服务内部设计时，还要分别阅读 `docs/memory-service-architecture.md` 和 `docs/agent-communication-routing.md`；本文件只定义工具调用边界和运行链路。

## 新对话快速交接

- 默认真实 LLM 运行时是 `AgentGraphRuntime` 内的 provider-native loop；非 mock chat adapter 不再进入 prompt-json 控制面调用。
- 工具契约由 `ToolSpec` 表达，来源是 `ToolRegistry.list_specs()`；真实 LLM 路径把 ToolSpec 转成 OpenAI-compatible tools schema，并通过 provider 原生 `content` / `tool_calls` 判断本轮响应类型。
- `ToolSpec.side_effect` 表达工具副作用策略，`ToolSpec.execution` 表达稳定调度/依赖/资源事实；未知工具默认按 confirmation-sensitive 且需要串行观察处理。realtime task-state 会消费 side-effect 策略判断 interrupt 后应重规划、等待确认、补偿还是报告已提交动作。
- 真实 LLM 只能返回自然语言 `content` 或 provider-native `tool_calls`。native tool call 会先归一化成内部 `AssistantDecision(type="tool_call")`，再走同一套校验和执行。
- 当第一轮 native response 是 `tool_calls` 时，runtime 不把该轮模型 `content` 当作正式回答输出；它只记录为内部 preamble，并发出一条可替换的 `progress_message` 事件（例如 `product_search` -> “我查一下。”）。该事件不写入 LLM messages，也不参与第二轮回答生成。
- 工具执行必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。API、WebSocket、MCP 或新增入口都不能直接 `registry.run(...)`。
- `ToolExecutor` 是运行时治理边界：重新从 registry 解析 `ToolPolicyView`，绑定 user/session 身份，执行 approval/idempotency、安全 retry 和 deadline 传播，检查 provider budget，记录 state/tool history/event/trace，再调用 registry。
- 工具实现应保持薄适配层：Pydantic input/output schema、调用 adapter/service、包装 `ToolResult` 和 `CapabilityOutputContract`。真实外部能力必须在 provider/service adapter 层受 runtime profile 控制。
- 工具 observation 是下一轮 LLM 的数据，不是系统指令；写入 prompt 前会被摘要、脱敏、压缩。不要把 provider raw response、密钥、base64 大 payload 暴露给 assistant context。
- Provider capability facts live in `src/assistant_agent/schemas/provider_specs.py` as `ProviderSpec.capabilities`; chat adapters read that matrix for native tools, response format, streaming and modality switches instead of maintaining a separate provider table. Adapter factories should prefer `ResolvedProviderSpec.adapter_kind` / `ResolvedProviderSpec.capabilities` and must not maintain parallel provider-name dispatch tables.
- 当前 native loop 会先对同一轮 provider-native `tool_calls` 做 `ToolScheduler` 预检：明确 read-only、无确认需求、`execution.dependency_mode=independent`、无重复工具名、无资源写入、无 concurrency/resource 冲突且 provider budget 可安全检查的 batch 可以并发执行；`requires_prior_observation`、`terminal`、`needs_confirmation`、`unsafe` 或未知执行属性继续按 provider 顺序串行执行。无论并发或串行，每个工具仍独立进入 `ActionValidator -> ToolExecutor -> ToolRegistry`，并受 `max_tool_iterations` 预算限制。mock/local/offline 仍保留 deterministic rule plan，用于稳定测试和演示。

## 设计收敛原则

本项目工具系统的主身份是：本地优先、受治理、provider-native 的 assistant tool system。对 Hermes、LangChain、OpenClaw、Claude Code 等外部设计只能分层借鉴，不能按某一套系统整体迁移。

- 不可突破的主干是 `AgentGraphRuntime -> provider-native tool_calls -> AssistantDecision -> ActionValidator -> ToolExecutor -> ToolRegistry -> ToolResult/Observation`。
- 借鉴 Hermes 时，只优先吸收低摩擦、低抽象的注册/发现思想；不要引入模块级全局 `registry` 或绕过依赖注入的执行入口。
- 借鉴 LangChain 时，只保留类型契约、Pydantic schema 和结构化结果这类可测试边界；不要增加 `BaseTool -> StructuredTool -> ToolNode` 式多层抽象。
- 借鉴 OpenClaw 时，优先吸收权限、审批、side-effect、trace、budget、plugin 安全边界；不要提前建设 npm extension、Canvas、Node 或大插件生态，除非已有明确第三方扩展需求。
- 新增工具能力必须先映射到现有治理链路和 `ToolSpec` 契约；无法映射的设计先进入 Backlog，不直接改 runtime。

## 当前主调用链

```text
UserRequest
  -> AgentGraphRuntime.run_state
  -> provider-native runtime loop, if chat adapter is non-mock
  -> load_memory
  -> ToolRegistry.list_specs()
  -> build_assistant_context_pack()
  -> ChatRequest(messages=[...], tools=[...], tool_choice="auto")
  -> ChatAdapter.chat()
       ├─ content/refusal
       │    -> stream response_delta when available
       │    -> final_response
       └─ tool_calls[]
            -> each native tool call normalized to AssistantDecision(type="tool_call")
            -> ToolScheduler batch precheck through ActionValidator.validate()
            -> execute read-only independent execution-safe calls concurrently, otherwise serially
            -> ToolExecutor.run_tool()
            -> append assistant tool_call + tool observation messages in provider order
            -> ChatAdapter.chat() again for final content or next tool call

Mock/local/offline compatibility path:

UserRequest
  -> AgentGraphRuntime.run_state
  -> build_assistant_loop_graph
  -> load_memory
  -> assistant_node
       -> deterministic rule plan
       -> AssistantDecision
  -> route_after_assistant
  -> execute_requested_tool_node
       -> optional plan step check, if local plan state is active
       -> ActionValidator.validate()
       -> ToolExecutor.run_tool()
            -> bind runtime identity
            -> ProviderCallBudget.check_before_call()
            -> AgentState.add_tool_call()
            -> tool_started event / ToolHistoryStore.record_start()
            -> ToolRegistry.run()
            -> tool.run(input, ToolContext)
            -> provider budget record, retry/recovery, events/history/trace
       -> ToolObservation
  -> assistant_node again, or compose_response
  -> save_memory
```

相关源码：

- `src/assistant_agent/agent/runtime.py`: 组装 registry、memory manager、chat adapter、tool executor、trace store，并承载真实非 mock provider 的 native content/tool_calls 主循环。
- `src/assistant_agent/agent/tool_scheduler.py`: provider-native tool batch 的保守调度器；只把明确 read-only、独立、预算安全的 batch 标为 parallel。
- `src/assistant_agent/agent/assistant_loop_nodes.py`: mock/offline assistant loop、native tool handoff 兼容节点、validator 调用、observation 回传和 loop guard。
- `src/assistant_agent/agent/action_validator.py`: 工具执行前的本地校验边界。
- `src/assistant_agent/agent/tool_executor.py`: 工具执行、预算、retry/recovery、event/history/trace 的统一边界。
- `src/assistant_agent/tools/registry.py`: 工具注册、ToolSpec 生成和默认工具集合。
- `src/assistant_agent/tools/base.py`: `ToolContext`、`BaseTool`、`MockTool` 基础契约。
- `src/assistant_agent/schemas/tools.py`: `ToolSpec`、`ToolExecutionPolicy`、`ToolResult`、`ToolCallRecord`。
- `src/assistant_agent/schemas/assistant_decision.py`: 内部 assistant decision 和 native tool call 归一化。
- `src/assistant_agent/schemas/tool_spec_adapters.py`: ToolSpec 到 OpenAI/MCP 工具 schema 的转换。
- `src/assistant_agent/schemas/tool_observation.py`: assistant-facing observation 摘要和脱敏。

## Provider-Native Only

生产 runtime 只保留 provider-native tool calling：

- 非 mock chat adapter 一律走 native loop；不再存在 `response_mode` 或 `assistant_tool_call_mode` 分支。
- 请求向 provider 发送 `messages`、`tools` 和 `tool_choice="auto"`。
- provider 返回 `content` 或 `refusal` 时，runtime 直接作为用户可见回答输出；普通 direct answer 应只有一次 chat call。
- provider 返回 `tool_calls` 时，先丢弃/记录本轮模型 preamble，发出可替换 `progress_message`，再把 batch 归一化为内部 `AssistantDecision(type="tool_call")` 并进入 `ToolScheduler`。调度器会批量预检 `ActionValidator`，并通过 `ToolPolicyInterpreter` 消费 `ToolSpec.side_effect + ToolSpec.execution`；只有明确 read-only、无确认需求、`dependency_mode=independent`、无重复工具名、无资源写入、无 concurrency/resource 冲突、预算检查可并发安全的 batch 才会并发调用 `ToolExecutor.run_tool()`。其他情况保持 provider 顺序串行。无论哪种模式，observation 都按 provider call 顺序回填；超过 `max_tool_iterations` 的剩余调用会记录为预算跳过，而不是绕过治理边界。
- 工具 observation 会作为后续 native messages 中的 `tool` role 内容回传；拿到工具结果后的第二次 LLM 调用是合理工具路径，不是隐藏控制面调用。
- 如果 provider adapter 明确 `supports_native_tools=False`，runtime fails immediately，不静默 fallback 到旧 JSON 控制面。
- `AssistantDecision` 仍是内部治理结构，用于复用 validator/executor/trace；真实 LLM 不再被要求输出自定义 `AssistantDecision` JSON。

## Backlog

- 安全并行工具执行扩展：当前 v1 已支持单个 read-only independent native batch 的保守并发执行，并消费静态 `ToolSpec.execution` 做依赖、资源写入和 concurrency group 检查。后续如需扩展到多组调度、重复同名 read-only 工具、动态依赖声明、realtime 动态 `max_tool_iterations` 或 async executor，必须继续保持 `ToolExecutor` 为唯一执行入口，并保留每个工具独立的 trace、event、history、observation 和预算记录。副作用工具、confirmation-sensitive 工具、未知工具、资源或路径可能冲突的工具默认继续串行。
- ToolRegistry 延迟注册 / factory descriptor：当前不采用 Hermes 风格的模块级全局单例 `registry`，避免 provider profile、adapter、video context、agent delegation 和测试隔离被全局状态污染。后续如果默认工具数量或 import 耦合明显增长，可以引入保留依赖注入语义的 `ToolRegistryBuilder` 或 lazy tool factory descriptor：`registry.py` 只保存轻量工具定义和 factory，`create_default_registry()` 按 `ProviderConfig`、runtime context 和 opt-in capability 实例化工具。该方案不得绕过 `ActionValidator -> ToolExecutor -> ToolRegistry`，也不得把工具执行入口改成全局 `registry.run(...)`。

## ToolSpec 契约

`ToolSpec` 是唯一工具契约视图：

```text
name
description
input_schema
required_inputs
when_to_use
when_not_to_use
runtime_constraints
side_effect
execution
```

ToolSpec 由 `ToolRegistry.list_specs()` 从工具类的 Pydantic `input_schema` 生成，并补充 `_ACTION_USAGE` 中的使用条件和运行约束。默认约束包含 `Use only through ToolExecutor.`。

注意事项：

- memory dedicated tools 会隐藏 `user_id`、`session_id` 这类运行时身份字段；模型不应决定记忆归属。
- `tool_spec_to_json_schema()` 会输出 object schema，并设置 `additionalProperties=False`。
- provider-native tools 发送完整 ToolSpec，并把 `terminal` / `requires_prior_observation` 等执行约束追加成简短 prompt-safe 描述；旧 prompt-facing ToolSpec 子集只服务历史 renderer 测试和离线兼容材料，不是生产决策路径。
- `side_effect` 包含 `level`、`requires_confirmation`、`description`、可选 `confirmation_kind` 和 `compensation_hint`；provider-native/MCP 描述也会包含该信息。
- `execution` 包含 `dependency_mode`、可选 `concurrency_group`、`resource_reads`、`resource_writes`、`realtime_safety`、`artifact_reuse` 和可选 `progress_message`。它只表达调度/依赖/资源事实、realtime artifact 复用提示和等待提示，不表达“允许并发”命令。
- 未分类工具使用保守默认：`level=pending_confirmation`、`requires_confirmation=true`、`dependency_mode=requires_prior_observation`、`realtime_safety=needs_confirmation` 且 `artifact_reuse=requires_validation`。
- MCP 工具 schema 通过 `tool_spec_to_mcp_tool()` 支持，但当前 `OfflineMCPServer.list_tools()` 暴露的是 MCP wrapper 工具；registry ToolSpec 通过 `tool_list` 返回。

## Tool Side-Effect Policy

工具副作用策略是工具治理元数据，不属于 Gateway 协议。

- read-only 工具应标为 `local_read` 或 `external_read`，例如 `memory_retrieval`、`web_search`、`product_search`、`price_compare`、image/video understanding。
- 创建可替换 artifact 的工具标为 `compensatable`，例如 `image_generation` 和 `render_3d`；中断后应生成修正版或说明已有 artifact，而不是宣称旧结果被撤销。
- confirmation-sensitive 工具标为 `pending_confirmation`，例如 `memory_save` 和 legacy `memory`；如果工具结果返回 `requires_confirmation=true` 或 `confirmation_id`，realtime task-state 会记录 pending confirmation。
- 如果 confirmation-sensitive 或未知工具已经成功返回，realtime task-state 会把它视为 `committed`，中断后的下一轮必须报告已发生状态或提供安全后续动作。
- 工具结果可用 `data.side_effect`、`data.side_effect_level`、`data.requires_confirmation`、`data.confirmation_id` 和 `data.compensation_hint` 覆盖静态策略。

Runtime gate 映射：

- `auto`: `local_read`、`external_read` 或无副作用工具，直接执行，不需要幂等 ledger。
- `soft_gate`: `compensatable` 工具，执行前解析或生成幂等 key；同一 user/session/tool/key 已提交时返回 safe duplicate-suppressed result，不再次调用 registry。
- `hard_gate`: `pending_confirmation`、`committed` 或未分类工具。realtime/Gateway 上下文中的未分类工具会返回 pending-confirmation result，不执行实际工具；`memory` / `memory_save` 继续交给 memory service 自己的确认边界处理。
- 显式确认后的外部写工具可通过 request metadata `tool_confirmation.confirmed=true` 和匹配的 `tool_name` 放行；若工具声明 `execution.idempotency=required`，确认后仍必须提供 `idempotency_key`，成功提交会写入幂等 ledger，重复提交返回 duplicate-suppressed result。
- `block`: 预留给后续策略禁止类工具，本阶段没有默认工具映射到 block。

当前限制：risk gate 和 idempotency ledger 是 process-local runtime guard；完整用户确认 UX、持久化 ledger、跨进程恢复和 irreversible external action provider 仍未接入。

## 当前默认工具

`create_default_registry()` 默认注册：

- `vision_understanding`
- `video_understanding`
- `web_search`
- `product_search`
- `price_compare`
- `image_generation`
- `render_3d`
- `memory`
- `memory_retrieval`
- `memory_save`

`delegate_to_agent` 是 opt-in 工具，仅在 `enable_agent_delegation=True` 且传入 `AgentCommunicationService` 时注册。它的通信路由和 A2A 边界以 `docs/agent-communication-routing.md` 为准。

Repo-local Skill System v1 manifests under `skills/<skill_id>/SKILL.md` are capability metadata, not execution plugins. A skill can declare governed tools and `tool:<name>` permissions, and the context capability catalog may expose that prompt-safe descriptor only when the governed tools are available and prompt-selected. Unknown permission vocabulary is rejected before prompt rendering, and a repo-local skill id suppresses same-name built-in fallback even when the local manifest is disabled or invalid. Skill-only tools are hidden from prompt fallback unless a valid skill manifest declares the governed tool, has the matching permission, and matches the current request. Skill exposure is reported through prompt-safe `skill_report_v1`; skills do not register `run_skill`, do not call `ToolRegistry.run(...)`, and do not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.

Repo/user-local Python tools use the explicit `@tool` decorator plus `load_local_tools()` / `register_local_tools()`. They are not import-time global registrations. A local tool may declare `ToolPolicyMetadata` and `ToolExecutionPolicy`; when present, runtime risk gate, boundary summaries, scheduler metadata, trace/history summaries, and `tools simulate` consume that declaration through the same `ToolSpec -> ToolPolicyView -> ActionValidator -> ToolExecutor` path. `assistant_agent.tools.cli validate` checks declaration shape and policy requirements; `simulate` executes one explicitly loaded tool through validator/executor for local verification.

默认副作用分类：

- `vision_understanding`、`video_understanding`、`web_search`、`product_search`、`price_compare`: `external_read`。
- `memory_retrieval`: `local_read`。
- `memory_ingest_status`: `external_read`。
- `image_generation`、`render_3d`: `compensatable`。
- `memory`、`memory_save`: `pending_confirmation`，成功提交后从 realtime interrupt 视角记为 `committed`。
- `memory_media_ingest`: `committed` / confirmation-sensitive because it submits media to an external Memory Server task that may create durable remote memories. It is only selected for explicit media-ingestion-into-memory requests and returns `provider_unconfigured` unless remote memory is explicitly configured.
- `delegate_to_agent`: `compensatable`，因为子任务可能已经开始，需要取消、覆盖或补发修正。

默认执行属性：

- `web_search`、`product_search`、`memory_retrieval`、`memory_ingest_status`、`vision_understanding`、`video_understanding`: `dependency_mode=independent`、`realtime_safety=safe`、`artifact_reuse=reusable`。
- `price_compare`: `dependency_mode=requires_prior_observation`，因为同一批次中通常需要先消费商品候选或先前 observation。
- `image_generation`、`render_3d`: `dependency_mode=terminal`、`realtime_safety=needs_progress`、`artifact_reuse=requires_validation`。
- `delegate_to_agent`: `dependency_mode=terminal`、`realtime_safety=needs_progress`、`artifact_reuse=do_not_reuse`。
- `memory_save`、`memory_media_ingest`: `dependency_mode=independent`、`realtime_safety=needs_confirmation`、`artifact_reuse=do_not_reuse`，并声明 memory 资源写入。
- legacy `memory`: 保守视为 `requires_prior_observation`、`needs_confirmation`、`artifact_reuse=do_not_reuse`，并声明 memory 读写。

## Web Search 工具

`web_search` 是只读实时信息检索工具，用于“最新消息 / 实时信息 / 今天新闻 / 联网搜索 / current/latest/news/web search”等时间敏感请求。它只返回搜索结果列表和摘要输入，不做网页全文抓取、浏览器渲染、爬虫或多页面阅读。

输入字段：

- `query`: 必填搜索 query。
- `recency_days`: 可选时间窗口。
- `site_filter`: 可选站点过滤。
- `limit`: 结果数量，schema 限制为 1-10。

输出字段：

- `query_used`
- `results[]`，每项包含 `title`、`url`、`snippet`、可选 `source` 和 `published_at`
- `summary`
- `provider`
- `latency_ms`
- `output_ref`

Provider 边界：

- 默认 `MULTIMODAL_AGENT_SEARCH_PROVIDER=mock`，`MockWebSearchAdapter` 返回稳定假数据。
- 真实 HTTP 搜索必须同时满足 `provider_smoke` 或 `pilot` runtime profile、显式 `MULTIMODAL_AGENT_SEARCH_PROVIDER=http`、`WEB_SEARCH_BASE_URL` 和 `WEB_SEARCH_API_KEY`。
- local/offline profile 即使检测到 `WEB_SEARCH_API_KEY` 也不会选择 http provider。
- `HttpWebSearchAdapter` 缺配置时返回结构化 `provider_unconfigured`，不 fallback 到 mock。
- v1 通用 HTTP 后端使用 JSON POST：`query`、`recency_days`、`site_filter`、`limit`；响应应包含 `results` 或 `items`。

## ActionValidator 边界

`ActionValidator.validate()` 在执行前拒绝不安全或不可执行的 action。当前校验包括：

- `decision.type != "tool_call"` 时不执行工具。
- 必须有 `tool_name`，`tool_input` 必须是 JSON object。
- `tool_name` 必须存在于当前 registry。
- `vision_understanding` 必须有 `image_ids`，`video_understanding` 必须有 `video_ref` 或 `video_ids`。
- `image_generation` 必须有 prompt 或 product information。
- `web_search` 必须有非空 query；`limit` 等范围由工具 Pydantic schema 校验。
- `product_search` 必须有 query 或 visual summary。
- `price_compare` 必须有 query 或 items。
- `memory_retrieval` 必须有 query，并且当前用户请求必须显式引用历史、上次/之前、已保存记忆、个人偏好或继续旧任务；query 本身不构成读取授权。
- legacy `memory` 只允许 `action=retrieve/save`，并复用 memory save 校验。
- `memory_save` 必须有 query、`content.text` 或 `content.summary`，且 assistant-loop 调用必须包含 `source_intent`、`source_reason`、`future_use`、`evidence`。
- `source_intent=user_confirmed` 保留给确认服务，assistant-loop 不得使用。
- `render_3d` 必须有明确 3D/render/modeling/scene-preview 意图。
- `delegate_to_agent` 必须有 `target_agent_id` 和 text/image/video/audio payload。
- 最后用目标工具的 Pydantic `input_schema` 做结构校验。

对已知工具，`ActionValidator` 会附带 prompt-safe `metadata.pre_tool_call` 摘要，包含工具名、运行时身份、副作用策略、确认需求、幂等 key 是否存在、输入规模摘要和 realtime task-state 摘要；它不包含原始 query/prompt、provider payload 或媒体内容。

被拒绝的 action 不会进入 `ToolExecutor`，不会产生 `ToolCallRecord`；assistant loop 会生成 `ToolObservation(status="rejected")`，记录 `action_rejected` trace，并由 loop guard 判断是否终止。

Phase 0 tool governance rejection tests live in `tests/test_phase0_tool_governance_contracts.py`.

## ToolExecutor 边界

`ToolExecutor.run_tool()` 是唯一工具执行入口。它负责：

- 对 memory 工具用 `AgentState.user_id/session_id` 覆盖模型参数。
- 检查 run-scoped cancel token；取消时跳过工具执行或停止 retry，并把 run 交回 runtime 标记为 `cancelled`。
- 创建 `ToolCallRecord` 并更新 `AgentState.status`。
- 发出带 `pre_tool_call` 摘要的 `tool_started` 事件；risk gate、幂等重复、预算阻断都进入同一生命周期，但不一定会调用 registry。
- 在 provider budget 和 registry 之前应用 runtime risk gate：read-only 自动放行，compensatable 使用 idempotency ledger，realtime 未分类 hard-gate 工具返回 pending confirmation。
- 在真正执行前调用 `ProviderCallBudget.check_before_call()`。
- 发出带 `post_tool_call` 摘要的 `tool_finished` / `tool_failed` 事件，覆盖成功、失败、取消、pending confirmation、duplicate suppression 和预算阻断。
- 写入 `ToolHistoryStore` 的 started/succeeded/failed 记录。
- 通过 `ProviderExecutionPolicy.retry` 对 retryable provider 错误重试。
- 把 registry/tool 异常转为失败 `ToolResult`，不让异常穿透给 Agent。
- `AgentRunCancelled` 是例外：它是运行时控制信号，会穿透 graph node 并由 `AgentGraphRuntime` 收口。
- 记录 provider budget call record。
- 用 `RecoveryPolicy` 决定失败后 stop、partial continue 或 optional step skip。
- 写入 trace event，失败时包含 sanitized error 和 input/output summary。

`post_tool_call` 只暴露 prompt-safe lifecycle 信息，例如 status、risk gate、side-effect summary、confirmation summary、idempotency summary、output_ref、latency、retry count 和压缩后的 observation summary。它不会携带 raw provider payload、secret-like error text 或大媒体内容。

只有 `ToolExecutor._run_once()` 可以直接调用 `registry.run(...)`。新增入口、API、MCP、graph node 或 service 不应直接调用 registry。

`ToolContext` 携带 `run_id/user_id/session_id`、运行时 metadata，以及可选的 `cancel_token`。工具若有自然轮询点，可以调用 `context.is_cancelled()` 提前返回；不要求现有同步工具在本阶段改成 async。

## ToolResult 和 observation

工具必须返回 `ToolResult`：

```text
tool_name
success
data
voice_summary
model_observation
trace_summary
audit_payload
raw_data_ref
error
output_ref
latency_ms
contract
```

实践规则：

- `data` 必须是结构化 JSON-compatible dict；不要只塞散乱字符串。
- `model_observation` 是 assistant-facing 结构化视图；存在时优先用于 `ToolObservation`，否则回退 `data`。
- `voice_summary` 是实时通话口播/恢复摘要；存在时优先用于 realtime side-effect snapshot。
- `trace_summary` 是 prompt-safe 调试摘要；存在时优先用于 post boundary、trace 和 tool history output summary。
- `audit_payload` 只保存显式提供的审计 payload，不从 raw provider response 自动派生。
- `raw_data_ref` 只能引用外部原始数据位置，不能进入 prompt、trace summary 或 observation。
- 推荐提供 `CapabilityOutputContract`，并在 `data["contract"]` 中保留 JSON 形式，便于 API/WebSocket/response composer 统一消费。
- `error` 应是可解释、已脱敏的错误信息。
- `output_ref` 用于引用生成物、搜索结果、记忆项、agent task 等 artifact。
- provider/model/cost 等可放在 `data` 或 contract metadata，让 provider budget 和 trace 摘要可记录。

`ToolObservation` 是 assistant-facing 摘要，不等同于原始 `ToolResult`：

- 成功 observation 包含 summary、output_ref、structured_output、next_step_hint。
- 失败或 rejected observation 包含 sanitized error_code/error_message 和 recovery hint。
- `web_search` 会保留 `title`、`url`、`snippet`、`published_at`、`source`，成功 observation 摘要首条结果和总数。
- 商品搜索/比价会保留 title、price、currency、URL、url_status 等后续回答和 price_compare 必需字段。
- context builder 会压缩大 observation、截断命令输出、移除 raw provider payload、base64、secret-like 内容。

## Loop Guard 和计划状态

assistant loop 有本地保护，不依赖模型自律：

- `MAX_TOOL_ITERATIONS` 默认 5；接近上限时会要求模型 final answer，不再继续工具调用。
- unknown tool、invalid input、missing required input、render intent 缺失等会触发 rejection guard。
- 同一工具失败达到阈值会停止重复调用。
- `image_generation` 和 `render_3d` 是 terminal tools；成功后再次请求同一工具会被阻止并转为 final answer。
- 明确比价请求中，`product_search` 成功后会强制或修复下一步 `price_compare`，防止只搜索不比价。

计划状态是本地治理结构，不是生产 runtime 的独立 planner/controller 调用：

- `enter_plan_mode` 的 `TaskPlan` 先经 `PlanValidator` 校验 step 数量、唯一 step_id、依赖、未知工具和环。
- active plan mode 下，tool call 必须匹配计划步骤和依赖。
- 工具失败后 plan mode 可进入 `replanning`。
- 真实非 mock runtime 不再要求 LLM 输出 `enter_plan_mode` / `exit_plan_mode` JSON；`execution_strategy=plan_and_solve` 会被接受为请求元数据，但仍走 native content/tool_calls 主循环。

## Memory tool 特殊规则

Memory 工具选择采用 LLM-first：

- 本地关键词和向量信号只记录 audit，不覆盖 LLM 是否调用 `memory_retrieval` / `memory_save`。
- `memory_retrieval` 和 legacy `memory action=retrieve` 即使由 LLM 选择，也必须先通过 `ActionValidator -> MemoryReadPolicy` 的读取意图 gate。
- `memory_retrieval` 成功结果返回 `memory_context`、`items`、`total`、`trust_policy` 和 `usage_hint`；这些字段声明 memory 是用户历史证据，不是权威或系统指令。
- `memory_save` 必须声明 `source_intent`、`source_reason`、`future_use`、`evidence`。
- `source_intent=user_explicit` 只用于用户明确要求保存/以后使用。
- `source_intent=assistant_candidate` 只记录候选，不直接写长期记忆。
- `source_intent=user_confirmed` 只能由确认服务使用，assistant-loop 调用会被 validator 拒绝。
- `ToolExecutor` 和 `MemoryTool` 都会用运行时 `ToolContext` 绑定身份，模型传入的 user/session 不作为记忆归属。

记忆服务内部检索、写入策略、确认、TTL、审计和 store 选择以 `docs/memory-service-architecture.md` 为准。

Memory Server media ingestion uses separate tools:

- `memory_media_ingest` submits safe media file references to the configured external Memory Server ingestion API. It is not `memory_save`, does not accept raw media/base64 payloads, and must bind user/session identity from `ToolContext`.
- `memory_ingest_status` reads an ingestion task status and surfaces the external service's current weak user-scope warning.
- `ActionValidator` only accepts `memory_media_ingest` when the user request explicitly asks to upload/import media into memory; ordinary image/video analysis should use `vision_understanding` or `video_understanding`.
- Default local/mock runs register these tools but keep them unconfigured unless `dual_core` / `hybrid_remote` plus a Memory Server URL is explicitly enabled.

## MCP 和外部入口

`OfflineMCPServer` 提供四个 MCP wrapper 工具：

- `agent_run`: 走 `AgentGraphRuntime`。
- `tool_list`: 返回 MCP wrapper 列表和 registry ToolSpec。
- `tool_run`: 构造 `AssistantDecision(type="tool_call")`，先过 `ActionValidator`，再调用 `ToolExecutor`。
- `demo_flow_run`: 走离线 demo flow。

MCP server 不直接依赖 provider SDK，不直接访问 OpenAI/DashScope/httpx/requests。错误 envelope 会脱敏。新增外部入口必须遵守同样边界：先归一成内部 request/decision，再走 validator/executor。

## 新增或修改工具清单

新增工具时按这个顺序做：

1. 定义 Pydantic input/output schema。通用 schema 放 `schemas/`，工具私有 schema 可放工具模块。
2. 实现 `MockTool` 或满足 `BaseTool` 协议：`name`、`description`、`input_schema`、`output_schema`、`run/_run`。
3. 真实能力先建 service/provider adapter interface 和 mock/local implementation；工具只调用 adapter/service。
4. 返回结构化 `ToolResult`，失败也要返回可解释错误和可选 contract，不抛未处理异常。
5. 在 `ToolRegistry.create_default_registry()` 注册。默认注册只放 mock/local/offline 安全工具；高风险或跨 agent 工具用显式开关。
6. 在 `_ACTION_USAGE` 增加 `when_to_use`、`when_not_to_use`、`runtime_constraints`。
7. 如有语义必需参数或安全条件，在 `ActionValidator` 增加执行前校验。
8. 如旧 mock/rule plan 需要支持，在 `tool_input_builder.py` 增加 action 到 tool input 的兼容构造。
9. 如 observation 后续会驱动另一个工具，更新 `tool_observation.py` 的 summary/next_step_hint/保留字段。
10. 如涉及 provider-native 或 MCP schema，补充 `tool_spec_adapters` 相关测试。
11. 如涉及 memory 或 agent delegation，先按对应权威文档确认服务边界。

最小测试覆盖：

- registry spec 暴露和 schema 转换。
- ActionValidator 接受合法输入、拒绝缺参/未知工具/危险输入。
- ToolExecutor 成功、失败、预算阻断、retry/recovery 和 cooperative cancellation。
- native direct-answer 路径必须只有一次 chat call，且首个用户可见 delta 来自 provider content。
- native tool 路径第一轮必须是 provider `tool_calls`，工具执行后第二轮生成自然语言回答。
- provider 不支持 native tools 时必须 fail immediately，不能回退到 JSON 控制面。
- observation 和 trace/event 中不出现 raw provider payload、API key、Authorization、Bearer token。
- 若工具可默认注册，离线 pytest 和 demo flow 不能依赖真实 provider。

## 当前验证入口

优先跑这些离线测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_tool_executor.py \
  tests/test_provider_budget_in_tool_executor.py \
  tests/test_retry_policy.py \
  tests/test_phase8a1_react_action_quality.py \
  tests/test_native_tool_call_handoff.py \
  tests/test_assistant_context_renderer.py \
  tests/unit/test_tool_spec_adapters.py \
  tests/test_mcp_server_skeleton.py \
  tests/test_architecture_boundaries.py
```

更大范围验收：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

## 不要做

- 不要让 API、WebSocket、MCP、demo 或 eval 直接调用 provider SDK 或 `registry.run(...)`。
- 不要只靠 prompt 约束防止危险工具调用；必须在 `ActionValidator` 或 service policy 层落地。
- 不要把 model-provided user_id/session_id 当作身份来源。
- 不要在工具里写入 API key、raw provider response、真实用户数据 dump 或大媒体 payload。
- 不要把 mock/offline 行为伪装成真实 LLM/provider 能力。
- 不要把 `docs/development/**` 当作当前 tool calling 设计权威。
