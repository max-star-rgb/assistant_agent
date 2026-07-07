# Context Engineering Status

Last updated: 2026-07-07

本文件记录上下文工程的当前进展、已实现能力、限制和下一步方向。涉及 assistant context、prompt/context rendering、conversation history、memory context、tool observation compaction 或 context budget 的任务，应先读本文件顶部快速交接，再读对应小节、源码和测试。

## 新对话快速交接

如果新对话涉及上下文工程，先读本节即可快速接上当前状态。

- 当前结论：上下文工程第一版已经可用并适合阶段性收口，不是缺核心组件的状态。
- 当前权威入口：本文件。不要把 `docs/development/context-engine-memory-policy-plan.md` 当成新的 active roadmap。
- 已实现核心闭环：`AssistantContextPack`、session summary、增量滑动窗口摘要、realtime task-state snapshot、reusable task artifacts、side-effect records、realtime call-state snapshot、规则触发压缩、tool observation prompt 副本裁剪、字符预算控制、token 报告、provider overflow retry-once、trace/API 上下文摘要、skill-style capability catalog 和 repo-local `skills/<skill_id>/SKILL.md` capability loader。
- 默认摘要方式：deterministic/local；`LLMCompactor` 只在 `provider_smoke` 或 `pilot` 且非 mock chat adapter 下启用。
- 预算现状：全局压缩控制仍以字符预算为准；token-aware 目前是报告层。Memory context 有单独 token-aware 注入边界。
- memory 边界：`context_summary` 是当前 session 状态，不是长期 memory；长期写入仍由 `MemoryManager` / `MemoryWritePolicy` 管。
- 当前不建议继续做：场景分类器、质量反馈自动调参、组件注册器、裁剪 undo 日志、默认 LLM 摘要、全局 token 强控制。
- 如果用户问“继续上下文工程”：优先做验收案例、调试说明、具体失败复现和小回归测试；不要默认新增复杂架构。
- 按需补读：给人解释机制时读 `docs/context-engineering-walkthrough.md`；涉及长期记忆写入/检索时读 `docs/memory-service-architecture.md`；追溯阶段决策时读 `docs/development/context-engine-memory-policy-plan.md`。

## Current Stage

上下文工程已进入可用实现和硬化阶段，不是单纯规划。

- 多阶段 Context Engine + Memory Policy 计划已经完成；后续应把 `docs/CONTEXT_ENGINEERING_STATUS.md` 作为当前入口，把 `docs/development/context-engine-memory-policy-plan.md` 作为执行记录和参考。
- 主运行时是 LangGraph/ReAct assistant loop，默认 mock/local/offline。
- `AssistantContextPack` 已接入 assistant 每轮决策，统一收集 request、conversation、memory、plan state、tool observations、tool specs、source counts 和 budget。
- `AssistantContextPack` 会按已选 prompt tools 注入一个小型 skill-style capability catalog；它可从 repo-local `skills/<skill_id>/SKILL.md` 加载 prompt-safe descriptor，但只描述何时使用现有受治理工具，不是新的执行路径，也不会读取 `.codex/skills`。
- CLI、API、WebSocket 共享 `run_assistant_request` 入口，会在进入 runtime 前注入 session-scoped conversation context。
- Gateway/realtime 请求会在进入 runtime 前注入 session-scoped realtime task-state snapshot；普通 `/agent/run` 不自动启用，除非 metadata 显式打开。
- `MemoryManager` 负责加载分层 memory context，并把 prompt-safe metadata 写回 `AgentState.request.metadata`。
- Assistant context 已有字符预算兜底；超限时优先压缩 memory/conversation，最后才压缩工具 observation。
- `ContextPolicy` 统一管理字符预算和压缩阈值：默认 12000 chars，80% 触发压缩，92% 进入 hard compact 口径，最近 2 轮保留原文。
- `CompactionPolicy` 统一判断压缩触发：usage 高水位、超预算、大 tool observation、provider context overflow metadata、显式 `/compact` 或 `compact_context=True`。
- `ContextCompactor` 已抽象为可插拔边界；默认 deterministic/local，不调用真实 LLM。`LLMCompactor` 仅在 `provider_smoke` 或 `pilot` profile 且 chat adapter 非 mock 时启用，输出无效时回退 deterministic。
- 真实 provider 返回 context overflow 类错误时，assistant loop 会标准化为 `provider_context_overflow`，触发 hard compaction 后重试一次，仍失败则停止并返回可解释最终回答。
- Context budget 会报告自动压缩阶段和原因，便于 trace/API 判断是否发生 conversation、observation 或 budget 级压缩。
- `TokenBudgetReporter` 已作为可选报告层接入；默认压缩触发仍使用字符预算，metadata 启用估算或提供 provider usage 时才填充 token fields。
- Memory context now has a separate `MemoryContextBuilder` token-aware injection boundary. It can enforce memory-only token budgets and report injected IDs, token count, omitted count, rejection reasons, and retrieval version without changing global assistant context compaction.
- Tool observation compaction 会在 prompt 副本中移除 raw provider/file/media payload、inline media data URI 和过大的命令输出；原始 observation 不被修改。
- Cross-agent delegation now has a separate child-context boundary in `AgentCommunicationService`: child runs receive explicit `context_refs`, child budget metadata, and redacted audit summaries, not parent history, `memory_context_*`, raw provider payloads, secrets, or raw tool results.
- Trace/API 已暴露 versioned context debug summary，包括 context budget、source counts、tool catalog summary、observation compaction summary 和 memory promotion counters。
- Context build now also emits canonical `context.build.started` and
  `context.build.finished` trace events with redacted budget, source-count,
  compaction, and tool-catalog summaries.

