# 原生 LangGraph M2 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M1 的 `AssistantTurnGraph` 升级为最小可序列化、可持久恢复、可作为 planner/worker/verifier 子图复用的公共内核，并由原生 v2 async stream 单向投影产品事件。

**Architecture:** standalone graph 以稳定 conversation `thread_id` 接入官方 async persistent checkpointer；嵌套 profile graph 不自带 saver，由父图继承 checkpoint namespace。checkpoint 只保存版本化 JSON state 和稳定引用，运行对象继续经 `GraphRuntimeContext` 注入；等待输入使用 `interrupt()`，恢复使用同一 thread 上的新 `run_id` 与 `Command(resume=...)`。

**Tech Stack:** Python 3.11+、LangGraph 1.2.4、langgraph-checkpoint 4.1.1、官方 persistent saver package（授权门后确定兼容版本）、Pydantic、stdlib SQLite business ledger、asyncio、pytest。

## Global Constraints

- 本计划只实现主 spec 的 M2，不提前迁移 Workflow v2 DAG，也不删除 Langfuse；M3/M5 边界不前移。
- Agent-Service、媒体 API、Tool 治理、身份隔离与 mock/real 显式隔离保持兼容。
- `thread_id` 对 conversation 稳定；每次 invoke/resume 产生新 `run_id`；不得把 `run_id` 塞入 `thread_id`，不得手工伪造 `checkpoint_ns`。
- saver 只保存 graph 执行事实；Provider、executor/registry、数据库连接、service、sink、callback、cancel token、媒体/artifact 正文不得进入 checkpoint。
- 写 Tool 仍经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`，且恢复重放必须经过 operation barrier。
- pytest 全程 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、local/offline；临时 RED/GREEN 只放 `tests/tdd/native-langgraph-m2/`。
- M1 的真实 LangSmith operator acceptance 仍 pending；M2 不以离线测试冒充真实 Experiment 验收。
- 只提交当前 task 文件，不回滚用户改动；每个 task 由 fresh implementer 完成并经独立 review 后再继续。

## 依赖授权门（实施前必须处理）

当前环境没有 `langgraph-checkpoint-sqlite`、`langgraph-checkpoint-postgres` 或 `aiosqlite`，`pyproject.toml` 也没有直接声明 LangGraph。M2 禁止自研 checkpoint saver，也禁止把安装隐含在测试或脚本中。

1. Tasks 1、3、4、5、6 可先以官方 `InMemorySaver` 完成离线结构和恢复 TDD。
2. 执行 Task 2 的 production backend 前必须暂停并取得用户安装依赖的明确授权。
3. 获授权后，只从官方 saver 包元数据选择与本机 LangGraph 1.2.4 / checkpoint 4.1.1 兼容的 async SQLite saver，并把实际兼容区间显式写入 `pyproject.toml`；不兼容则停止，不降级、自研或静默改用 memory。
4. 未完成官方 persistent saver 的重建进程恢复测试时，只能报告“M2 离线结构完成，持久验收阻塞”，不得报告 M2 完成。

## 文件结构

**新建：**

- `src/assistant_agent/runtime/assistant_graph_state.py`：版本化 checkpoint DTO、JSON 编解码与 fail-closed 校验。
- `src/assistant_agent/runtime/assistant_graph_profiles.py`：`standard/planner/worker/verifier` 的结构化 profile。
- `src/assistant_agent/runtime/assistant_interrupts.py`：interrupt payload、resume command 与等待态判定。
- `src/assistant_agent/runtime/tool_operation_barrier.py`：写副作用的 SQLite 业务幂等/未知态屏障；不是 checkpointer。
- `src/assistant_agent/runtime/product_event_projector.py`：LangGraph stream → 既有 `AgentEvent` 的单向投影。
- `tests/tdd/native-langgraph-m2/`：state、saver、profile、interrupt、barrier、projector、Runtime 纵向测试。

**修改：**

- `pyproject.toml`、`src/assistant_agent/config/__init__.py`、`runtime/checkpointer.py`：官方 async persistent saver 配置与生命周期。
- `runtime/assistant_loop_graph.py`、`assistant_loop_nodes.py`、`graph_runtime.py`、`assistant_graph_app.py`：持久 state、profile 子图、interrupt/resume。
- `runtime/tool_executor.py`、`runtime/runtime.py`、`assistant_run_service.py`、`event_stream.py`：operation barrier、async stream/projector、薄 facade。
- `tests/core/integration/test_runtime_lifecycle.py`、`tests/core/contract/test_tool_contract.py`、`tests/core/INVARIANTS.md`：只回补稳定 M2 契约。
- `docs/runtime-event-stream-architecture.md`、`docs/tool-calling-architecture.md`、`docs/gateway-architecture.md`：当前 authority 对齐。

---

### Task 1: 版本化最小 checkpoint state

**Files:**
- Create: `src/assistant_agent/runtime/assistant_graph_state.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Test: `tests/tdd/native-langgraph-m2/test_checkpoint_state.py`

