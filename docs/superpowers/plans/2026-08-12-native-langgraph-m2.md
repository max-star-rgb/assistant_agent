# 原生 LangGraph M2 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M1 的 `AssistantTurnGraph` 升级为最小可序列化、可持久恢复、可作为 planner/worker/verifier 子图复用的公共内核，并由原生 v2 async stream 单向投影产品事件。

**Architecture:** standalone graph 以稳定 conversation `thread_id` 接入官方 async persistent checkpointer；嵌套 profile graph 不自带 saver，由父图继承 checkpoint namespace。checkpoint 只保存版本化 JSON state 和稳定引用，运行对象继续经 `GraphRuntimeContext` 注入；等待输入使用 `interrupt()`，恢复使用同一 thread 上的新 `run_id` 与 `Command(resume=...)`。

**Tech Stack:** Python 3.11+、LangGraph 1.2.4、langgraph-checkpoint 4.1.1、官方 persistent saver package（授权门后确定兼容版本）、Pydantic、stdlib SQLite business ledger、asyncio、pytest。

## Global Constraints

- 全程以 Graph API 原生语义为执行事实源：`StateGraph/State/Node/Edge/START/END/Conditional Edge`、
  `Command/Send/Reducer/Subgraph`、Pregel super-step、compile/invoke/stream、checkpoint/checkpointer/thread、
  interrupt/resume、memory/store/runtime context、retry/timeout/fallback、streaming modes 与
  time-travel/replay/fork；本里程碑只实施其中属于 M2 的部分，但不得新增与后续原生能力重叠的自研层。
- 本计划只实现主 spec 的 M2，不提前迁移 Workflow v2 DAG，也不删除 Langfuse；M3/M5 边界不前移。
- Agent-Service、媒体 API、Tool 治理、身份隔离与 mock/real 显式隔离保持兼容。
- M2 的 `interrupt/resume/waiting_user` 只作为内部 compiled graph / `AgentGraphRuntime` API；现有 Agent-Service、Gateway、HTTP 和媒体 wire 不接入、不投影 waiting/resume，也不能获得一个会返回内部 waiting 的 composition root。M3 Durable Workflow 再定义产品 waiting-input 投影。
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
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Create: `tests/tdd/native-langgraph-m2/state_inventory.md`
- Test: `tests/tdd/native-langgraph-m2/test_checkpoint_state.py`

**Interfaces:**
- Produces: `AssistantTurnState`、`ASSISTANT_GRAPH_NAME = "AssistantTurnGraph"`、`ASSISTANT_GRAPH_VERSION = "2"`、`ASSISTANT_STATE_SCHEMA_VERSION = 1`。
- Produces: strict `AssistantTurnState` 及 `AssistantNodeInput/AssistantNodeUpdate` adapter；compiled `StateGraph` 的 schema 直接改为 `AssistantTurnState`。
- Migration closure: node adapter 可在单个 node 调用期间 hydrate 临时 `AgentState/UserRequest/ToolResult`，但每个 node return 前必须投影回 strict DTO；Task 1 终态 compiled checkpoint 不再存在 legacy `AssistantLoopState` snapshot，后续 task 不允许恢复双 state 轨。

- [ ] **Step 1: 建立 node read/write inventory**

逐一列出 `assistant_node`、`execute_requested_tool_node`、`compose_response_node`、route 与 Runtime finalize 对现有 `AssistantLoopState`/`AgentState` 的读取、原地写入和返回字段，写入 `tests/tdd/native-langgraph-m2/state_inventory.md`。inventory 必须把完整恢复闭包映射到 strict DTO：request messages/text/media refs、`response_style`、`task_execution_mode`、runtime task facts、identity/status/errors、context/perception/memory refs、capability refs/grants、run Tool catalog、assistant output、pending calls、Tool observations/results、response/citations/artifact refs、phase/counters/stream boundary facts。任何未映射字段必须证明是可从 runtime context 重建或明确删除，不能塞入 generic metadata/data。

- [ ] **Step 2: 写 state RED 测试**

