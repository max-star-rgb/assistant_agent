# 原生 Agent 机制收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LangGraph 原生 channel reducer、node retry/error handler、edge 与 `Command` 承担运行机制，并把 planning 收缩为 planner、DAG worker waves、finalize 的最小闭环。

**Architecture:** 正常控制流使用静态 edge，Memory 失败通过 LangGraph 原生 `error_handler + Command` 扩展能力回到显式公共路由或结束节点；planning 结果使用 LangGraph `BinaryOperatorAggregate` 的列表追加语义，不再维护 revision、repair、verification、artifact 和派生完成 ID。项目不建立自研降级层，只声明 `degraded` 且继续这一产品结果；保留结构化 execution mode、父图一次 recall/commit、DAG 引用与无环校验、同一个 fast Agent worker。

**Tech Stack:** Python 3.12、LangGraph StateGraph/Command/Send/RetryPolicy、LangChain messages、Pydantic 2、pytest。

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider、Memory 或外部服务。
- 不修改 `tests/core`：RUN-001、LOOP-001、MEMORY-001 的稳定行为不变。
- RED/GREEN 只更新 `tests/tdd/native-agent-parent-graph/`；该目录由用户决定是否整目录删除。
- 保留 fast Agent、父图 checkpoint/runtime context、主动投递和 Memory backend 协议。
- 项目代码不引用或配置 LangGraph 生成的 `__error_handler__*` 私有节点名；框架编译结果仍可暴露这些内部节点。
- 保留现有未跟踪计划 `2026-08-14-native-agent-parent-graph.md`，不回滚用户工作。

---

### Task 1: 用原生列表 reducer 收缩 Planning state 与模型

**Files:**
- Modify: `tests/tdd/native-agent-parent-graph/test_state_channels.py`
- Modify: `tests/tdd/native-agent-parent-graph/test_planning_graph.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/models.py`
- Modify: `src/assistant_agent/native_agent/planning_graph.py`
- Modify: `src/assistant_agent/agent_server/services.py`

**Interfaces:**
- Produces: `PlanningState.worker_results: Annotated[list[WorkerResult], operator.add]`；最小 `NativePlanNode(node_id, objective, depends_on)` 和 `NativePlanProposal(schema_version, nodes)`。
- Removes: `PlanningArtifact`、`VerificationResult`、acceptance/deliverable/constraint DTO、自定义 merge 函数、`completed_work_item_ids`、`verification`、`repair_count`。

- [x] **Step 1: 写 RED 测试**

  将 state channel 测试改为两个 node 依次返回单元素 `worker_results` 列表，并断言 LangGraph 累积为 `[node-a, node-b]`。将 planning 测试改为只提供 `NativePlanProposal`，断言依赖 wave、并行 root、DAG cycle rejection 和 plan 顺序 finalize；移除 verifier/repair/artifact 断言。

- [x] **Step 2: 运行 RED**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/tdd/native-agent-parent-graph/test_state_channels.py \
    tests/tdd/native-agent-parent-graph/test_planning_graph.py
  ```

  预期：旧 dict reducer/model/verifier 接口与新测试不一致而失败。

- [x] **Step 3: 实现最小 state/model/planning graph**

  `WorkerResult` 只保留 `work_item_id` 和 `content`。`PlanningState` 只增加 `plan` 与原生列表 reducer：

  ```python
  worker_results: NotRequired[
      Annotated[list[WorkerResult], operator.add]
  ]
  ```

  planner 初始化 `worker_results=[]`；worker 返回 `[WorkerResult(...)]`；`_ready_worker_sends` 从结果 ID 推导完成集合；join 后无 ready worker 时直接 finalize。finalize 按 proposal nodes 顺序输出标准 `AIMessage`。

- [x] **Step 4: 运行 GREEN**

  重复 Step 2 命令，预期全部通过。

### Task 2: 用 node error handler 与 Command 统一 Memory 降级路由

**Files:**
- Modify: `tests/tdd/native-agent-parent-graph/test_root_graph.py`
- Modify: `tests/tdd/native-agent-parent-graph/test_native_memory.py`
- Modify: `src/assistant_agent/native_agent/memory.py`
- Modify: `src/assistant_agent/native_agent/root_graph.py`

**Interfaces:**
- Produces: `memory_recall_degraded(state, error) -> Command`，更新 degraded snapshot 并 `goto="execution_router"`；`memory_commit_degraded(state, error) -> Command`，不更新答案并 `goto=END`。
- Preserves: recall 最多三次、失败仍进入结构化 fast/planning 分支、commit 失败仍保留最终 `AIMessage`。

- [x] **Step 1: 写 RED 测试**

  更新 root topology 断言，只约束显式 `execution_router` 等项目节点，不再把 LangGraph 生成的 `__error_handler__*` 节点纳入项目拓扑合同；让 `MemoryProbe` 可触发 commit 异常，断言最终答案保留。Memory 单元图通过 `add_node(..., error_handler=...)` 注册两个 handler，不再依赖 commit node 内部吞异常。

- [x] **Step 2: 运行 RED**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/tdd/native-agent-parent-graph/test_root_graph.py \
    tests/tdd/native-agent-parent-graph/test_native_memory.py
  ```

  预期：旧 topology 暴露 recall handler，commit node 内部捕获异常且 handler API 未返回 Command，测试失败。

- [x] **Step 3: 实现 handler 与控制流**

  recall/commit node 保持纯 backend 调用。handler 接收 state 和 `NodeError`，分别返回：

  ```python
  Command(update={"memory_context": (), "memory_status": "degraded"}, goto="execution_router")
  Command(goto=END)
  ```

  父图注册 `execution_router`，正常 recall 静态 edge 指向该节点；router conditional edge 选择 fast/planning。error-handler nodes 仅由 LangGraph 内部调度和 `Command` 离开，不为它们添加静态 edge。

- [x] **Step 4: 运行 GREEN**

  重复 Step 2 命令，预期全部通过。

### Task 3: 同步权威文档并验证整体契约

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`

**Interfaces:**
- Documents: reducer 是 LangGraph 原生 channel 配置；planning 仅保留最小 DAG；Memory error recovery 使用 node error handler + Command；固定父图生命周期和产品降级决策不变。

- [x] **Step 1: 更新 owner authority**

  删除 revision/repair/verifier/artifact provenance 描述，加入 execution router 与 LangGraph 原生 `error_handler + Command` 恢复路径；不得把 reducer、降级恢复或 Command 描述为项目自研层或自研能力。

- [x] **Step 2: 运行 feature 与 core 定向验证**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/tdd/native-agent-parent-graph \
    tests/core/integration/test_runtime_lifecycle.py \
    tests/core/integration/test_memory_lifecycle.py
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent/native_agent
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
  ```

- [x] **Step 3: 完成审计**

  使用 `rg` 确认生产 native planning 不再引用 `repair_count`、`revision`、`VerificationResult`、`PlanningArtifact`、`completed_work_item_ids` 和自定义 merge 函数；检查 diff 未修改无关文件。