**Interfaces:**
- Produces: `AssistantTurnState`、`ASSISTANT_GRAPH_NAME = "AssistantTurnGraph"`、`ASSISTANT_GRAPH_VERSION = "2"`、`ASSISTANT_STATE_SCHEMA_VERSION = 1`。
- Produces: `to_checkpoint_state(execution_state: AssistantLoopState) -> AssistantTurnState` 与 `to_execution_state(state: AssistantTurnState) -> AssistantLoopState`。
- Consumes later: graph builder 只把 `AssistantTurnState` 交给 LangGraph；node wrapper 执行前 hydrate，返回前 dehydrate。

- [ ] **Step 1: 写 state RED 测试**

覆盖：完整 assistant/tool trajectory round-trip；`json.dumps(state)` 成功；递归扫描不存在 `ToolExecutor`、adapter、store、sink、cancel token、bytes、Path、callback；只保留媒体/artifact ref；graph/schema version 不匹配抛 `GraphExecutionError(code="graph_state_incompatible")`。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_checkpoint_state.py
```

Expected: FAIL，缺少 `assistant_graph_state`。

- [ ] **Step 3: 实现显式 JSON DTO**

`AssistantTurnState` 至少包含以下恢复事实，不能用 `model_dump()` 整包保存任意 Runtime 对象：

```python
class AssistantTurnState(TypedDict, total=False):
    graph_name: Literal["AssistantTurnGraph"]
    graph_version: Literal["2"]
    state_schema_version: Literal[1]
    profile: Literal["standard", "planner", "worker", "verifier"]
    request: dict[str, JsonValue]
    run: dict[str, JsonValue]
    outputs_by_step: dict[str, dict[str, JsonValue]]
    current_step_index: int
    assistant_output: dict[str, JsonValue] | None
    pending_tool_calls: list[dict[str, JsonValue]]
    assistant_iterations: int
    tool_calls_used: int
    action_tool_calls_used: int
    control_tool_calls_used: int
    run_phase: str
    tool_observations: list[dict[str, JsonValue]]
    pending_interrupt: dict[str, JsonValue] | None
    final_response: dict[str, JsonValue] | None
```

`run` 只编码恢复所需的 identity/status/error/tool history/catalog/context reference；`frozen_memory_context`、client、正文媒体与任意未知对象 fail-closed。node wrapper 的 hydrate/dehydrate 是唯一兼容旧 `AssistantLoopState` 的边界。

- [ ] **Step 4: 运行 GREEN 与 M1 graph tests**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_checkpoint_state.py tests/tdd/native-langgraph-runtime/test_graph_app.py
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/runtime/assistant_graph_state.py src/assistant_agent/runtime/assistant_loop_nodes.py src/assistant_agent/runtime/graph_runtime.py tests/tdd/native-langgraph-m2/test_checkpoint_state.py
git commit -m "refactor(runtime): define persistent assistant graph state"
```

---

### Task 2: 官方 async persistent checkpointer 与生命周期

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/runtime/checkpointer.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Test: `tests/tdd/native-langgraph-m2/test_persistent_checkpointer.py`

