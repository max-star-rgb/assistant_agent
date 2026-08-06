# Live View Tool Observation 精简 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 仅收窄 `live_view_inspect` 的 LLM-facing observation，在不改变完整 ToolResult、trace、API 和 as-of 行为的前提下减少重复及内部字段。

**Architecture:** 保留 `VideoUnderstandingBranch` 构造的完整 payload，以一个 live-view 专用投影函数生成分组后的 `visual_facts` 与 `freshness`。通用 `observation_from_tool_result()` 和其他媒体工具保持不变。

**Tech Stack:** Python 3.11、Pydantic、pytest、现有 ToolResult/ToolObservation 契约。

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，pytest 不读取真实 Provider 配置、不访问网络。
- 不修改完整 `ToolResult.data`、`trace_summary`、Capability contract、等待上限和 as-of sequence 语义。
- 不修改 `tests/core`；临时 TDD 测试保存在独立 feature 目录。
- 更新当前权威媒体协议文档；设计和计划文档不自动提交。

---

### Task 1: 收窄 live-view 模型投影

**Files:**
- Create: `tests/tdd/live-view-observation-compaction/test_live_view_observation_compaction.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Modify: `tests/tdd/unified-siglip2/test_live_view_semantic_store.py`

**Interfaces:**
- Consumes: `_video_model_observation(payload: dict[str, Any]) -> dict[str, Any]` 的现有调用点。
- Produces: `_live_view_model_observation(payload: dict[str, Any]) -> dict[str, Any]`，只用于 rolling live-view 成功与不可用结果。

- [x] **Step 1: 写成功与不可用投影的失败测试**

测试构造真实 `LiveViewInspectTool` + 本地 semantic store，断言完整 `result.data` 仍含旧字段，同时 `result.model_observation` 只包含 `status/summary/visual_facts/confidence/freshness/usable_visual_text/error_code` 中有意义的字段，并明确排除内部字段。

- [x] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/live-view-observation-compaction
```

Expected: FAIL，因为当前模型投影仍是平铺字段。

- [x] **Step 3: 实现最小 live-view 专用投影**

增加 `_live_view_model_observation()`：视觉事实归入 `visual_facts`；观察时间、gap、fallback 和刷新状态归入 `freshness`；保留 false/0，删除空容器；成功与不可用 live-view 分支改用该函数。显式视频仍使用 `_video_model_observation()`，避免扩大变更范围。

- [x] **Step 4: 更新旧临时测试断言并运行 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/live-view-observation-compaction tests/tdd/unified-siglip2/test_live_view_semantic_store.py
```

Expected: PASS。

### Task 2: 同步文档并验证改动

**Files:**
- Modify: `docs/media-agent-service-websocket.md`

**Interfaces:**
- Consumes: Task 1 的实际模型投影结构。
- Produces: 当前权威文档中的 live-view observation 边界说明。

- [x] **Step 1: 更新媒体协议权威文档**

在 `live_view_inspect` 行为段落说明：完整 ToolResult 保持宽结构；主 LLM 只接收分组后的视觉事实、新鲜度、置信度和可用性，不接收 Provider、模型、媒体引用与原始 sequence 标识。

- [x] **Step 2: 运行最终定向验证**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/live-view-observation-compaction tests/tdd/unified-siglip2
git diff --check -- src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py \
  tests/tdd/live-view-observation-compaction tests/tdd/unified-siglip2/test_live_view_semantic_store.py \
  docs/media-agent-service-websocket.md
```

Expected: pytest 全部 PASS，`git diff --check` 无输出且退出码为 0。

- [x] **Step 3: 基于既有 trace 与源码形成 latency 优化分析**

用 trace `855908e1a7d760e26bd40957382af6d8` 的 `semantic_publish_latency_ms`、`observation_latency_ms`、Provider latency、队列状态和 `wait_for_sequence` 剩余等待，按收益、风险和实现成本排序建议；不在本任务中修改后台视觉执行行为。