## Implemented

### Conversation Context

- 会话历史有独立 `ConversationStore` 边界，支持 in-memory 和 JSONL。
- `ConversationStore` 同时保存普通 turn 和 session-scoped `context_summary`；summary 用于当前 session 恢复，不写入长期 memory。
- 默认每个 user/session 保留最近 8 轮历史，配置项是 `MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS`。
- prompt 上下文默认保留最近 2 轮原文，较早轮次进入 session summary。
- 会话摘要采用增量滑动窗口：每轮进入 runtime 前，只把新滑出最近窗口且尚未在 summary refs 中出现的 turn 合并进 `context_summary`，避免重复压缩同一 turn。
- 请求注入顺序是 session summary、recent turns、memory context；`reset_conversation=True` 同时清空 turns 和 session summary。
- 压缩元数据包括 `conversation_context_compacted`、`conversation_context_recent_turns`、`conversation_context_compacted_turns`。
- `reset_conversation` metadata 可清空当前 session 的短期对话历史。

### Memory Context

- `MemoryManager` 是 memory 检索、上下文格式化、显式保存、去重、用户画像更新和 completed-run promotion candidate 的边界。
- memory context 分层为 semantic、session、episodic、artifact、procedural。
- 默认 `top_k=5`，默认 `max_context_chars=500`。
- `MemoryContextBuilder` 负责实际注入选择；`MemoryContext.items` 表示已注入的 memory 子集，而不是所有检索候选。
- 可通过 `memory_context_max_tokens` / `memory_context_budget_tokens` 或 `MemoryManager` 参数限制 memory context token budget。
- memory context metadata includes `memory_context_tokens`, `memory_context_budget_tokens`, `memory_context_omitted_count`, `memory_context_rejected_reasons`, `memory_context_retrieval_version`, and `memory_context_injected_ids`.
- 非空 query 走关键词/中文片段相关性门控；只有明确承接型 query 才允许 recent memory fallback。
- 显式用户记忆会合并重复项，并更新 compact `user_profile` 记忆。
- completed-run summary 默认只生成 policy-gated promotion candidate 和审计 metadata，不自动写长期 memory；`allow_auto_write=True` 时才会落库。

### Boundary With Memory Service

上下文工程消费 memory service 产出的 prompt-safe memory context，但不拥有 memory 行为。

