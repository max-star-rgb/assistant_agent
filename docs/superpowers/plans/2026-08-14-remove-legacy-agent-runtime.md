# 旧 Agent Runtime 清理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 迁移或删除仍依赖 `AgentGraphRuntime` 的外围入口，彻底移除 `AgentGraphRuntime`、`AssistantTurnGraphApp`、`WorkflowGraphHost` 及仅服务于它们的旧 Graph 兼容闭环。

**Architecture:** 生产执行统一落到 `AssistantRootGraph`；服务生命周期继续由 LangGraph Agent Server 拥有，仓库内后续重建真实评测时只使用 evaluation-scoped target，不重建通用 Runtime facade。现有三个 LangSmith runner 的 evidence、fixture backend 与终态合同绑定旧 `AgentState`，本次直接删除并在 authority 记录缺口；可注入的外围协议继续保留。

**Tech Stack:** Python 3.11、LangGraph 1.2.x、LangChain 1.3.x、Pydantic 2、LangSmith、pytest。

## Global Constraints

- 默认和 pytest 始终使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider、LangSmith 或外部 Tool。
- 不新增第二个通用 Runtime facade；生产 thread/run/checkpoint/stream/cancel/interrupt 仍由 Agent Server 原生拥有。
- 保留仍被 Tool、Provider、Context、媒体、durable task 使用的中立 DTO、adapter 与治理模块，不按目录整包误删。
- 删除三个绑定旧 Runtime 的 LangSmith runner；不得用兼容 facade 冒充原生 Graph 评测。
- 临时 RED/GREEN 测试放在 `tests/tdd/legacy-agent-runtime-removal/`，用户可在完成后手动整目录删除。
- 修改 authority 后运行 `scripts/check_documentation_authority.py --repo-root .`。

---

### Task 1: 建立原生替代入口的 RED 测试

**Files:**
- Create: `tests/tdd/legacy-agent-runtime-removal/test_native_evaluation_target.py`

**Interfaces:**
- Consumes: `AssistantRootGraph` 的真实 mock/offline composition。
- Produces: evaluation-scoped target 可执行一次 fast run 并返回标准 `AIMessage` 的替代合同。

- [ ] 写测试调用 `NativeGraphEvaluationTarget.ainvoke`，断言标准 messages、稳定 thread/run identity 和最后一个 `AIMessage`；该测试会在 target 尚不存在时失败。
- [ ] 显式运行测试并确认因替代入口尚不存在而 RED。

### Task 2: 收敛仍有价值的外围消费者

**Files:**
- Create: `src/assistant_agent/evaluation/native_graph_target.py`
- Delete: `evals/langsmith_runtime_regression/`
- Delete: `evals/release_review/`
- Delete: `scripts/run_langsmith_runtime_regressions.py`
- Delete: `scripts/run_release_review.py`
- Delete: `scripts/run_system_tool_evals.py`
- Delete: `scripts/run_system_shopping_eval.py`
- Delete: `tests/tdd/release-review-native-experiment/`
- Delete: `tests/tdd/langsmith-parallel-evaluation/`
- Modify: `src/assistant_agent/multi_agent/agent_router.py`
- Modify: `src/assistant_agent/multi_agent/agent_communication.py`
- Modify: `src/assistant_agent/multi_agent/agent_transports.py`
- Modify: `src/assistant_agent/automation/durable_tasks/worker.py`
- Modify: `src/assistant_agent/mcp/server.py`

**Interfaces:**
- Produces: evaluation-scoped `NativeGraphEvaluationTarget`，直接返回标准 messages 与结构化 evaluation result。
- Consumes: `AgentServerExecutionOwner`、`AssistantRootInput`、`AssistantRunContext` 与 LangGraph `ainvoke`。

- [ ] 为原生 evaluation target 添加临时 RED 测试，证明 mock 模式可执行 fast run、返回最后一个 `AIMessage`，且目标 API 不暴露 `run_state`、产品状态机或旧 `AgentState`。
- [ ] 删除绑定旧 `AgentState`、`ToolExecutionBackend` 与 `trace_store` 的 Runtime Regression/Release Review runner；保留原生 target 作为后续重建入口。
- [ ] 将 multi-agent 本地 transport 收敛为窄的 injected invoker protocol；删除自动构造两个旧 Runtime 的 default router convenience factory。
- [ ] 将 durable task worker 的类型边界只保留 `DurableTaskRuntime` protocol。
- [ ] 从离线 MCP skeleton 删除 `agent_run` 及 Runtime 参数；保留独立 Tool list/run/demo 能力。
- [ ] 运行原生 target 临时测试与保留入口的 import/CLI 最小集合。