覆盖：真实 compiled graph 用 `InMemorySaver` 执行一轮后，直接调用 saver `aget_tuple()`/graph `aget_state()` 检查 checkpoint `channel_values`；递归断言不存在 `AgentState`、`UserRequest`、`ToolResult`、`ToolExecutor`、adapter、store、sink、cancel token、bytes、Path、callback，且 `json.dumps(values)` 成功。再覆盖媒体/artifact 只保存稳定 ref、version 不匹配 fail-closed，以及同一 stable thread 开启新 turn 时旧 observation/pending call/final/error/counter 全部被显式清空。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_checkpoint_state.py
```

Expected: FAIL，缺少 `assistant_graph_state`。

- [ ] **Step 4: 实现显式 JSON DTO**

不得保留 `request: dict[str, JsonValue]`、`run: dict[str, JsonValue]` 或其他任意字典逃生舱。定义有长度上限的 nested Pydantic DTO（所有 model `extra="forbid"`），再由 TypedDict 作为 LangGraph channel schema：

```python
class PersistedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    text: str | None = Field(default=None, max_length=32_000)
    assistant_mode: Literal["standard", "deep_research"]
    response_style: PersistedResponseStyle
    task_execution_mode: Literal["auto", "foreground", "durable"]
    messages: tuple[PersistedMessage, ...] = Field(max_length=128)
    runtime_task_facts: PersistedRuntimeTaskFacts | None
    media_refs: tuple[PersistedMediaRef, ...] = Field(max_length=16)
    capability_refs: tuple[str, ...] = Field(max_length=64)

class PersistedRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    status: Literal["created", "running", "waiting_user", "completed", "failed", "cancelled"]
    errors: tuple[PersistedError, ...] = Field(max_length=32)
    tool_calls: tuple[PersistedToolCall, ...] = Field(max_length=64)
    tool_results: tuple[PersistedToolResult, ...] = Field(max_length=64)

class AssistantTurnState(TypedDict):
    graph_name: Literal["AssistantTurnGraph"]
    graph_version: Literal["2"]
    state_schema_version: Literal[1]
    profile: Literal["standard", "planner", "worker", "verifier"]
    request: PersistedRequest
    run: PersistedRun
    outputs_by_step: tuple[PersistedStepOutput, ...]
    current_step_index: int
    assistant_output: PersistedAssistantOutput | None
    pending_tool_calls: tuple[PersistedToolCallRequest, ...]
    assistant_iterations: int
    tool_calls_used: int
    action_tool_calls_used: int
    control_tool_calls_used: int
    run_phase: str
    tool_observations: tuple[PersistedToolObservation, ...]
    context_refs: tuple[PersistedContextRef, ...]
    capability_refs: tuple[str, ...]
    catalog: PersistedRunToolCatalog
    pending_interrupt: PersistedInterrupt | None
    final_response: PersistedResponse | None
```

每个 nested DTO 明列 primitive/enum/有界 tuple/稳定 reference 字段；Tool result 只保留 safe observation、status、operation key、output/artifact refs，不保存任意 `data/audit_payload/raw response`。转换器只能逐字段构造，禁止 `AgentState.model_dump()`、`UserRequest.model_dump()` 或递归通用 JSON sanitizer。Graph input builder 必须为每个 channel 提供显式初值，因此 stable thread 的新 turn 会覆盖并清空全部 run-scoped channel，而不是与上个 checkpoint 合并。测试还必须分别在 assistant node 后、Tool 执行边界前后、compose 前重建 app 并继续，证明 Provider messages、catalog、observation、response style/runtime task facts 和终态与不中断 baseline 等价。

- [ ] **Step 5: 运行 GREEN 与 M1 graph tests**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_checkpoint_state.py tests/tdd/native-langgraph-runtime/test_graph_app.py
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/runtime/assistant_graph_state.py src/assistant_agent/runtime/assistant_loop_graph.py src/assistant_agent/runtime/assistant_loop_nodes.py src/assistant_agent/runtime/assistant_graph_app.py src/assistant_agent/runtime/graph_runtime.py src/assistant_agent/runtime/runtime.py tests/tdd/native-langgraph-m2/state_inventory.md tests/tdd/native-langgraph-m2/test_checkpoint_state.py
git commit -m "refactor(runtime): define persistent assistant graph state"
```

---

