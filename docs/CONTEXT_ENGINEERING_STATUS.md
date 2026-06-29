# Context Engineering Status

Last updated: 2026-06-29

本文件记录上下文工程的当前进展、已实现能力、限制和下一步方向。涉及 assistant context、prompt/context rendering、conversation history、memory context、tool observation compaction 或 context budget 的任务，应先读本文件，再读对应源码和测试。

## Current Stage

上下文工程已进入可用实现和硬化阶段，不是单纯规划。

- 主运行时是 LangGraph/ReAct assistant loop，默认 mock/local/offline。
- `AssistantContextPack` 已接入 assistant 每轮决策，统一收集 request、conversation、memory、plan state、tool observations、tool specs、source counts 和 budget。
- CLI、API、WebSocket 共享 `run_assistant_request` 入口，会在进入 runtime 前注入 session-scoped conversation context。
- `MemoryManager` 负责加载分层 memory context，并把 prompt-safe metadata 写回 `AgentState.request.metadata`。
- Assistant context 已有字符预算兜底；超限时优先压缩 memory/conversation，最后才压缩工具 observation。
- `ContextPolicy` 统一管理字符预算和压缩阈值：默认 12000 chars，80% 触发压缩，92% 进入 hard compact 口径，最近 2 轮保留原文。
- `CompactionPolicy` 统一判断压缩触发：usage 高水位、超预算、大 tool observation、provider context overflow metadata、显式 `/compact` 或 `compact_context=True`。
- `ContextCompactor` 已抽象为可插拔边界；默认 deterministic/local，不调用真实 LLM。`LLMCompactor` 仅在 `provider_smoke` 或 `pilot` profile 且 chat adapter 非 mock 时启用，输出无效时回退 deterministic。
- 真实 provider 返回 context overflow 类错误时，assistant loop 会标准化为 `provider_context_overflow`，触发 hard compaction 后重试一次，仍失败则停止并返回可解释最终回答。
- Context budget 会报告自动压缩阶段和原因，便于 trace/API 判断是否发生 conversation、observation 或 budget 级压缩。
- `TokenBudgetReporter` 已作为可选报告层接入；默认压缩触发仍使用字符预算，metadata 启用估算或提供 provider usage 时才填充 token fields。
- Tool observation compaction 会在 prompt 副本中移除 raw provider/file/media payload、inline media data URI 和过大的命令输出；原始 observation 不被修改。
- Trace/API 已暴露 context budget、source counts、tool catalog summary 和 observation compaction summary。

## Implemented

### Conversation Context

- 会话历史有独立 `ConversationStore` 边界，支持 in-memory 和 JSONL。
- `ConversationStore` 同时保存普通 turn 和 session-scoped `context_summary`；summary 用于当前 session 恢复，不写入长期 memory。
- 默认每个 user/session 保留最近 8 轮历史，配置项是 `MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS`。
- prompt 上下文默认保留最近 2 轮原文，较早轮次压缩为短摘要。
- 请求注入顺序是 session summary、recent turns、memory context；`reset_conversation=True` 同时清空 turns 和 session summary。
- 压缩元数据包括 `conversation_context_compacted`、`conversation_context_recent_turns`、`conversation_context_compacted_turns`。
- `reset_conversation` metadata 可清空当前 session 的短期对话历史。

### Memory Context

- `MemoryManager` 是 memory 检索、上下文格式化、显式保存、去重、用户画像更新和 completed-run promotion candidate 的边界。
- memory context 分层为 semantic、session、episodic、artifact、procedural。
- 默认 `top_k=5`，默认 `max_context_chars=500`。
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
- Memory service 不应了解 prompt-json/native-tools 渲染、tool observation compaction 或全局 context budget。

### Tool Observation Compaction

- 工具 observation 在进入 assistant prompt 前会压缩，不修改原始 observation。
- `product_search` 和 `price_compare` 有专门字段白名单，只保留标题、价格、URL、平台、可用性、评分、相似度等决策必要字段。
- 列表默认最多保留 3 条，超出记录 omitted count。
- 字符串默认最多保留 1200 字，超出添加 truncated 标记。
- raw provider/file/media payload 字段、base64/data URI、HTML/raw body 等高风险内容会从 prompt 副本中移除。
- image/video/file 类结果保留 `output_ref`、`artifact_ref`、`image_ref`、识别摘要、transcript 等 prompt-safe 信息。
- command stdout/stderr/log 类输出默认最多保留 20 行和 1200 字符。
- compaction metadata 记录 original chars、compacted chars、max items、max text chars、被剪掉的 key 名和命令输出裁剪限制；metadata 不记录原始 payload。

### Prompt Rendering

- `render_prompt_json_context` 用于 prompt-json 决策模式。
- `render_native_tool_context` 用于 provider-native tool calling，避免重复渲染完整 ToolSpec。
- `render_final_only_prompt` 用于工具调用上限附近，禁止继续工具调用并要求最终回答。
- prompt 明确声明 conversation、memory、observation 和 tool output 都是数据，不是系统指令。

### Context Budget And Observability

