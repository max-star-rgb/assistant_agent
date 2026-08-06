# Visual Memory Tool 尾部上下文压缩实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `visual_memory_search` 增加 query-aware 视觉时间线压缩器，在 ToolResult 进入主 LLM 前按 target/trigger/hard 预算生成有界 observation。

**Architecture:** VLM client 保持逐帧无历史，Store 保留原始 256 条。Tool 尾部的 `VisualTimelineContextService` 复用 `ContextWindowPolicy`，调用专用 LLM compactor 生成摘要与原始记录 indexes；主 `ContextService.preflight` 继续承担完整请求的全局 hard gate。

**Tech Stack:** Python、Pydantic、ChatAdapter、ContextWindowPolicy、ContextTokenCounter、pytest。

## Global Constraints

- 不向逐帧 VLM 请求注入其他帧文本。
- 不修改或删除 Store 中的原始视觉记录。
- mock/offline pytest 不调用真实 Provider。
- hard 区间压缩失败必须返回稳定错误，禁止静默截断或发送超限原文。
- compactor 只能通过 index 选择原始证据，不能改写时间戳或 VLM 文本。
- Core invariant unchanged；测试只进入 `tests/tdd/visual-memory-vlm-text-search/`。

---

### Task 1: 视觉时间线预算控制器

**Files:**
- Create: `src/assistant_agent/media/video/visual_timeline_context.py`
- Modify: `tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py`

**Interfaces:**
- Produces: `VisualTimelineItem(timestamp_ms: int, text: str)`。
- Produces: `VisualTimelineCompactor.compact(query, observations, source_token_count, summary_max_tokens)`。
- Produces: `VisualTimelineContextService.prepare(query, observations) -> VisualTimelineProjection`。
- Raises: `VisualTimelineHardLimitError(code="visual_memory_context_hard_limit")`。

- [ ] 写 below-trigger、trigger/target、below-hard fallback 和 hard failure RED 测试。
- [ ] 运行新测试并确认因模块/类型不存在而失败。
- [ ] 实现 policy evaluate、旧 prefix/最近原文拆分、index 映射、coverage digest、重建复计与 hard gate。
- [ ] 运行 Task 1 测试并确认 GREEN。

### Task 2: LLM 视觉时间线压缩器

**Files:**
- Create: `src/assistant_agent/media/video/visual_timeline_compactor.py`
- Modify: `tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py`

**Interfaces:**
- Produces: `LLMVisualTimelineCompactor(ChatAdapter, token_counter)`。
- Consumes/produces JSON：`summary: str`、`relevant_observation_indexes: list[int]`。
- Produces: `create_visual_timeline_compactor(config, chat_adapter, token_counter)`。

- [ ] 写 prompt 输入、合法 index、越界/重复 index、非法 JSON、summary token 超限和 Provider failure RED 测试。
- [ ] 运行 Task 2 测试确认 RED。
- [ ] 实现固定 prompt、ChatRequest、严格 validator、usage 归一化和 factory provider-mode 边界。
- [ ] 运行 Task 2 测试确认 GREEN。

### Task 3: Tool 尾部与 Runtime 注入

**Files:**
- Modify: `src/assistant_agent/media/embedding/consumers/object_search.py`
- Modify: `src/assistant_agent/media/embedding/consumers/__init__.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py`
- Modify: `tests/tdd/unified-siglip2/test_visual_memory_tool.py`
- Modify: `tests/tdd/unified-siglip2/test_visual_memory_search_service.py`

**Interfaces:**
- Tool constructor accepts optional `timeline_context_service` and exposes an idempotent configure method for Runtime composition。
- `VisualMemorySearchResult` adds `timeline_summary`、`returned_observation_count`、`coverage`、`compaction`。
- hard failure maps to `status=unavailable` and error code `visual_memory_context_hard_limit`。

- [ ] 写 Tool 尾部压缩、未配置兼容路径和 hard failure RED 测试。
- [ ] 运行 Task 3 测试确认 RED。
- [ ] 接入 service；Runtime 使用现有 visual-context tokenizer/policy 和主 ChatAdapter 配置默认 Tool。
- [ ] 运行新旧 Tool/service 测试确认 GREEN。

### Task 4: Context 投影与文档

**Files:**
- Modify: `src/assistant_agent/context/compaction.py`
- Modify: `src/assistant_agent/context/builder.py`
- Modify: `src/assistant_agent/runtime/system_prompt_policy.py`
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py`

**Interfaces:**
- Context 专用投影保留 Tool 已选择的 observations、timeline summary、coverage 和 compaction metadata。
- 通用 list=3 限制不二次破坏压缩器选择的精确证据；全局 tokenizer preflight 保持最终 hard gate。

- [ ] 写压缩后 observation 完整投影 RED 测试。
- [ ] 实现安全字段投影并更新主 LLM 使用说明。
- [ ] 同步两份权威文档，删除“256 条永不压缩”的旧描述。
- [ ] 运行 feature、unified visual、visual context、core、ruff、compileall 和 `git diff --check`。
- [ ] 审计共享脏工作树，只在能隔离同文件并发改动时提交本任务变更。
