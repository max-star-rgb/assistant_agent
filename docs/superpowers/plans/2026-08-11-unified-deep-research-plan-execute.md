# Deep Research 统一 Plan-and-Execute Runtime 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让显式 `deep_research` mode 启动一个从主 Agent `plan` 节点开始的 durable Plan-and-Execute execution，并保留可替换的 subagent work-item executor。

**Architecture:** Gateway 继续只归一化 mode；`AgentGraphRuntime` 在可信 `deep_research` 入口执行薄启动，不再运行一次前台 ReAct 来调用 `workflow_submit`。`WorkflowRuntime` 以 durable `plan` work item 开始，主 Agent 产出结构化 DAG，随后通过 worker executor 端口调度可替换子 Agent；全部节点属于同一 Workflow execution 和 Langfuse trace。

**Tech Stack:** Python、Pydantic、LangGraph、WorkflowStore、OpenTelemetry/Langfuse、pytest。

## Global Constraints

- 不启动或访问 8089。
- pytest 固定使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider。
- Gateway 不解析用户自然语言，不承担 planner 或 executor 职责。
- WorkflowStore 继续是 durable 状态事实源；单个子 Agent run 只作为可恢复 quantum。
- 主 Agent planner 与 subagent executor 使用显式接口分离，默认实现允许复用同一个 `AgentGraphRuntime` 内核。
- 不回滚工作区中已有的无关修改。

---

### Task 1: Durable planner 契约

**Files:**
- Modify: `src/assistant_agent/workflows/models.py`
- Modify: `src/assistant_agent/workflows/agent_runtime.py`
- Modify: `src/assistant_agent/workflows/execution.py`
- Test: `tests/tdd/deep-research-plan-execute/test_plan_execute_runtime.py`

**Interfaces:**
- Produces: 结构化 `WorkflowPlanProposal`。
- Produces: planner role 的 bounded Agent request/result；worker executor 端口保持独立。

- [ ] 写失败测试：Deep Research 初始 durable plan 只有可执行的 `plan` work item。
- [ ] 写失败测试：planner 必须返回严格结构化 DAG，普通文本进入可解释失败。
- [ ] 实现 proposal schema、planner prompt 与严格解析。
- [ ] 运行定向测试确认 GREEN。

### Task 2: Plan-and-Execute 状态转换

**Files:**
- Modify: `src/assistant_agent/workflows/research/definition.py`
- Modify: `src/assistant_agent/workflows/runtime.py`
- Modify: `src/assistant_agent/workflows/transitions.py`
- Test: `tests/tdd/deep-research-plan-execute/test_plan_execute_runtime.py`

**Interfaces:**
- Consumes: `WorkItemExecutionResult.plan_proposal`。
- Produces: planner 成功后原子提交的新 Plan version 与 `workflow.plan.created` 事件。

- [ ] 写失败测试：第一 quantum 执行 planner，提交生成 DAG 后才允许 worker item ready。
- [ ] 写失败测试：planner retry、非法 DAG 与恢复不丢失 Workflow identity。
- [ ] 实现 plan proposal admission、DAG 校验和 plan revision commit。
- [ ] 运行定向测试确认 GREEN。

### Task 3: 显式 mode 薄启动

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/context/tool_catalog.py`
- Modify: `src/assistant_agent/context/prompt_compiler.py`
- Test: `tests/tdd/deep-research-plan-execute/test_plan_execute_runtime.py`
- Test: `tests/tdd/deep-research-mode/test_deep_research_mode.py`

**Interfaces:**
- Consumes: `UserRequest.assistant_mode == "deep_research"`。
- Produces: 一个 Workflow handle 响应；不发生前台 planner LLM/tool call。

- [ ] 写失败测试：Deep Research 入口零次调用 chat adapter，直接创建 planner-pending Workflow。
- [ ] 写失败测试：standard 模式继续走既有 assistant loop。
- [ ] 实现 runtime strategy 分支和稳定 idempotency；保留 legacy `workflow_submit` 给 standard/其他显式 Tool 场景。
- [ ] 运行 mode、Gateway 与 Workflow 定向测试确认 GREEN。

### Task 4: 单 execution Trace

**Files:**
- Modify: `src/assistant_agent/observability/workflow_otel.py`
- Modify: `src/assistant_agent/observability/otel_mapping.py`
- Modify: `src/assistant_agent/runtime/event_publisher.py`
- Test: `tests/tdd/workflow-langfuse-overview/test_workflow_observability.py`

**Interfaces:**
- Produces: 根 observation `deep_research.workflow`。
- Produces: `workflow.start`、`plan`、worker/subagent 和 synthesis 子层级。

- [ ] 写失败测试：Trace 根不是 `assistant.submit`，且 planner/worker 在同一 trace 下。
- [ ] 实现跨进程稳定 parent/span identity 和 fail-open 投影。
- [ ] 运行观测专项确认 GREEN。

### Task 5: Authority 与验证

**Files:**
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/observability-harness.md`

- [ ] 更新职责边界、Plan-and-Execute 状态图与主/子 Agent 扩展点。
- [ ] 运行 Deep Research、Workflow、Gateway、observability 定向 pytest。
- [ ] 运行 Ruff、`git diff --check` 和 documentation authority validator。