**Interfaces:**
- Produces: `AsyncCheckpointerHandle(saver, aclose)`；`create_async_checkpointer(config) -> AsyncCheckpointerHandle | None`。
- Config: `LANGGRAPH_CHECKPOINTER_BACKEND=none|memory|sqlite` 与独立 `LANGGRAPH_CHECKPOINTER_PATH`；production 默认不得把 `memory` 描述为持久。
- Depends on: 顶部“依赖授权门”；未授权时不得执行安装/依赖修改或声称完成。

- [ ] **Step 1: 用户授权后先验证官方兼容矩阵**

用包 metadata/官方文档确认兼容区间，再修改 `pyproject.toml`。若解析会升级/降级现有 LangGraph 1.2.4 或 checkpoint 4.1.1，停止并报告冲突；不得 `pip install` 猜版本。

- [ ] **Step 2: 写 RED**

测试 `none`、显式 memory、SQLite async saver；SQLite 用 `tmp_path`，跨 `AgentGraphRuntime.close()/重建` 后相同 thread 能 `aget_state()`；路径父目录安全创建；缺官方包时报结构化 startup error 而非回退 memory；`aclose()` 恰好一次。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_persistent_checkpointer.py
```

Expected: FAIL，factory 尚不支持 sqlite/async lifecycle。

- [ ] **Step 4: 实现官方 saver adapter**

factory 只负责官方 saver 构造与 close，不实现 `BaseCheckpointSaver` 子类。Runtime application root 持有 handle 并在 async shutdown 关闭；同步 `close()` 只作兼容入口，不能在活动 loop 中嵌套 `asyncio.run()`。

- [ ] **Step 5: 运行 GREEN 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_persistent_checkpointer.py
git add pyproject.toml src/assistant_agent/config/__init__.py src/assistant_agent/runtime/checkpointer.py src/assistant_agent/runtime/runtime.py tests/tdd/native-langgraph-m2/test_persistent_checkpointer.py
git commit -m "feat(runtime): add official persistent graph checkpointer"
```

---

### Task 3: standalone graph 与可继承 saver 的 profile subgraph

**Files:**
- Create: `src/assistant_agent/runtime/assistant_graph_profiles.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Test: `tests/tdd/native-langgraph-m2/test_graph_profiles.py`

**Interfaces:**
- Produces: `AssistantGraphProfileName = Literal["standard", "planner", "worker", "verifier"]` 和 immutable `AssistantGraphProfile`。
- Produces: `AssistantTurnGraphApp.graph_for_profile(profile) -> CompiledStateGraph`；standalone graph compile 时接 saver，profile child compile 时 `checkpointer=None`，由未来父图继承。

- [ ] **Step 1: 写 RED**

证明四个 profile 是结构化选择而非文本关键词；每个 compiled child 名称稳定；父 probe graph 嵌入 worker child 后 `subgraphs=True` stream 出现原生 namespace；child 没有独立 saver；profile 只能收窄 Tool catalog/预算，不能绕过治理。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_graph_profiles.py
```

- [ ] **Step 3: 实现最小 profile family**

```python
@dataclass(frozen=True)
class AssistantGraphProfile:
    name: AssistantGraphProfileName
    max_tool_iterations: int
    max_control_tool_iterations: int
    allowed_categories: frozenset[ToolCategory]
```

profile 来自可信调用参数/runtime context，并写入 checkpoint state 与 LangSmith metadata；禁止从 request text 推断。M2 不为 profile 复制 node 或 loop。

- [ ] **Step 4: 运行 GREEN 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_graph_profiles.py tests/tdd/native-langgraph-runtime/test_langsmith_native.py
git add src/assistant_agent/runtime/assistant_graph_profiles.py src/assistant_agent/runtime/assistant_loop_graph.py src/assistant_agent/runtime/assistant_graph_app.py src/assistant_agent/runtime/graph_runtime.py tests/tdd/native-langgraph-m2/test_graph_profiles.py
git commit -m "feat(runtime): expose reusable assistant graph profiles"
```

---

### Task 4: 原生 interrupt、resume、state history

**Files:**
- Create: `src/assistant_agent/runtime/assistant_interrupts.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Test: `tests/tdd/native-langgraph-m2/test_interrupt_resume.py`

