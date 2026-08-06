# Visual Memory VLM 文本检索实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `visual_memory_search` 一次性把可信范围内最多 256 条带时间戳的单帧 VLM 文本交给主 LLM，不再执行 query/record embedding 或相似度过滤。

**Architecture:** 并行功能提供单帧 VLM `summary` 和 Store 时间线事实源；本功能新增 session 全历史 as-of 读取，并把 `{timestamp_ms, text}` 列表作为 Tool observation。Context 对该 Tool 使用专用无损投影并把完整列表纳入 hard context preflight，其他 Tool 继续使用通用压缩。

**Tech Stack:** Python、Pydantic、assistant context pipeline、pytest。

## Global Constraints

- 不修改并行进程负责的 VLM 单帧输出、`live_view_inspect` 时间线和视觉提醒生产路径。
- `visual_memory_search` 不调用 query embedding、VLM、网络或真实 Provider。
- Runtime 继续绑定可信 user/session/as-of；调用方不能扩大可见范围。
- 返回列表按观察时间正序，最多包含 Store retention 内的 256 条记录。
- 不返回向量、similarity、evidence path、图片或 raw Provider payload。
- Context 不得把该列表截成 3 条；超出 hard model context 时必须由现有 preflight 显式阻断。

---

### Task 1: Store 全历史文本时间线

**Files:**
- Modify: `src/assistant_agent/media/video/semantic_store.py`
- Create: `tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py`

**Interfaces:**
- Consumes: 并行功能新增的 `VisualSemanticRecord.summary` 单帧文本记录。
- Produces: `SessionVisualSemanticStore.text_timeline(as_of_sequence: int | None, since_ms: int | None, until_ms: int | None, limit: int = 256) -> list[VisualSemanticRecord]`。
- Produces: `SessionVisualSemanticStore.has_visual_history() -> bool`。

- [ ] **Step 1: 写 Store RED 测试**

构造 256 条历史记录和 1 条 future record，断言 `text_timeline(as_of_sequence=256)` 返回全部 256 条、防御性副本、按 `(captured_at_ms or created_at_ms)` 正序排列，并验证 `since_ms/until_ms`。

- [ ] **Step 2: 运行 Store RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py \
  -k 'store_text_timeline'
```

Expected: FAIL，因为 `text_timeline` / `has_visual_history` 尚不存在。

- [ ] **Step 3: 实现最小 Store API**

在现有 lock 内筛选 `_records.values()`：sequence 不超过 as-of、timestamp 落在窗口内；按观察时间、sequence、created time 排序，截取最多 256 条并返回 deep copies。`has_visual_history` 只判断是否存在成功 VLM record，不读取 `index_status`。

- [ ] **Step 4: 运行 Store GREEN**

重复 Step 2 命令，Expected: PASS。

### Task 2: Tool 输出完整 VLM 文本列表

**Files:**
- Modify: `src/assistant_agent/media/embedding/consumers/object_search.py`
- Modify: `src/assistant_agent/media/embedding/consumers/__init__.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/system_prompt_policy.py`
- Test: `tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py`
- Modify: `tests/tdd/unified-siglip2/test_visual_memory_tool.py`
- Modify: `tests/tdd/unified-siglip2/test_visual_memory_tool_gating.py`
- Modify: `tests/tdd/unified-siglip2/test_visual_memory_search_service.py`

**Interfaces:**
- Consumes: `text_timeline(...)` 和 Runtime 注入的 `_trusted_visual_memory_as_of_sequence`。
- Produces: `VisualMemoryTextObservation(timestamp_ms: int, text: str)`。
- Produces: `VisualMemorySearchResult(status: Literal["records", "empty"], observations: list[VisualMemoryTextObservation], observation_count: int, errors: list[dict[str, object]])`。

- [ ] **Step 1: 写 Tool RED 测试**

用一个 `embed_text()` 会抛 `AssertionError` 的 coordinator sentinel（若旧依赖仍存在）执行 Tool；断言 Tool 成功、没有调用 embedding，并返回 sequence 33 的黑色手机文本及全部 256 条时间线。断言 time window 和 as-of 会收窄列表，但 `query/search_mode` 不参与排序或过滤。

- [ ] **Step 2: 运行 Tool RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py \
  -k 'visual_memory_tool'
```

Expected: FAIL，旧实现调用 embedding 并返回 `matches/similarity`。

- [ ] **Step 3: 实现文本列表 Service 与 Tool**

