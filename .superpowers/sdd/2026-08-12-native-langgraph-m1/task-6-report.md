# M1 Task 6 实施报告

## 结果

- Runtime Regression 改为 `Client.aevaluate()` async Dataset target；target 在 LangSmith 当前
  `RunTree` 内校验 `reference_example_id`，零参数创建 production `AgentGraphRuntime`，直接 await
  `arun_state()`，并在成功、graph 异常及 close 失败路径上严格执行 `finally` 生命周期。
- LangSmith Runtime factory 不再接收 `LangSmithExperimentBinding`，不再创建 Experiment 专用 OTel
  trace store，也不注入 `RuntimeTraceContext`。CLI 只在 `main()` 执行一次 `asyncio.run()`。
- 新增 `audit_native_graph_tree()`，从 LangSmith `list_runs()` 返回的真实 `id/parent_run_id/trace_id/
  reference_example_id/run_type` 审核：
  `task → AssistantTurnGraph → assistant → llm.chat`，`compose_response` 是 graph 直接 child；Tool run
  必须位于 `execute_tool` 子树。缺 graph、graph sibling、LLM/tool sibling、trace/example 错配均 fail-closed。
- 完整性轮询继续要求每个 active Example 恰有一个 task root 和全部三项 Feedback；429 重试与剩余
  deadline 截断语义保持不变。Dataset/reference/actual evaluator output 与稳定 Example ID 保持原契约。
- 删除仅供旧 LangSmith OTel binding 使用的 `evaluation/langsmith_trace.py`；LangSmith 路径不再依赖共享
  `experiment_runtime.py`。

## TDD 证据

首轮 RED：新 async target/current RunTree helper 与 `audit_native_graph_tree()` 尚不存在，共 8 项按预期失败。
CLI RED 随后证明 `_create_item_runtime(config, binding)`、同步 `_execute()` 和未 await Experiment 仍是旧路径。
附加 mutation RED 证明“存在一个合法 LLM 后额外 sibling LLM”及 graph 内 trace mismatch 会被旧审核漏过。

最终临时测试覆盖：async target、current Example 关联、Runtime 三类关闭路径、native graph 正反树、Tool
parentage、trace/example identity、Feedback 完整性、429、CLI 单 async 顶层和无 OTel factory。

## LangSmith 0.10.18 离线 API 证据

本机 `hello_agent` 环境精确版本为 `langsmith==0.10.18`：

```text
Client.aevaluate(self, target, /, data=..., evaluators=..., metadata=...,
                 max_concurrency=0, blocking=True, experiment=...,
                 error_handling='log', ...) -> AsyncExperimentResults
```

调用本身需要 await，结果通过 async iteration 读取。`Client.list_runs()` 支持 `select`，`Run` schema 包含
`id/parent_run_id/name/run_type/reference_example_id/trace_id/inputs/outputs`，可直接审计 parent-child，不需
从名称集合或 OTel attributes 推断。probe 只做本地 import/signature/schema 反射，无网络请求。

## 旧代码与测试处理

- 删除 `test_langsmith_experiment_runtime.py`，因为其中两项只保护已删除的
  `LangSmithExperimentBinding → RuntimeTraceContext/OTel attributes` 投影；另一项通用
  `ExperimentRuntimeHost` 生命周期已由
  `tests/tdd/runtime-eval-feedback-loop/test_experiment_runtime_host.py` 独立覆盖。
- `experiment_runtime.py` 未修改。`rg` 证明它的生产消费者仅剩 Langfuse
  `evals/runtime_regression/cli.py` 与 `evals/release_review/cli.py`；Task 6 的 LangSmith 路径已零引用。
  在 M5 前改动该模块会越界影响仍需兼容的 Langfuse runner。
- Task 7 的 server canonical OTel dual-tree composition 没有修改；相关 factory/test 保留给 Task 7。

## 验证

```text
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/tdd/native-langgraph-runtime tests/tdd/langsmith-parallel-evaluation
77 passed

MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/tdd/runtime-eval-feedback-loop tests/tdd/langsmith-evaluator-automation
31 passed

MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q
86 passed

python scripts/check_documentation_authority.py --repo-root .
valid=true, errors=[]

python -m compileall -q src evals/langsmith_runtime_regression \
  tests/tdd/native-langgraph-runtime tests/tdd/langsmith-parallel-evaluation
通过

git diff --check
通过
```

全部 pytest 使用 mock/local/offline；未调用真实 LangSmith API、真实 Provider、网络或付费服务。

验证期间曾并发启动三套 pytest，Task 7 既有的 50ms wall-clock 用例
`test_observer_close_uses_one_parallel_deadline` 一次返回 `True`（该轮相关套件为 76 passed / 1 failed）。
按 systematic debugging 隔离复跑该项为 1 passed，随后取消并发、串行重跑完整相关套件为 77 passed；
根因是并发 pytest 进程调度抖动放大极短 deadline，不涉及 Task 6 代码。本任务未修改该 observer 或测试，
也未把首次失败隐去或计作首次全绿。

## 测试策略

Core invariant: unchanged。Task 6 改变的是具体 LangSmith Runtime Regression integration，不新增稳定 core
invariant；现有默认 core 86 项全部通过，未修改 `tests/core`。

Tests: 新增/更新 `tests/tdd/native-langgraph-runtime` 与
`tests/tdd/langsmith-parallel-evaluation` 临时 RED/GREEN；用户可手动删除整个 feature 目录，不自动晋升
core。

## 范围与限制

- 本任务只迁移 LangSmith Runtime Regression；Langfuse Release Review 留给 M5。
- 本任务不停止 server canonical OTel dual tree；该删除属于 Task 7。
- 未获 operator 真实 Provider/LangSmith 运行授权，因此没有运行真实 Experiment；未用 fake 结果冒充真实
  行为证据。

## Review fix round 1/5

独立审查提出 3 个 Important，均经 RED 复现并修复：

1. `langsmith==0.10.18` 的 `AsyncExperimentResults.get_dataset_id()` 是 coroutine。fake 现改为 async 并记录
   await 事实；production 使用 `await native.get_dataset_id()`，避免把 coroutine repr 写入结果并消除
   unawaited coroutine warning。
2. native tree audit 现严格校验 run type：Experiment task、`AssistantTurnGraph`、`assistant`、
   `compose_response`、`execute_tool` 必须为 `chain`，`llm.chat` 必须为 `llm`；任一类型错误均以结构化问题
   fail-closed。真实 LangGraph `astream_events(v2)` 离线 probe 显示上述 graph/node 全部产生
   `on_chain_start`；LangSmith async target 默认 `traceable` 类型也是 `chain`，项目 LLM/Tool wrapper 分别
   显式使用 `llm` / `tool`。
3. 删除“出现 `execute_tool` 就必须有 Tool child”的错误假设。validator rejection、loop guard 等路径允许
   zero Tool child；但只要存在任何 `run_type=tool` run，它仍必须位于 `execute_tool` 子树。

Fix round RED：`8 failed, 10 passed`，分别命中 async dataset ID、zero Tool child 和六类错误 run type。

Fix round GREEN：Task6 单文件 18 passed；完整 native+LangSmith 临时套件 84 passed；legacy related 31
passed；default core 86 passed；authority、compileall、diff check 通过。全部为 fake/mock/offline，无网络或
真实 Provider。