**Interfaces:**
- Produces: `AssistantInterrupt` Pydantic union、`AssistantResume` Pydantic union。
- Produces: `AssistantTurnGraphApp.aresume(*, identity, context, resume) -> GraphStreamResult`、`aget_state(identity)`、`aget_state_history(identity, limit)`。
- `aresume` 必须调用 `astream(Command(resume=resume.model_dump(mode="json")), ...)`，同 thread/new run。

- [ ] **Step 1: 写 RED**

用 explicit trusted interrupt fixture 证明首次运行结束为 `interrupted` 而非 completed/failed；snapshot `next/tasks/interrupts` 可读；重建 app 后同 thread resume；interrupt 前 node 可能重跑但不可重复副作用；错 thread、无 pending interrupt、schema/version 不兼容均 fail-closed。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_interrupt_resume.py
```

- [ ] **Step 3: 实现 interrupt node 与结果分类**

`interrupt()` 只出现在无不可重复副作用的 gate node；`GraphStreamResult` 增加 `status: completed|interrupted`、`interrupts` 和 checkpoint config。stream 结束时若 root values 携带 interrupt，不再误判 `graph_final_state_missing`。

- [ ] **Step 4: 运行 GREEN 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_interrupt_resume.py tests/tdd/native-langgraph-runtime/test_graph_app.py
git add src/assistant_agent/runtime/assistant_interrupts.py src/assistant_agent/runtime/assistant_loop_graph.py src/assistant_agent/runtime/assistant_loop_nodes.py src/assistant_agent/runtime/assistant_graph_app.py tests/tdd/native-langgraph-m2/test_interrupt_resume.py
git commit -m "feat(runtime): add native assistant interrupt resume"
```

---

### Task 5: 可恢复写 Tool 的 operation barrier

**Files:**
- Create: `src/assistant_agent/runtime/tool_operation_barrier.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Modify: `src/assistant_agent/runtime/tool_executor.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Test: `tests/tdd/native-langgraph-m2/test_tool_operation_barrier.py`

**Interfaces:**
- Produces: `ToolOperationStore.reserve/commit_success/commit_failure/load` 与 stdlib SQLite 实现。
- Operation key: SHA-256(`thread_id`, profile, provider tool-call ID, canonical tool name, normalized input digest)；不使用随机 attempt/run ID。
- 状态：`reserved|succeeded|failed|outcome_unknown`；相同 key 不同 digest fail-closed。

- [ ] **Step 1: 写 RED**

覆盖 read Tool 不进 barrier；write/dangerous 首次只执行一次；checkpoint replay 返回已提交的结构化 `ToolResult`；进程在外部调用后、commit 前中断时后续变为 `outcome_unknown` 且不重放；声明 runtime `idempotency_key` binding 的 Tool 可用同 key安全重试；并发 reserve 只有一个 owner。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_tool_operation_barrier.py
```

- [ ] **Step 3: 在 ToolExecutor 内实现 barrier**

barrier 位于 validation 之后、`ToolExecutionBackend.run()` 之前，不能包在 Registry 外侧或从 checkpoint 推断副作用成功。SQLite 是业务幂等/审计事实，不实现 LangGraph saver；payload 仅保存安全 digest、状态、稳定 output ref 和可恢复的结构化结果。

- [ ] **Step 4: 运行 GREEN 与 TOOL-001**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_tool_operation_barrier.py tests/core/contract/test_tool_contract.py
```

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/runtime/tool_operation_barrier.py src/assistant_agent/runtime/graph_runtime.py src/assistant_agent/runtime/tool_executor.py src/assistant_agent/runtime/runtime.py tests/tdd/native-langgraph-m2/test_tool_operation_barrier.py
git commit -m "feat(tools): guard resumable side effects by operation key"
```

---

### Task 6: v2 stream 驱动的 ProductEventProjector

**Files:**
- Create: `src/assistant_agent/runtime/product_event_projector.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/events.py`
- Test: `tests/tdd/native-langgraph-m2/test_product_event_projector.py`

**Interfaces:**
- Produces: `ProductEventProjector.project(part: GraphStreamPart) -> tuple[AgentEvent, ...]`。
- Projected facts limited to run started、text delta、governed Tool/product progress、waiting input、final、cancelled、failed；`checkpoints/tasks/完整 state/namespace` 不进入公共 payload。

- [ ] **Step 1: 写 RED**

输入真实 v2 `updates/messages/custom/tasks/checkpoints` fixture，断言只投影 allowlist；同一 occurrence 不重复；interrupt → waiting input；projector 不调用 graph、router、store update 或 workflow service。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_product_event_projector.py
```

