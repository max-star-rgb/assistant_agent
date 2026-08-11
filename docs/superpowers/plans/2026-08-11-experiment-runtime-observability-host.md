# Experiment Runtime 可观测装配深层修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让所有真实 Langfuse Experiment 通过统一 Runtime Host 获得生产级 trace store、父级 Trace 上下文和完整资源生命周期，并对内部 Trace 缺失 fail-closed。

**Architecture:** 新增共享的 Experiment Runtime Host，负责构造 `AgentGraphRuntime`、从当前 OTel span 捕获 `RuntimeTraceContext`、运行请求以及关闭 Runtime/trace store。Runtime Regression 与 Release Review 只保留案例差异，统一复用该 Host；Agent loop 和 canonical event 模型保持不变。

**Tech Stack:** Python 3.12、Pydantic、OpenTelemetry、Langfuse Python SDK、pytest。

## Global Constraints

- pytest 必须使用 mock/local/offline，不调用真实 Provider。
- 日常生产 observability 保持 fail-open；Experiment 的内部 Trace 完整性必须 fail-closed。
- 不回滚当前工作区已有的 Workflow/Trace 修改。
- 新增临时测试只放入 `tests/tdd/runtime-eval-feedback-loop/`；`OBS-001` 的稳定生命周期契约只在现有 core 文件中最小扩展。

---

### Task 1: 统一 Experiment Runtime Host

**Files:**
- Create: `src/assistant_agent/evaluation/runtime_host.py`
- Modify: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py`
- Modify: `tests/core/contract/test_observability_contract.py`

**Interfaces:**
- Consumes: `ProviderConfig`、`RuntimeTraceContext`、`create_server_trace_store()`、`close_trace_store()`。
- Produces: `ExperimentRuntimeHost`、`current_runtime_trace_context()`、`create_experiment_runtime_host()`。

- [ ] 写入父级 trace/span 传播与统一 close 的失败测试。
- [ ] 显式运行测试并确认因 Host 尚不存在而失败。
- [ ] 实现最小 Host 和 OTel context 读取。
- [ ] 运行定向测试并确认通过。

### Task 2: 两类 Experiment 复用 Host

**Files:**
- Modify: `evals/runtime_regression/experiment.py`
- Modify: `evals/runtime_regression/cli.py`
- Modify: `evals/release_review/experiment.py`
- Modify: `evals/release_review/cli.py`
- Modify: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py`
- Modify: `tests/tdd/release-review-native-experiment/test_native_release_experiment.py`

**Interfaces:**
- Consumes: `ExperimentRuntimeHost.run_state(request)` 和 `close()`。
- Produces: Runtime Regression 与 Release Review 一致的 trace/context/lifecycle 行为。

- [ ] 先修改 Fake Host 契约并确认旧 runner 测试失败。
- [ ] 将两个 runner 的 runtime factory 改为 host factory。
- [ ] CLI 使用共享生产装配，不再直接构造裸 `AgentGraphRuntime`。
- [ ] 运行两个 feature 测试目录并确认通过。

### Task 3: Experiment Trace 完整性门禁

**Files:**
- Create or Modify: `src/assistant_agent/evaluation/experiment_trace.py`
- Modify: `evals/runtime_regression/experiment.py`
- Modify: `evals/release_review/langfuse_backend.py`
- Modify: corresponding TDD tests

**Interfaces:**
- Consumes: Dataset run item 的 trace ID、Langfuse Observations API。
- Produces: 对 `experiment-item-task -> agent.runtime -> llm.chat` 的结构化完整性结果；缺失时抛出 infrastructure failure。

- [ ] 写入层级缺失、父级错误和完整层级三组失败测试。
- [ ] 实现共享 Observation 层级校验器。
- [ ] Runtime Regression 在等待 Score 后执行完整性回查；Release Review 复用同一校验器。
- [ ] 运行相关 TDD 并确认通过。

### Task 4: 权威同步与完成验证

**Files:**
- Modify: `evals/README.md`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Produces: 真实入口装配、Experiment trace 层级和 fail-closed 边界的当前权威说明。

- [ ] 同步两份 authority，不复制开发过程。
- [ ] 运行 Runtime Regression、Release Review、OBS-001 定向测试。
- [ ] 运行 documentation authority validator。
- [ ] 在 operator 已授权的现有本地真实配置上重跑一个 Dataset item，验证 Langfuse 实际层级。
- [ ] 审查 diff，只提交本任务相关文件。