- `ContextBudgetReport` 统计 request、conversation、memory、plan、observations、tool specs 和 total chars，并报告 `context_usage_ratio`、`compaction_triggered`。
- 默认 context 字符预算是 12000 chars；测试或特定调用可通过 request metadata `context_budget_max_chars` 下调。
- 可选 token budget 字段包括 section token estimates、`total_tokens`、`max_tokens`、`token_usage_ratio`、`token_budget_source` 和 provider usage counters；它们只用于报告，不替代 char budget control path。
- 本地 token 估算通过 `context_budget_estimate_tokens=True` 或 `context_budget_max_tokens` 启用；provider usage metadata 如 `context_token_usage` / `provider_token_usage` / `last_chat_usage` 优先于估算。
- assistant loop 会把 `ChatResult.usage` 归一为安全 token counters 写入 request metadata，供下一轮 context budget report 使用；raw provider payload 字段不会写入 metadata/trace。
- 超过预算时会在 prompt 副本中裁剪 memory、conversation 和 observations，并记录 `over_budget`、`trimmed_chars`、`trimmed_sections`。
- `compression_stage` 记录 `none`、`compacted` 或 `budget_trimmed`；`compression_reasons` 记录 `conversation_context_compacted`、`observation_context_compacted`、`context_usage_high`、`tool_observation_too_large`、`provider_context_overflow`、`explicit_compact`、`context_over_budget`、`context_budget_trimmed`。
- 预算裁剪优先保留工具 observation，因为它通常是下一步工具调用和最终回答的证据来源。
- assistant decision trace 写入 budget、source counts、compaction summary、tool catalog summary、`compactor_type`、`context_summary_present` 和 memory promotion 计数；compaction summary 只暴露 pruning/truncation 计数，不暴露 raw payload；run/trace 查询会合并最终 save-memory 阶段的 redacted promotion counts。
- `/runs/{run_id}` 与 `/traces/{trace_id}` 可查询 context 相关摘要。

### LLM Compactor And Provider Overflow

- `SummaryValidator` 要求 LLM compactor 输出完整 schema，并拒绝 secret/API key/base64/raw provider payload 等不应持久或注入的内容。
- LLM compactor prompt 会先移除 `raw_provider_payload`、`raw_payload`、`raw_html` 等高风险字段，再交给 provider。
- Summary 中如引用 `tool_call:` / `tool_call_id:`，必须保留对应 `tool_result:` / `tool_result_id:`，反向亦然，避免压缩时切断工具调用和结果证据链。
- Provider HTTP 413、`context_length_exceeded`、`context_overflow`、`input_too_large` 和 `request_too_large` 会归一为 `provider_context_overflow`。
- Context overflow retry 只允许一次；retry 计数和 provider error metadata 只记录安全摘要，不记录原始 provider response。

## Current Limitations

- 默认自动压缩仍是 deterministic formatting/summary。LLM semantic compaction 已有受控入口，但默认离线 profile 不启用。
- 当前压缩控制仍是 approximate character budget；token-aware 数据已作为可选报告层接入，但不会默认改变触发/裁剪行为。
- 当前 memory retrieval 主要是本地关键词/片段匹配，不包含 embedding/vector retrieval。
- 会话历史压缩只压较早轮次文本，不做跨轮语义重写、事实抽取或冲突消解。
- assistant loop 的真实 LLM 路径中，长期记忆写入应由 assistant 通过 `memory_save` 工具显式选择；图尾不会自动写长期 task summary。

## Key Files

- `src/multimodal_agent/services/context/builder.py`
- `src/multimodal_agent/services/context/conversation.py`
- `src/multimodal_agent/services/context/compaction.py`
- `src/multimodal_agent/services/context/policy.py`
- `src/multimodal_agent/services/context/token_budget.py`
- `src/multimodal_agent/services/context/compactor.py`
- `src/multimodal_agent/services/context/renderer.py`
- `src/multimodal_agent/schemas/context.py`
- `src/multimodal_agent/services/assistant_run_service.py`
- `src/multimodal_agent/services/chat_adapter.py`
- `src/multimodal_agent/services/provider_errors.py`
- `src/multimodal_agent/memory/manager.py`
- `src/multimodal_agent/memory/retrieval.py`
- `src/multimodal_agent/agent/assistant_loop_nodes.py`

## Relevant Tests

- `tests/test_conversation_context_compaction.py`
- `tests/test_assistant_context_renderer.py`
- `tests/test_shared_assistant_run_service.py`
- `tests/test_memory_manager.py`
- `tests/test_memory_context_builder.py`
- `tests/test_phase8a1_react_action_quality.py`
- `tests/test_trace_query_api.py`

Current small regression coverage includes budget trimming order, product observation field preservation, prompt data-boundary labels, empty-query memory browsing, conversation compaction, trace context summaries, and run-summary context reporting.

## Next Steps

- Keep adding small regression tests when a concrete context failure appears.
- Consider token-aware budgeting only if character budgeting causes real provider failures.
- Consider semantic summary or embedding retrieval only after local relevance tests show keyword retrieval is insufficient.