- [ ] **Step 3: 让 Runtime 边消费边投影**

`AssistantTurnGraphApp.astream()` 保持原生事实流；Runtime async path `async for part` 收集 root final/interrupt 并立即投影。删除 `_emit_graph_execution_event()` 对整个 graph 的模拟 `graph_node_started/finished`，不得用 projector 决定下一节点。

- [ ] **Step 4: 运行 GREEN 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_product_event_projector.py tests/tdd/native-langgraph-runtime/test_async_runtime.py
git add src/assistant_agent/runtime/product_event_projector.py src/assistant_agent/runtime/assistant_graph_app.py src/assistant_agent/runtime/runtime.py src/assistant_agent/runtime/events.py tests/tdd/native-langgraph-m2/test_product_event_projector.py
git commit -m "refactor(runtime): project product events from graph stream"
```

---

### Task 7: Runtime facade、Service/Gateway resume 兼容纵切

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_run_service.py`
- Modify: `src/assistant_agent/runtime/event_stream.py`
- Modify: `src/assistant_agent/gateway/runtime_adapter.py`
- Modify: `src/assistant_agent/gateway/runtime_event_mapping.py`
- Test: `tests/tdd/native-langgraph-m2/test_runtime_resume.py`
- Test: `tests/tdd/native-langgraph-m2/test_gateway_compatibility.py`

**Interfaces:**
- Produces: `AgentGraphRuntime.aresume_state(request, *, resume, run_id, ...) -> AgentState` 与 `astream_state(...)`；同步 `run_state()` 仅保留兼容，不作为 service 主路。
- Gateway 继续拥有 cancel/disconnect/replacement；Graph 拥有 waiting execution position。M2 只接已有结构化 interrupt/input 边界，不新增公共 wire 字段。

- [ ] **Step 1: 写 RED**

证明 service/Gateway 主路不调用 `invoke()`、`asyncio.to_thread()` 或 `run_in_executor()` 执行 graph；interrupt 不发 completed；resume 恰好一个 terminal；disconnect 只停止订阅不删除 checkpoint；cancel 与 interrupt 分离；现有 Agent-Service/media frames 快照不变。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_runtime_resume.py tests/tdd/native-langgraph-m2/test_gateway_compatibility.py
```

- [ ] **Step 3: 收缩 facade**

prepare/context/finalize 继续共享，但执行输入明确为 `AssistantTurnState | Command`；waiting run 不执行 terminal memory/history commit，resume terminal 才 commit。删除 service/runtime 内模拟 graph lifecycle 和 graph execution thread bridge；Gateway pool 自身同步资源 checkout bridge 不属于 graph 执行，不能机械删除。

- [ ] **Step 4: 运行 GREEN 与受保护协议测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2 tests/tdd/native-langgraph-runtime tests/core/contract/test_gateway_contract.py tests/core/integration/test_runtime_lifecycle.py
```

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/runtime/runtime.py src/assistant_agent/runtime/assistant_run_service.py src/assistant_agent/runtime/event_stream.py src/assistant_agent/gateway/runtime_adapter.py src/assistant_agent/gateway/runtime_event_mapping.py tests/tdd/native-langgraph-m2/test_runtime_resume.py tests/tdd/native-langgraph-m2/test_gateway_compatibility.py
git commit -m "refactor(runtime): resume assistant graph through thin async facade"
```

---

### Task 8: Core 契约、authority、删除门槛与验收

**Files:**
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/contract/test_tool_contract.py`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/gateway-architecture.md`
- Create: `.superpowers/sdd/2026-08-12-native-langgraph-m2/m2-final-report.md`

**Core invariant:** `RUN-001` 增加 interrupted/resume 同义终态；`LOOP-001` 增加 versioned persistent state/profile；`IDENT-001` 删除 M1 root saver 临时说明并增加 stable thread/new run resume；`TOOL-001` 增加 resumable write operation barrier；`GATE-001` 外部协议保持不变。`DUR-001` 的 Workflow scheduler 迁移不属于 M2。

- [ ] **Step 1: 先做 mutation RED，再写最小 core assertion**

只把上述跨实现稳定结构晋升 core；profile 具体预算、第三方 saver 内部字段、prompt、完整 checkpoint snapshot 留在临时 TDD。

- [ ] **Step 2: 更新 authority 与最终报告**

报告必须列出实际 saver 包/版本、重建恢复证据、未运行的真实 LangSmith operator 项、临时 TDD 可由用户手动整目录删除，并明确 M3 接管 Workflow DAG。

- [ ] **Step 3: 运行验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2 tests/tdd/native-langgraph-runtime
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_runtime_lifecycle.py tests/core/contract/test_tool_contract.py tests/core/contract/test_gateway_contract.py
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent
git diff --check
```

