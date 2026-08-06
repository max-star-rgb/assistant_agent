# 修复 Langfuse Live Evaluator Metadata 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 OTel 导出的可筛选 observation metadata 与现有 Langfuse live evaluator 规则一致，使新产生的日常 trace 自动生成对应 Score。

**Architecture:** canonical event 继续保留现有 `assistant_agent.*` 普通属性；仅在 OTel 投影边界为 evaluator 所需字段增加 `langfuse.observation.metadata.*` 显式映射。现有规则继续过滤顶层 metadata，不修改 trace schema，也不回填历史 Score。

**Tech Stack:** Python、Pydantic、OpenTelemetry、Langfuse 3.224.2、pytest。

## Global Constraints

- pytest 保持 mock/local/offline，不调用真实 Provider 或 Judge。
- 不修改或覆盖用户当前其他未提交改动。
- 只投影 `runtime_action` 与 `memory_semantic_evidence` 两个既有安全枚举字段。
- 本机 Langfuse 配置只做最终显式 operator 操作；代码测试阶段不写远端状态。

---

### Task 1: 对齐 evaluator metadata 投影

**Files:**
- Modify: `src/assistant_agent/observability/otel_mapping.py`
- Create: `tests/tdd/runtime_audit/test_evaluator_metadata_projection.py`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Consumes: `TraceEvent.attributes["runtime_action"]` 与 memory ingestion content evidence。
- Produces: `OtelSpanSpec.attributes["langfuse.observation.metadata.assistant_agent.runtime_action"]` 和 `OtelSpanSpec.attributes["langfuse.observation.metadata.assistant_agent.memory_semantic_evidence"]`。

- [ ] **Step 1: 写失败测试**

  构造最终 `llm.chat.finished` 与 `memory.ingestion.finished` 事件，通过公开的 `build_text_otel_span_specs()` / `build_late_text_otel_span_spec()` 断言两个显式 metadata 属性存在，并保留原普通属性。

- [ ] **Step 2: 验证 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime_audit/test_evaluator_metadata_projection.py`

  Expected: FAIL，缺少 `langfuse.observation.metadata.assistant_agent.*` 属性。

- [ ] **Step 3: 最小实现**

  在 OTel observation 投影边界增加两个显式、可筛选 metadata 属性；不改变 evaluator rule 名称、Score 名称或 canonical event。

- [ ] **Step 4: 验证 GREEN 与回归**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime_audit`

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/contract/test_observability_contract.py`

- [ ] **Step 5: 同步文档并提交**

  说明普通 OTel attributes 不可直接用于 Langfuse metadata filter，live evaluator 所需字段必须使用 `langfuse.observation.metadata.*`。提交只包含本任务源码、TDD 测试和文档片段。