### Task 3: 删除旧 workflow 与 assistant graph 闭环

**Files:**
- Delete: `src/assistant_agent/workflows/`
- Delete: `evals/langsmith_workflow_regression/`
- Delete: `scripts/run_langsmith_workflow_regressions.py`
- Delete: `src/assistant_agent/runtime/runtime.py`
- Delete: `src/assistant_agent/runtime/assistant_graph_app.py`
- Delete: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Delete: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Delete: `src/assistant_agent/runtime/assistant_graph_state.py`
- Delete: `src/assistant_agent/runtime/assistant_graph_profiles.py`
- Delete: `src/assistant_agent/runtime/graph_runtime.py`
- Delete: `src/assistant_agent/runtime/graph_time_travel.py`
- Delete: `src/assistant_agent/runtime/graph_invocation_claims.py`
- Delete: `src/assistant_agent/runtime/assistant_interrupts.py`
- Delete: `src/assistant_agent/runtime/runtime_host.py`
- Delete: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Delete: `src/assistant_agent/runtime/checkpointer.py`
- Delete: `src/assistant_agent/runtime/assistant_run_service.py`
- Delete: `src/assistant_agent/runtime/{demo_examples,event_stream,graph_capability_evidence,hook_invariants,hook_metrics,hooks,llm_event_mapping,loop_guard,proactive_delivery,provider_streaming,realtime_task_state,request_metadata,response_composer,response_templates,run_history,run_phase,server_startup_summary,session_models,session_store,startup_dependencies}.py`
- Create: `src/assistant_agent/config_env.py`
- Modify: `src/assistant_agent/observability/trace_conversation.py`
- Delete: `src/assistant_agent/memory/backends/`
- Delete: `src/assistant_agent/memory/commit_ledger.py`
- Delete: `src/assistant_agent/memory/factory.py`
- Delete: `src/assistant_agent/memory/node_bundle.py`
- Delete: `src/assistant_agent/memory/node_observability.py`

**Interfaces:**
- Consumes: Task 2 已迁移后的 import graph。
- Produces: 不含旧 Graph facade、旧 Workflow host 和旧 Memory node bundle 的源码树。

- [ ] 把仍有外部消费者的 `load_env_file` 移到 `config_env.py`；`trace_conversation.py` 就地声明所需窄 store protocol，并更新全部 imports。
- [ ] 删除旧 Memory node bundle/factory/backends；保留原生 `MemoryBackend` 使用的 Mem0 client 与第三方 adapter。
- [ ] 删除明确列出的旧模块；AST/import 入边审计确认无 source/script/eval 可达消费者后，删除同一旧闭环的孤儿模块。
- [ ] 运行 RED 测试，确认转为 GREEN；运行 `compileall` 捕获残留 import。

### Task 4: 清理 authority、脚本索引与过期评测合同

**Files:**
- Modify: `docs/authority.toml`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `evals/README.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Produces: authority 不再把旧模块列为 source，不再把已删除 runner 描述为当前发布门禁。

- [ ] 删除三个 LangSmith runner 的稳定入口与当前能力声明，明确原生行为评测需要后续重建。
- [ ] 更新四份 owner authority 的兼容边界，明确旧 Graph Runtime 已删除。
- [ ] 运行文档 authority validator 与离线 `--inspect`/`--help` 入口。

### Task 5: 最终验证与提交

**Files:**
- Verify only.

**Interfaces:**
- Produces: 可复核的测试、静态搜索、文档验证与 Git diff 证据。

- [ ] 运行 `tests/tdd/legacy-agent-runtime-removal`。
- [ ] 运行 authority manifest 中受影响 domain 的最小 core/TDD 集合；若影响仍无法界定，再运行默认 39 项 core。
- [ ] 运行 `compileall -q src/assistant_agent evals scripts`。
- [ ] 用 `rg` 证明三个旧类名和三个旧模块 import 在当前 authority/source/eval/script 中为零。
- [ ] 审核 `git diff --check`、变更范围和未跟踪文件，提交本任务改动，不 push、不合并、不创建 PR。