### Task 2: 官方 async persistent checkpointer 与生命周期

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/runtime/checkpointer.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `src/assistant_agent/runtime/runtime_host.py`
- Modify: `src/assistant_agent/runtime/assistant_run_service.py`
- Modify: `src/assistant_agent/gateway/runtime_pool.py`
- Modify: `src/assistant_agent/api/routes_agent.py`
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/api/gateway_runtime.py`
- Modify: `src/assistant_agent/mcp/server.py`
- Modify: `src/assistant_agent/multi_agent/agent_router.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Test: `tests/tdd/native-langgraph-m2/test_persistent_checkpointer.py`

**Interfaces:**
- Produces: `@asynccontextmanager open_async_checkpointer(config) -> AsyncIterator[AsyncCheckpointerHandle | None]`、`async create_async_runtime(...) -> AgentGraphRuntime` / `AssistantRuntimeApp.astart()`；`AssistantTurnGraphApp(checkpointer_handle=...)` 把 `handle.saver` 传给唯一一次 `build_assistant_loop_graph(checkpointer=saver)` 编译。
- Config: `LANGGRAPH_CHECKPOINTER_BACKEND=none|memory|sqlite` 与独立 `LANGGRAPH_CHECKPOINTER_PATH`；依赖门通过后的 composition root 默认 `sqlite` + `.local/langgraph/assistant_turns.sqlite3`，`memory` 仅供显式 local/TDD，`none` 仅供无恢复专项测试。production 缺 saver package/path readiness 必须启动失败，不得回退 memory。
- Depends on: 顶部“依赖授权门”；未授权时不得执行安装/依赖修改或声称完成。

- [ ] **Step 1: 用户授权后先验证官方兼容矩阵**

用包 metadata/官方文档确认兼容区间，再修改 `pyproject.toml`。若解析会升级/降级现有 LangGraph 1.2.4 或 checkpoint 4.1.1，停止并报告冲突；不得 `pip install` 猜版本。

- [ ] **Step 2: 写 RED**

测试 `none`、显式 memory、SQLite async saver；断言 app compiled graph 绑定同一个 saver instance；SQLite 跨 Runtime 重建可恢复；缺包 fail-closed；application root 的 `aclose()` 顺序正确。逐入口补 startup/shutdown 兼容测试：FastAPI lifespan/routes、Gateway runtime pool、`assistant_run_service.create_runtime` 调用方、Offline MCP server、multi-agent router 都从同一进程级 `AssistantRuntimeApp` async owner checkout Runtime，shutdown 归还 lease 并最终只关闭 saver 一次。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_persistent_checkpointer.py
```

Expected: FAIL，factory 尚不支持 sqlite/async lifecycle。

- [ ] **Step 4: 实现官方 saver adapter**

factory 只负责官方 saver async context，不实现 saver。`AssistantRuntimeApp.astart()/aclose()` 成为进程 owner，并提供 async checkout/checkin；FastAPI lifespan、Gateway pool、MCP server 和 multi-agent router 的 async startup/shutdown 均持有/借用这个 owner，不再各自 `AgentGraphRuntime()`。现有同步 `create_runtime()`/constructor 继续作为显式 offline/test 兼容，用 `none`/`InMemorySaver`，不能因为默认 config 为 sqlite 而让当前产品 import/构造直接报错；production entry 在处理请求前必须完成 async app startup。禁止后台 event-loop thread、嵌套 `asyncio.run()` 或用 thread bridge 包 graph。

- [ ] **Step 5: 运行 GREEN 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_persistent_checkpointer.py
git add pyproject.toml src/assistant_agent/config/__init__.py src/assistant_agent/runtime/checkpointer.py src/assistant_agent/runtime/assistant_graph_app.py src/assistant_agent/runtime/assistant_loop_graph.py src/assistant_agent/runtime/assistant_runtime_app.py src/assistant_agent/runtime/runtime_host.py src/assistant_agent/runtime/assistant_run_service.py src/assistant_agent/gateway/runtime_pool.py src/assistant_agent/api/routes_agent.py src/assistant_agent/api/app.py src/assistant_agent/api/gateway_runtime.py src/assistant_agent/mcp/server.py src/assistant_agent/multi_agent/agent_router.py src/assistant_agent/runtime/runtime.py tests/tdd/native-langgraph-m2/test_persistent_checkpointer.py
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
- Produces: `ProfileInvocationInput` / `ProfileInvocationResult` 及 `profile_input_adapter(parent_state, assignment) -> AssistantTurnState`、`profile_output_adapter(child_state) -> ProfileInvocationResult`；父图不得把自己的整份 state 直接传入 child。

- [ ] **Step 1: 写 RED**

证明四个 profile 是结构化选择而非文本关键词；每个 compiled child 名称稳定；父 probe graph 通过 input adapter 只传 assignment refs/objective/constraints/capability refs，child output adapter 只返回 response/tool trajectory/artifact refs/status；父 workflow 私有字段不会出现在 child checkpoint。`subgraphs=True` stream 出现原生 namespace且 child 没有独立 saver；namespace 中动态 task UUID 只用于 graph 定位/trace，绝不保存或投影为业务 work-item ID；profile 只能收窄 Tool catalog/预算，不能绕过治理。

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

profile 来自可信调用参数/runtime context，并写入 checkpoint state 与 LangSmith metadata；禁止从 request text 推断。`profile_input_adapter` 在建图输入时把 Registry catalog、Provider tool specs 和 validator allowed set 同时与 `allowed_categories/explicit tool allowlist` 取交集，profile 不可只改提示词或预算；Executor 仍做最终治理。resume 必须读取 checkpoint profile 并与调用 profile 相等，不传则沿用 checkpoint；任何切换以 `graph_profile_mismatch` fail-closed。M2 不为 profile 复制 node 或 loop。

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
- Produces: trusted `AssistantInterruptRequest(kind, prompt, action_ref, allowed_resume_kinds)`、public-safe `AssistantInterrupt`、strict `AssistantResume` Pydantic union。
- Produces: `AssistantTurnGraphApp.aresume(*, identity, context, resume) -> GraphStreamResult`、`aget_state(identity)`、`aget_state_history(identity, limit)`。
- `aresume` 必须调用 `astream(Command(resume=resume.model_dump(mode="json")), ...)`，同 thread/new run。

- [ ] **Step 1: 写 RED**

通过 Runtime 显式参数（以及未来父 graph adapter）注入 trusted `interrupt_request`，不得从用户文本/任意 metadata 推断。真实图路径必须可达：`assistant -> await_input -> execute_tool|assistant`；首次运行在 `await_input` 结束为 `interrupted`，snapshot `next/tasks/interrupts` 可读；重建 app 后同 thread resume。再覆盖：resume kind/action_ref 不匹配被拒绝；成功 resume 原子清除 `pending_interrupt` 并不会再次 gate；错 thread、无 pending interrupt、schema/version 不兼容均 fail-closed。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_interrupt_resume.py
```

