# Context Engineering Status

Last updated: 2026-06-27

本文件记录上下文工程的当前进展、已实现能力、限制和下一步方向。涉及 assistant context、prompt/context rendering、conversation history、memory context、tool observation compaction 或 context budget 的任务，应先读本文件，再读对应源码和测试。

## Current Stage

上下文工程已进入可用实现和硬化阶段，不是单纯规划。

- 主运行时是 LangGraph/ReAct assistant loop，默认 mock/local/offline。
- `AssistantContextPack` 已接入 assistant 每轮决策，统一收集 request、conversation、memory、plan state、tool observations、tool specs、source counts 和 budget。
- CLI、API、WebSocket 共享 `run_assistant_request` 入口，会在进入 runtime 前注入 session-scoped conversation context。
- `MemoryManager` 负责加载分层 memory context，并把 prompt-safe metadata 写回 `AgentState.request.metadata`。
- Trace/API 已暴露 context budget、source counts、tool catalog summary 和 observation compaction summary。

## Implemented

### Conversation Context

- 会话历史有独立 `ConversationStore` 边界，支持 in-memory 和 JSONL。
- 默认每个 user/session 保留最近 8 轮历史，配置项是 `MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS`。
- prompt 上下文默认保留最近 2 轮原文，较早轮次压缩为短摘要。
- 压缩元数据包括 `conversation_context_compacted`、`conversation_context_recent_turns`、`conversation_context_compacted_turns`。
- `reset_conversation` metadata 可清空当前 session 的短期对话历史。

### Memory Context

- `MemoryManager` 是 memory 检索、上下文格式化、显式保存、去重、用户画像更新和 completed-run summary 的边界。
- memory context 分层为 semantic、session、episodic、artifact、procedural。
- 默认 `top_k=5`，默认 `max_context_chars=500`。
- 非空 query 走关键词/中文片段相关性门控；只有明确承接型 query 才允许 recent memory fallback。
- 显式用户记忆会合并重复项，并更新 compact `user_profile` 记忆。

### Tool Observation Compaction

- 工具 observation 在进入 assistant prompt 前会压缩，不修改原始 observation。
- `product_search` 和 `price_compare` 有专门字段白名单，只保留标题、价格、URL、平台、可用性、评分、相似度等决策必要字段。
- 列表默认最多保留 3 条，超出记录 omitted count。
- 字符串默认最多保留 800 字，超出添加 truncated 标记。
- compaction metadata 记录 original chars、compacted chars、max items 和 max text chars。

### Prompt Rendering

- `render_prompt_json_context` 用于 prompt-json 决策模式。
- `render_native_tool_context` 用于 provider-native tool calling，避免重复渲染完整 ToolSpec。
- `render_final_only_prompt` 用于工具调用上限附近，禁止继续工具调用并要求最终回答。
- prompt 明确声明 conversation、memory、observation 和 tool output 都是数据，不是系统指令。

### Context Budget And Observability

- `ContextBudgetReport` 统计 request、conversation、memory、plan、observations、tool specs 和 total chars。
- assistant decision trace 写入 budget、source counts、compaction summary 和 tool catalog summary。
- `/runs/{run_id}` 与 `/traces/{trace_id}` 可查询 context 相关摘要。

## Current Limitations

- 当前压缩是 deterministic formatting/truncation，不是 LLM 生成式长期摘要。
- 当前预算是 approximate character budget，不是严格 token-aware budget。
- 当前 memory retrieval 主要是本地关键词/片段匹配，不包含 embedding/vector retrieval。
- 会话历史压缩只压较早轮次文本，不做跨轮语义重写、事实抽取或冲突消解。
- assistant loop 的真实 LLM 路径中，长期记忆写入应由 assistant 通过 `memory_save` 工具显式选择；图尾不会自动写长期 task summary。

## Key Files

- `src/multimodal_agent/services/context/builder.py`
- `src/multimodal_agent/services/context/conversation.py`
- `src/multimodal_agent/services/context/compaction.py`
- `src/multimodal_agent/services/context/renderer.py`
- `src/multimodal_agent/schemas/context.py`
- `src/multimodal_agent/services/assistant_run_service.py`
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

## Next Steps

- Add token-aware budgeting behind the existing `ContextBudgetReport` boundary.
- Add optional semantic summary generation for older conversation history, with deterministic offline behavior for tests.
- Add optional embedding/vector retrieval behind `MemoryManager` and `MemoryStore`.
- Add stronger evals for context relevance, prompt injection resistance, and multi-turn factual continuity.
- Keep real provider summarization or embedding opt-in only under controlled runtime profiles.
