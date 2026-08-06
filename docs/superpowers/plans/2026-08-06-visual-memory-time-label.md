# 视觉历史时间标签实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `visual_memory_search` 返回的每条 VLM 文本增加由可信帧时间确定的、主 LLM 可直接理解的 `time_label`。

**Architecture:** 在视觉历史查询服务冻结一次 `query_at_ms`，用独立纯函数把 observation 的 `timestamp_ms` 格式化为相对时间和 `Asia/Shanghai` 绝对时间。标签只进入 Tool 模型投影；Store、VLM、embedding 与相关性排序保持不变。

**Tech Stack:** Python 3.11、Pydantic、标准库 `datetime`/`zoneinfo`、pytest。

## Global Constraints

- 默认 mock/offline，不调用真实 Provider。
- 保留 `timestamp_ms`，新增 `time_label`；不修改 VLM prompt、embedding 输入或最终回答 prompt。
- 使用临时 `tests/tdd/visual-memory-time-label/`，不修改 `tests/core`。

---

### Task 1: Tool 时间投影

**Files:**
- Modify: `src/assistant_agent/media/video/visual_timeline_context.py`
- Modify: `src/assistant_agent/media/embedding/consumers/object_search.py`
- Modify: `src/assistant_agent/context/compaction.py`
- Create: `tests/tdd/visual-memory-time-label/test_visual_memory_time_label.py`
- Modify: `docs/multimodal-embedding-architecture.md`

**Interfaces:**
- Consumes: `VisualSemanticRecord.captured_at_ms|created_at_ms`、查询执行时的毫秒时钟。
- Produces: `VisualTimelineItem.time_label: str | None`；格式为 `约1分13秒前（2026-08-06 11:21:48 +08:00）`。

- [x] **Step 1: 写失败测试**

测试用固定 `query_at_ms=1785986581927` 和帧时间 `1785986508927`，断言查询结果包含：

```python
assert item.time_label == "约1分13秒前（2026-08-06 11:21:48 +08:00）"
```

另断言未来时间戳只显示绝对时间，以及 context projection 保留 `time_label`。

- [x] **Step 2: 验证 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-memory-time-label
```

Expected: 因 `VisualTimelineItem` 没有 `time_label` 或查询结果缺少该字段而失败。

- [x] **Step 3: 最小实现**

给 `VisualTimelineItem` 增加可选 `time_label`；在 `VisualMemorySearchService` 注入毫秒时钟并冻结一次查询时间；
用标准库确定性格式化相对时间和 `Asia/Shanghai` 绝对时间；context projection 显式复制该字段。

- [x] **Step 4: 验证 GREEN 与回归**

运行新 TDD、现有视觉历史相关 TDD、core pytest、Ruff 和 `git diff --check`；全部必须通过。

- [x] **Step 5: 同步权威文档**

更新 `docs/multimodal-embedding-architecture.md`，声明时间来自系统帧时间，仅用于 Tool 模型投影，不参与
VLM、embedding 或排序。