- [ ] **Step 3: 实现 interrupt node 与结果分类**

增加真实 `await_input_node` 与 conditional edges：仅当 Runtime/未来父图显式传入 trusted `interrupt_request`，或既有结构化 Tool policy 明确声明 `requires_approval` 时，assistant 才路由到 `await_input`；绝不因为 category 是 write/dangerous 就自动 HITL，也不得从用户文本/description/Tool 名推断审批。节点先校验 trusted request，再调用 `interrupt(public_payload)`，恢复值经 `AssistantResume` 和 action_ref allowlist 校验后清除 pending state，才路由到 governed Tool/assistant。`interrupt()` 前不允许执行对应写 Tool、artifact publish 或 delivery。

`GraphStreamResult` 增加 `status: completed|interrupted`、按 `Interrupt.id` 去重后的 `interrupts` 和 checkpoint config。LangGraph 1.2.4 在 interrupt 时 root `values` 仍会出现，且同一 child interrupt 会同时出现在 child/root stream；因此不能以“存在 root values”判断 completed，也不能按 namespace/动态 task UUID 去重。消费流后必须调用 `aget_state(config, subgraphs=True)`：`tasks/next` 或 task interrupt 非空且 Interrupt.id 存在即为 interrupted；只有 `next == ()`、tasks 无 pending/interrupt 且 state terminal 才是 completed。Runtime 将 interrupted invocation 投影为 `AgentState.status="waiting_user"`，它是可恢复非终态：不得写 completed/failed/cancelled terminal history、不得释放 thread checkpoint、不得触发 terminal Tool hook；resume 完成或显式 cancel 后才终结。

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
- Stable identity source: Runtime 在 tool edge 之前用 `thread_id + turn_origin_id + assistant_iteration + call_ordinal + canonical tool name + normalized input digest` 确定性生成 `operation_scope_id`，先作为 pending call 写入 checkpoint，之后才允许副作用；不同 turn 的 `turn_origin_id` 不同，同一 checkpoint/resume/逻辑重放复用原 scope。Provider native tool-call ID 有则原样保存为 correlation，但不作为可恢复性的强制前提。
- Operation key: SHA-256(`thread_id`, `operation_scope_id`, profile, canonical tool name)；normalized input 单独保存 digest并冲突校验，不使用 resume `run_id`、attempt、namespace 或动态 task UUID。
- 状态：`reserved|invoking|succeeded|failed|outcome_unknown`；相同 key 不同 digest fail-closed。