- Memory service 负责 memory item 的存储、检索、排序、分层、写入、去重、用户画像、TTL、审计和删除。
- `MemoryManager` 可以把检索结果格式化为 `MemoryContext`，并写入 `request.metadata["memory_context_*"]`。
- Context engineering 负责把 request、conversation、memory context、plan state、tool observations 和 tool specs 组装成 `AssistantContextPack`。
- Context engineering 负责 prompt/native rendering、tool observation compaction、全局 context budget、source counts 和 trace/debug 摘要。
- Context engineering 不应重新实现 memory 检索、ranking、fallback、write policy、profile merge 或 store 选择。
- Memory service 不应了解 legacy prompt-json/native-tools 渲染、tool observation compaction 或全局 context budget。

### Tool Observation Compaction

- 工具 observation 在进入 assistant prompt 前会压缩，不修改原始 observation。
- `product_search` 和 `price_compare` 有专门字段白名单，只保留标题、价格、URL、平台、可用性、评分、相似度等决策必要字段。
- 列表默认最多保留 3 条，超出记录 omitted count。
- 字符串默认最多保留 1200 字，超出添加 truncated 标记。
- raw provider/file/media payload 字段、base64/data URI、HTML/raw body 等高风险内容会从 prompt 副本中移除。
- image/video/file 类结果保留 `output_ref`、`artifact_ref`、`image_ref`、识别摘要、transcript 等 prompt-safe 信息。
- command stdout/stderr/log 类输出默认最多保留 20 行和 1200 字符。
- compaction metadata 记录 original chars、compacted chars、max items、max text chars、被剪掉的 key 名和命令输出裁剪限制；metadata 不记录原始 payload。

### Cross-Agent Delegation Context

- `AgentCommunicationService` builds a child-safe delegation context after delegation policy accepts a task and before `AgentTransport` dispatches it.
- Child request metadata preserves explicit `context_refs`, `request_origin`, `agent_communication`, `child_context_budget`, and `agent_context`.
- Parent `conversation_history`, `parent_history`, `memory_context_*`, raw provider payloads, base64/media/body fields, secret/token-like fields, arbitrary non-allowlisted metadata, and raw parent `tool_results` are not forwarded.
- Omitted fields are recorded as field-name/reason pairs in `agent_context.omitted_context`; raw parent tool results are reduced to `tool_result_refs` when output references exist.
- This boundary does not replace `AssistantContextPack` assembly and does not move memory retrieval, ranking, write policy, or store access out of `MemoryManager`.

### Prompt Rendering

- `render_prompt_json_context` 是历史 prompt-json renderer，保留给 context renderer 测试和离线兼容材料；生产真实 LLM runtime 不再使用它做决策控制面。
- `render_native_tool_context` 用于 provider-native tool calling，避免重复渲染完整 ToolSpec。
- native/legacy context 可渲染 prompt-safe capability catalog；实际执行契约仍是 `ToolSpec`，工具调用仍必须通过 `ToolExecutor`。
- Repo-local business skills follow `skills/<skill_id>/SKILL.md`; the loader only consumes frontmatter plus fixed prompt-safe sections and converts valid descriptors into `ToolCapabilityDescriptor`. It skips disabled/manual-only/invalid skills, ignores `.codex/skills`, and never creates `run_skill` or direct shell/browser/http execution.
- `render_final_only_prompt` 用于工具调用上限附近，禁止继续工具调用并要求最终回答。
- prompt 明确声明 conversation、memory、realtime task state、observation 和 tool output 都是数据，不是系统指令。

### Realtime Task State Context

