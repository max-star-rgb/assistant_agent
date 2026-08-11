# Deep Research Workflow 可观测性实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Deep Research 增加 Workflow 总览 trace，展示 Plan、work item、重试/返工和终态，同时保留并链接独立 Assistant trace。

**Architecture:** WorkflowStore 的提交成功边界通过 fail-open decorator 观察新增 WorkflowEvent；独立 mapper 将已提交事件转换为同一 `workflow_id` 下的 OTel span。每个 work item 继续运行独立 `AgentGraphRuntime` trace，其 trace/run ID 作为结构化关联写入 Workflow event，不把 Provider-native 搜索伪造成独立步骤。

**Tech Stack:** Python、Pydantic、SQLite/InMemory WorkflowStore、OpenTelemetry、Langfuse、pytest。

## Global Constraints

- 不访问、启动或重启 8089。
- pytest 只使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider。
- Workflow persistence 仍是事实源，OTel/Langfuse 仅做 fail-open 派生投影。
- 不把 artifact 正文、hidden reasoning 或 Provider 原始响应写进 Workflow 总览 trace。
- 不回滚工作区中已有的无关修改。

---

### Task 1: Work-item 关联事实

**Files:**
- Modify: `src/assistant_agent/workflows/agent_runtime.py`
- Modify: `src/assistant_agent/workflows/execution.py`
- Modify: `src/assistant_agent/workflows/runtime.py`
- Test: `tests/tdd/workflow-langfuse-overview/test_workflow_observability.py`

**Interfaces:**
- Produces: `WorkItemExecutionResult.assistant_trace_id`, `assistant_run_id`, `started_at`, `finished_at`。
- Produces: work-item terminal `WorkflowEvent.payload` 中对应关联字段和 `attempt_id`。

- [ ] 写失败测试，断言成功 work item 的持久化事件包含 Assistant trace/run ID 和执行时间。
- [ ] 运行定向 pytest，确认因字段缺失而失败。
- [ ] 从 `AgentGraphRuntime.run_work_item()` 向 execution result 传播 trace/run ID，并在 controller 包围 executor 记录时间。
- [ ] 将关联事实加入所有 work-item terminal/retry/repair/waiting 事件。
- [ ] 运行定向 pytest，确认测试通过。

### Task 2: Workflow OTel 总览投影

**Files:**
- Create: `src/assistant_agent/observability/workflow_otel.py`
- Modify: `src/assistant_agent/workflows/store.py`
- Test: `tests/tdd/workflow-langfuse-overview/test_workflow_observability.py`

**Interfaces:**
- Produces: `build_workflow_otel_span_specs(bundle, events) -> list[OtelSpanSpec]`。
- Produces: `WorkflowOtelObserver.on_workflow_commit(bundle, events) -> None`。
- Produces: `ObservedWorkflowStore(inner, observer)`，只在 create/save 成功后通知 observer。

- [ ] 写失败测试，断言 Plan、多个 work-item 和 terminal root 位于同一 Workflow trace。
- [ ] 写失败测试，断言 work-item span 输出包含 Assistant trace 链接身份。
- [ ] 实现稳定 root/plan/attempt span ID、Prompt-safe input/output 和 Langfuse metadata。
- [ ] 实现 fail-open observer 与 store decorator；observer 失败不得改变 create/save 结果。
- [ ] 运行定向 pytest，确认映射、提交后通知和 fail-open 全部通过。

### Task 3: 默认装配与权威同步

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `docs/observability-harness.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Test: `tests/tdd/workflow-langfuse-overview/test_workflow_observability.py`

**Interfaces:**
- Consumes: `create_workflow_otel_observer_from_env()` 与 `ObservedWorkflowStore`。
- Produces: 默认 durable workflow store 在 OTLP 可用时自动输出 Workflow 总览 trace。

- [ ] 写失败测试，验证 observer 关闭和底层 store 关闭都保持 best-effort 生命周期。
- [ ] 在默认 SQLite WorkflowStore 外装配 observer decorator；注入的 WorkflowService 保持不变。
- [ ] 更新 observability/runtime 权威，说明双层 trace、关联字段和 Provider-native 搜索边界。
- [ ] 运行临时 TDD、相关 durable workflow 测试、observability contract、Ruff 和文档 authority validator。