- [ ] **Step 1: 写 RED**

覆盖 read Tool 不进 barrier；无 Provider ID 的 call 仍获得稳定 scope，Provider ID 有则跨 checkpoint 保留；write/dangerous 首次只执行一次；checkpoint replay 返回已提交结果；backend 返回前/commit 前崩溃均变 `outcome_unknown` 且不自动重放；仅当 Tool 声明并实际绑定同一个业务 `idempotency_key`、且 backend 提供查询/幂等重放契约时才允许 reconcile；并发 reserve 只有一个 owner。不中断 baseline 与 `interrupt -> app/runtime 重建 -> resume` 的 Provider messages、Tool 调用次数/顺序/参数/业务 idempotency key、observation 和终态必须等价。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_tool_operation_barrier.py
```

- [ ] **Step 3: 在 ToolExecutor 内实现 barrier**

barrier 位于 validation 之后、`ToolExecutionBackend.run()` 之前。`reserve_and_mark_invoking` 必须在同一 SQLite transaction 中原子完成，避免 crash 留下无法判断是否进入 backend 的 `reserved`；之后是 `backend.run(含全部既有 retry) -> commit_success|commit_failure`。一次逻辑 Tool call 的全部 retry 共用同 operation 和同业务 `idempotency_key`，retry 不 reserve 新 operation。只有 `succeeded/failed` 可重放结果；重建时遗留 `invoking` 一律 CAS 为 `outcome_unknown`。SQLite 是 business idempotency/audit 事实，不实现 saver；默认独立 `.local/langgraph/tool_operations.sqlite3`。

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
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Modify: `src/assistant_agent/runtime/event_publisher.py`
- Modify: `src/assistant_agent/runtime/llm_event_mapping.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/events.py`
- Test: `tests/tdd/native-langgraph-m2/test_product_event_projector.py`

**Interfaces:**
- Produces: strict `RuntimeProductFact` union；graph 内 producer 调用 `emit_product_fact(writer, fact)` 只负责写 custom stream；graph 外 ingress/terminal 直接把 fact 交给 `ProductEventProjector.project_fact(fact)`。`ProductEventProjector.project_part(part)` 仅解析 custom part 后委托同一个 `project_fact`，两条路径共享同一有界 `fact_id` dedupe，projector 是唯一 public mapping。
- Projected facts limited to run started、text delta、governed Tool/product progress、waiting input、final、cancelled、failed；`checkpoints/tasks/完整 state/namespace` 不进入公共 payload。
- Fact ownership: graph node/LLM callback/ToolExecutor 不再直接向产品 `EventSink` 写 `AgentEvent`；它们通过当前 LangGraph stream writer 写带 `fact_id` 的 `custom` `RuntimeProductFact`。Runtime ingress 的 run-started 也先构造成同 union 再交 projector。`ProductEventProjector` 是唯一 `AgentEvent` 构造与 sink owner；canonical TraceEvent/业务审计继续走各自 store，不经过 projector。

- [ ] **Step 1: 写 RED**

输入真实 compiled graph 的 v2 stream，不只喂手写 fixture：断言 node/LLM/Tool 每个 `fact_id` 在 stream 中 exactly once、projector 输出 exactly once，tasks/checkpoints/updates 不被猜测成进度；direct `EventSink.emit(AgentEvent(...))` 在 node/LLM/Tool 路径的 mutation test 必须失败。内部 interrupt fact 可供内部 Runtime 测试，但 service composition root 不消费/投影 waiting。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_product_event_projector.py
```

- [ ] **Step 3: 让 Runtime 边消费边投影**