Expected: 全部 PASS；无真实 Provider/network 调用。

- [ ] **Step 4: 运行删除门槛**

```bash
! rg -n "_emit_graph_execution_event|asyncio\.to_thread\([^)]*(run_state|invoke)|run_in_executor\([^)]*(run_state|invoke)" src/assistant_agent/runtime src/assistant_agent/api src/assistant_agent/gateway
! rg -n "class .*CheckpointSaver|BaseCheckpointSaver" src/assistant_agent
rg -n "interrupt\(|Command\(resume=|subgraphs=True|stream_mode=.*checkpoints" src/assistant_agent/runtime
```

必须人工确认：不存在自研 saver；checkpoint 无运行对象/正文媒体；普通 turn 和 profile child 使用真实 graph 层级；模拟 graph started/finished 已退出；M1 LangSmith native tree 未被 projector 重建。

- [ ] **Step 5: 真实 LangSmith operator 验收（显式授权后）**

对 standard、worker child、interrupt/resume 各运行一个真实样例，确认同一 trace/tree 中 graph/node/subgraph/LLM/governed Tool 层级、thread/run metadata 和 interrupted/completed 区分。未授权则报告 pending，不阻塞离线代码提交，但阻塞“M2 全面验收通过”的声明。

- [ ] **Step 6: 提交**

```bash
git add tests/core/INVARIANTS.md tests/core/integration/test_runtime_lifecycle.py tests/core/contract/test_tool_contract.py docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md docs/gateway-architecture.md .superpowers/sdd/2026-08-12-native-langgraph-m2/m2-final-report.md
git commit -m "docs: establish native LangGraph M2 contracts"
```

## 里程碑删除清单

M2 完成时必须删除或冻结：

- Runtime 对整张 graph 人工发出的 `graph_node_started/graph_node_finished`；
- service/API/Gateway 主 graph 路径上的 `invoke() + asyncio.to_thread/run_in_executor`；
- M1“传入 saver 但保持空 storage”的临时行为与对应测试；
- 把 interrupt 当异常/自然语言 completed 的兼容分支；
- checkpoint 中的 runtime object 或正文 payload；
- 可恢复 write Tool 在 operation barrier 外直接执行的路径。

不得在 M2 删除：Workflow v2 scheduler（M3/M4）、Langfuse（M5）、Gateway connection/delivery/cancel ownership、媒体 API、安全审计。

## 最终自审

- Spec coverage: M2 的 state、persistent checkpoint、profile/subgraph、interrupt/resume、trajectory/幂等、async stream/projector 与旧桥删除均有独立 task。
- Type consistency: `AssistantTurnState`、`AssistantGraphProfileName`、`GraphExecutionIdentity`、`GraphStreamResult`、`AssistantInterrupt/Resume`、`ToolOperationStore` 在首次出现处定义，后续名称一致。
- Dependency reality: official persistent saver 未安装且未经授权；计划没有安装命令、自研 fallback 或完成假设。
- Scope: Workflow DAG/`Send`/join/repair 留给 M3；Langfuse 退出留给 M5。