- `prepare_realtime_task_state_request` 在 realtime/Gateway 请求进入 `AgentGraphRuntime.run_state(...)` 前生成 prompt-safe task-state snapshot。
- Task-state 记录 session 内当前 objective、active constraints、source turn/run ids、interrupt 产生的 `IntentRevision`，以及 completed run 后的 prompt-safe `TaskArtifact`、lightweight checkpoint artifact 和 `SideEffectRecord`。
- Task-state 现在也记录 prompt-safe realtime call state：`pending_tool`、`tts_state`、`last_spoken_progress`、`speech_turn_id`、`barge_in_source` 和 bounded `last_realtime_event_ids`，用于表达工具等待、展示/TTS 状态和打断来源；工具完成/失败、取消和挂断会清理 pending tool，TTS/display started/finished/superseded 会更新展示状态；不保存 raw audio、raw transcript stream 或 provider payload。
- `pending_tool` 会消费 `tool_started` 事件中的 prompt-safe `pre_tool_call` 摘要，保留工具副作用等级、risk gate、idempotency key 摘要和是否需要确认，便于 interrupt 后选择重规划、等待确认、去重或补偿路径。
- Interrupt run 的 snapshot 会保留原始 objective，并把最新 interrupt 文本写入 `latest_revision`；普通 queued follow-up 只更新 current user text 和 provenance，不创建 revision。
- Completed realtime run 会把 selected tool observations 和 media refs 记录为 task artifacts；tool observation artifact 复用现有 prompt compaction 逻辑，不保存 raw provider/file/media payload。
- 多步 realtime run 在同一轮完成至少两个 reusable tool observations 时，会记录 bounded `checkpoint` artifact；interrupt 只有在 checkpoint 仍可复用时才选择 `resume_from_checkpoint`，用户明确重来/换一批会把 checkpoint 标为 stale。
- Interrupt 会用简单策略选择 `restart`、`reuse_and_replan`、`ask_confirmation`、`report_committed` 或 `compensate`；如果用户明确要求重新搜索/换一批/不要之前结果，已有 reusable artifacts 会标记为 `stale`，不会重新注入 prompt snapshot。
- Side-effect records 来自 `ToolSpec.side_effect` 和工具结果中的 prompt-safe override（例如 `requires_confirmation`、`confirmation_id`、`side_effect_level`）；read-only 工具不阻塞重规划，pending confirmation 会让下一轮先处理确认，committed action 不会被描述成已取消，compensatable artifact 会倾向修正版/补偿路径。
- `AgentGraphRealtimeBackend` 和 shared run service 会发 display-only `run.progress`，用于 App + Media 展示 `task_state/revising`、strategy、reusable artifact count 和 side-effect count。
- 当前已接入 process-local risk gate/idempotency ledger：read-only 工具无额外开销，compensatable 工具可去重，realtime 未分类 hard-gate 工具会返回 pending confirmation。完整用户确认 UX、持久化 ledger 和跨进程恢复仍未接入。

### Context Budget And Observability

- `ContextBudgetReport` 统计 request、conversation、memory、plan、observations、tool specs 和 total chars，并报告 `context_usage_ratio`、`compaction_triggered`。
- `ContextBudgetReport` also tracks `tool_capability_chars` so the skill-style capability catalog is visible in budget/debug output.
- 默认 context 字符预算是 12000 chars；测试或特定调用可通过 request metadata `context_budget_max_chars` 下调。
- 可选 token budget 字段包括 section token estimates、`total_tokens`、`max_tokens`、`token_usage_ratio`、`token_budget_source` 和 provider usage counters；它们只用于报告，不替代 char budget control path。
- 本地 token 估算通过 `context_budget_estimate_tokens=True` 或 `context_budget_max_tokens` 启用；provider usage metadata 如 `context_token_usage` / `provider_token_usage` / `last_chat_usage` 优先于估算。
- assistant loop 会把 `ChatResult.usage` 归一为安全 token counters 写入 request metadata，供下一轮 context budget report 使用；raw provider payload 字段不会写入 metadata/trace。
- 超过预算时会在 prompt 副本中裁剪 memory、conversation 和 observations，并记录 `over_budget`、`trimmed_chars`、`trimmed_sections`。
- `compression_stage` 记录 `none`、`compacted` 或 `budget_trimmed`；`compression_reasons` 记录 `conversation_context_compacted`、`observation_context_compacted`、`context_usage_high`、`tool_observation_too_large`、`provider_context_overflow`、`explicit_compact`、`context_over_budget`、`context_budget_trimmed`。
- 预算裁剪优先保留工具 observation，因为它通常是下一步工具调用和最终回答的证据来源。
- assistant decision trace 写入 `context_schema_version="context_observability_v1"`、budget、source counts、compaction summary、tool catalog summary、`compactor_type`、`context_summary_present` 和 memory promotion 计数；compaction summary 只暴露 pruning/truncation 计数，不暴露 raw payload；run/trace 查询会合并最终 save-memory 阶段的 redacted promotion counts。
- Context pack construction emits standalone `context.build.started` /
  `context.build.finished` canonical trace events. The finished event carries the
  same redacted context summary shape used by trace/API context debugging.