`AssistantTurnGraphApp.astream()` 保持原生事实流；Runtime async path 对 custom part 调 `project_part`。把 Provider delta callback 和 Tool lifecycle publisher 改为 `emit_product_fact(writer, fact)`；run start/final/cancel/fail 是 graph 外事实，调用同一 projector 的 `project_fact`，从而与 custom stream 共用 exactly-once dedupe。删除 direct product `EventSink` 分支及模拟 graph lifecycle。Projector 不推断路由；内部 waiting 不进入 service root。

- [ ] **Step 4: 运行 GREEN 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_product_event_projector.py tests/tdd/native-langgraph-runtime/test_async_runtime.py
git add src/assistant_agent/runtime/product_event_projector.py src/assistant_agent/runtime/assistant_graph_app.py src/assistant_agent/runtime/assistant_loop_nodes.py src/assistant_agent/runtime/graph_runtime.py src/assistant_agent/runtime/event_publisher.py src/assistant_agent/runtime/llm_event_mapping.py src/assistant_agent/runtime/runtime.py src/assistant_agent/runtime/events.py tests/tdd/native-langgraph-m2/test_product_event_projector.py
git commit -m "refactor(runtime): project product events from graph stream"
```

---

### Task 7: 内部 Runtime resume facade 与外部入口隔离

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_run_service.py`
- Modify: `src/assistant_agent/runtime/event_stream.py`
- Test: `tests/tdd/native-langgraph-m2/test_runtime_resume.py`
- Test: `tests/tdd/native-langgraph-m2/test_gateway_compatibility.py`
- Test: `tests/tdd/native-langgraph-m2/test_no_graph_thread_bridge.py`

**Interfaces:**
- Produces: `AgentGraphRuntime.aresume_state(request, *, resume, run_id, ...) -> AgentState` 与 `astream_state(...)`；同步 `run_state()` 仅保留兼容，不作为 service 主路。
- `aresume_state` 仅供内部 graph/runtime tests 与未来 M3 parent graph 使用。Service/Gateway composition roots 必须以 `allow_interrupt=False` 构造 Runtime，不能接收/返回 waiting 或 resume。

- [ ] **Step 1: 写 RED**

证明内部 Runtime interrupt 后 state 为 `waiting_user` 且不发 terminal，重建后 resume 恰好一个 terminal。另证明 service/Gateway composition roots 拒绝 trusted interrupt request、从不返回 waiting/resume frame，现有 Agent-Service/media frames 完全不变；其主路仍不以 thread bridge 执行 graph。M3 才把 Workflow waiting input 投影为产品事件。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_runtime_resume.py tests/tdd/native-langgraph-m2/test_gateway_compatibility.py
```

- [ ] **Step 3: 收缩 facade**

prepare/context/finalize 继续共享，但内部执行输入为 `AssistantTurnState | Command`；waiting run 不执行 terminal commit，resume terminal 才 commit。Service path 固定禁用 interrupt，若意外得到 waiting 则 fail closed 为 infrastructure error 而不产生新 wire。删除 service/runtime 内模拟 graph lifecycle 和 graph execution thread bridge；Gateway pool 自身资源 checkout bridge不机械删除。

- [ ] **Step 4: 运行 GREEN 与受保护协议测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2 tests/tdd/native-langgraph-runtime tests/core/contract/test_gateway_contract.py tests/core/integration/test_runtime_lifecycle.py
```

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/runtime/runtime.py src/assistant_agent/runtime/assistant_run_service.py src/assistant_agent/runtime/event_stream.py tests/tdd/native-langgraph-m2/test_runtime_resume.py tests/tdd/native-langgraph-m2/test_gateway_compatibility.py tests/tdd/native-langgraph-m2/test_no_graph_thread_bridge.py
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

**Core invariant:** `RUN-001` 增加 `waiting_user` 可恢复非终态及 resume 后唯一终态（不得把 interrupted 称为 terminal）；`LOOP-001` 增加 versioned persistent state/profile 与 scripted trajectory 等价；`IDENT-001` 删除 M1 root saver 临时说明并增加 stable thread/new run resume；`TOOL-001` 增加 resumable write operation barrier；`GATE-001` 外部协议保持不变。`DUR-001` 的 Workflow scheduler 迁移不属于 M2。

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
! rg -n "_emit_graph_execution_event" src/assistant_agent/runtime src/assistant_agent/api src/assistant_agent/gateway
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m2/test_no_graph_thread_bridge.py
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