保留 `query/time_window/search_mode` 输入兼容性；Service 仅调用 Store 时间线。Tool 只 acquire semantic-store lease，删除 coordinator lease、candidate/confirmed threshold 和 embedding 错误文案。Plugin 在 `vision_ready + visual_semantic_store_pool` 时注册 Tool，不再要求 coordinator。Runtime exposure 改用 `has_visual_history()`；system policy 明确 status 只表示列表可用性，主 LLM必须阅读所有 `observations` 后自行判断。

- [ ] **Step 4: 更新旧临时测试契约并运行 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-memory-vlm-text-search \
  tests/tdd/unified-siglip2/test_visual_memory_tool.py \
  tests/tdd/unified-siglip2/test_visual_memory_tool_gating.py \
  tests/tdd/unified-siglip2/test_visual_memory_search_service.py
```

Expected: PASS。

### Task 3: Context 无损 Tool observation

**Files:**
- Modify: `src/assistant_agent/context/compaction.py`
- Modify: `src/assistant_agent/context/builder.py`
- Test: `tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py`

**Interfaces:**
- Consumes: `visual_memory_search` observation 中最多 256 条 `{timestamp_ms, text}`。
- Produces: Provider tool-role message 保留全部 `observations`；其他 Tool list 仍受 `MAX_ITEMS_PER_LIST=3` 限制。

- [ ] **Step 1: 写 Context RED 测试**

构造包含 256 条时间线的 canonical Tool observation，经过 `project_observations_for_context` 后断言仍为 256 条；构造普通 Tool 的 4 项 list，断言仍压缩为 3 条。再用小 char budget 构建 context pack，断言视觉时间线不被 `_trim_observations_to_chars` 改写，预算报告明确 over-budget/observations 未静默缩减。

- [ ] **Step 2: 运行 Context RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py \
  -k 'context_preserves_visual_memory'
```

Expected: FAIL，通用 compactor 只保留 3 条或 builder 把 observation 概括掉。

- [ ] **Step 3: 实现 Tool 专用 Context 投影**

在 `_compact_data` 为 `VISUAL_MEMORY_SEARCH_TOOL_NAME` 添加窄分支，仅复制安全的 `status/observations/observation_count/errors`，保留全部 observation 元素；复用 sanitize 以删除 inline media。调整 `_trim_observations_to_chars`：永不概括或删除该 Tool observation，优先收缩其他可压缩项；若受保护 observation 本身超过预算，保留原文和 over-budget 事实，由 `ContextService.preflight` 的 hard 决策阻断 Provider 调用。

- [ ] **Step 4: 运行 Context GREEN**

重复 Step 2 命令，Expected: PASS。

### Task 4: 权威文档与最小回归

**Files:**
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Test: `tests/tdd/visual-memory-vlm-text-search/test_visual_memory_vlm_text_search.py`

**Interfaces:**
- Documents: 历史视觉检索改为完整 VLM 文本时间线；提醒仍保持 VLM 前实时 text-to-image。

- [ ] **Step 1: 等待并行视觉时间线变更稳定后同步权威文档**

只修改历史检索段落，不覆盖对方对单帧 VLM、live view、提醒或 proactive delivery 的变更。

- [ ] **Step 2: 运行完整定向验证**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-memory-vlm-text-search \
  tests/tdd/live-view-text-timeline \
  tests/tdd/unified-siglip2/test_visual_memory_tool.py \
  tests/tdd/unified-siglip2/test_visual_memory_tool_gating.py \
  tests/tdd/unified-siglip2/test_visual_memory_search_service.py
```

Expected: PASS。

- [ ] **Step 3: 静态验证**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/media/video/semantic_store.py \
  src/assistant_agent/media/embedding/consumers/object_search.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py \
  src/assistant_agent/context/compaction.py \
  src/assistant_agent/context/builder.py

git diff --check -- \
  src/assistant_agent/media/video/semantic_store.py \
  src/assistant_agent/media/embedding/consumers/object_search.py \
  src/assistant_agent/media/embedding/consumers/__init__.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py \
  src/assistant_agent/runtime/runtime.py \
  src/assistant_agent/runtime/system_prompt_policy.py \
  src/assistant_agent/context/compaction.py \
  src/assistant_agent/context/builder.py \
  tests/tdd/visual-memory-vlm-text-search \
  docs/multimodal-embedding-architecture.md \
  docs/context_engineering_status.md
```

Expected: exit 0。

- [ ] **Step 4: 提交本任务相关文件**

按项目规则只 stage 本任务文件；不提交并行进程或用户已有改动。设计/计划文档默认不纳入提交，除非用户另行要求。