- Trace sanitization 会过滤 `raw_provider_payload`、`raw_provider_response`、base64/media/file payload key 和 secret key，作为 public API 前的额外防线。
- `/runs/{run_id}` 与 `/traces/{trace_id}` 可查询 context 相关摘要。

### LLM Compactor And Provider Overflow

- `SummaryValidator` 要求 LLM compactor 输出完整 schema，并拒绝 secret/API key/base64/raw provider payload 等不应持久或注入的内容。
- LLM compactor prompt 会先移除 `raw_provider_payload`、`raw_payload`、`raw_html` 等高风险字段，再交给 provider。
- Summary 中如引用 `tool_call:` / `tool_call_id:`，必须保留对应 `tool_result:` / `tool_result_id:`，反向亦然，避免压缩时切断工具调用和结果证据链。
- Provider HTTP 413、`context_length_exceeded`、`context_overflow`、`input_too_large` 和 `request_too_large` 会归一为 `provider_context_overflow`。
- Context overflow retry 只允许一次；retry 计数和 provider error metadata 只记录安全摘要，不记录原始 provider response。

## Current Limitations

- 默认自动压缩仍是 deterministic formatting/summary。LLM semantic compaction 已有受控入口，但默认离线 profile 不启用。
- 当前全局压缩控制仍是 approximate character budget；token-aware 数据已作为可选报告层接入。Memory context 可单独按 token budget 控制注入，但这不替代 AssistantContextPack 的全局字符预算。
- 当前 memory retrieval 主要是本地关键词/片段匹配，不包含 embedding/vector retrieval。
- 会话历史压缩只增量合并滑出窗口的较早轮次，不做跨轮语义重写、事实抽取、冲突消解或质量反馈调参。
- assistant loop 的真实 LLM 路径中，长期记忆写入应由 assistant 通过 `memory_save` 工具显式选择；图尾不会自动写长期 task summary。

## Key Files

- `src/assistant_agent/services/context/builder.py`
- `src/assistant_agent/services/context/conversation.py`
- `src/assistant_agent/services/context/compaction.py`
- `src/assistant_agent/services/context/policy.py`
- `src/assistant_agent/services/context/token_budget.py`
- `src/assistant_agent/services/context/compactor.py`
- `src/assistant_agent/services/context/renderer.py`
- `src/assistant_agent/services/context/capability_catalog.py`
- `src/assistant_agent/services/context/skill_loader.py`
- `src/assistant_agent/services/realtime_task_state.py`
- `src/assistant_agent/services/agent_delegation_context.py`
- `src/assistant_agent/schemas/context.py`
- `src/assistant_agent/services/assistant_run_service.py`
- `src/assistant_agent/services/chat_adapter.py`
- `src/assistant_agent/services/provider_errors.py`
- `src/assistant_agent/memory/context_builder.py`
- `src/assistant_agent/memory/manager.py`
- `src/assistant_agent/memory/retrieval.py`
- `src/assistant_agent/agent/assistant_loop_nodes.py`

## Relevant Tests

- `tests/test_conversation_context_compaction.py`
- `tests/test_assistant_context_renderer.py`
- `tests/test_realtime_task_state.py`
- `tests/test_shared_assistant_run_service.py`
- `tests/test_memory_manager.py`
- `tests/test_memory_context_builder.py`
- `tests/test_agent_communication_routing.py`
- `tests/test_phase8a1_react_action_quality.py`
- `tests/test_trace_query_api.py`

Current small regression coverage includes budget trimming order, product observation field preservation, prompt data-boundary labels, empty-query memory browsing, conversation compaction, trace context summaries, and run-summary context reporting.

## Next Steps

- Keep adding small regression tests when a concrete context failure appears.
- Use `docs/development/context-engine-memory-policy-plan.md` as a completed implementation log, not an active roadmap.
- Consider token-aware control decisions only if reporting-only token fields show real provider failures that character budgeting cannot prevent.
- Consider semantic summary or embedding retrieval only after local relevance tests show keyword retrieval is insufficient.
