# Supervisor Todo Planning Graph A-lite 实验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改动生产 Planning Graph 的隔离实验中，验证 A-lite 所依赖的 Supervisor 协议、`Send` 并行 fan-out/fan-in、Worker 私有 `create_agent` 上下文、Todo/replan 保留规则、pending writes 恢复与原生可观察性，并据此给出继续、修订或终止生产迁移的结论。

**Architecture:** 所有 spike 代码和 RED/GREEN 验证都放在可手动整目录删除的 `tests/tdd/supervisor-todo-planning-alite-experiment/`。实验使用普通 LLM node 充当 Supervisor、标准 `ToolNode` 执行 control Tool、条件边把 `task(todo_id)` 转为多个 `Send("worker", ...)`，Worker wrapper 只向唯一的 `create_agent` 子图投影当前 Todo 和可信上下文；父图使用 `InMemorySaver` 验证失败 super-step 的 pending writes 与恢复行为。

**Tech Stack:** Python 3.12、LangChain `>=1.3.15,<2`、本机 `langchain==1.3.15`、LangGraph `>=1.2.4,<2`、本机 `langgraph==1.2.11`、`langgraph-checkpoint>=4.1.1,<5`、Pydantic v2、pytest、项目 `hello_agent` conda 环境。

**Spec:** `docs/superpowers/specs/2026-08-24-supervisor-todo-planning-graph-design-alite.md`

## Global Constraints

- 执行本计划前使用 `superpowers:using-git-worktrees` 创建隔离 worktree；实验提交不得直接混入当前工作分支。
- 本计划只验证架构假设，不修改 `src/assistant_agent/native_agent/planning_graph.py`、`state.py`、`models.py`、`planning_phase.py`、`planning_recovery.py` 或 `planning_budget.py`。
- 不修改 `langgraph.json`，不启动第二套 `langgraph dev`，不占用或重启现有 `8089` 服务。
- 全部自动化实验固定使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得读取真实 `.env`，不得访问网络、真实 Provider、MCP 或付费 API。
- Supervisor 必须是普通模型 node；实验 Graph 中只有 Worker 通过 `langchain.agents.create_agent` 构建。
- `task` 只作为模型可见 ToolCall schema 和 Graph 路由协议存在，不得进入普通业务 `ToolNode`。
- `write_todos`、`load_skill`、`load_skill_reference` 属 control Tool；本实验用最小 `write_todos` probe 验证标准 `ToolNode` 路径，不复制生产 Skill loader。
- 同一 Supervisor `AIMessage` 只允许一种动作类别：control ToolCall、一个或多个 `task` ToolCall、或无 ToolCall 的最终回答；混合调用必须 fail closed。
- Worker 输入不得包含父级完整 `messages`；只允许当前 Todo、必要的 Skill/reference 投影、可信上下文和新建的私有 `messages`。
- Worker 业务受阻返回 `WorkerResult(status="blocked")`；Provider/连接/进程等 operational exception 必须继续抛出，禁止转成 `blocked`。
- 本实验不验证外部副作用 replay；所有 probe Tool 都是 deterministic read-only，副作用幂等与 planning HITL 留给后续生产实施计划。
- 不修改 `tests/core` 或 `tests/core/INVARIANTS.md`。实验通过后，生产实施必须单独评审并同步重写 `LOOP-001`、`CTX-001` 及其现有负责测试。
- 所有临时 pytest 都放入 `tests/tdd/supervisor-todo-planning-alite-experiment/`，只显式运行，不进入默认收集，不自动晋升 core；实验结束后用户可手动删除整个目录。
- 不创建 `evals/system/incubating`：本轮验证的是离线 Graph/节点契约，没有真实 Provider 专项或需长期观察的能力风险；若后续出现真实 trace 风险证据，再单独立项。
- 实验事实只来自结构化 state、标准 message、checkpoint snapshot、调用计数和原生 stream namespace；不以完整自然语言文案或私有框架实现细节作为主要断言。

---

## 实验问题与决策门

| Gate | 问题 | 通过条件 | 失败动作 |
| --- | --- | --- | --- |
| G1 | Supervisor ToolCall 协议能否由一个薄路由函数完整判别？ | control、单 task、多 task、final 四条路径可达；混合/未知/重复 task fail closed | 修订规格，先收窄 Supervisor 输出 schema；不得靠宽松兜底路由上线 |
| G2 | `write_todos` 能否经标准 `ToolNode` 维护 Todo working memory？ | pending 可增删改；completed 及其成功结果不能被静默删除或降级；ToolMessage 正确配对 | 修订 Todo/control 契约；不得在入口或 prompt 外隐式维护计划 |
| G3 | 多个 `task` 能否通过 `Send` 形成同一 super-step 的并行 Worker 并只 join 一次？ | A/B/C 同时进入 barrier；三条结果齐全；join 只执行一次；ToolMessage 与原 call ID 一一对应 | 停止生产迁移；不得用手写 `asyncio.gather` 或自建 scheduler 替代 |
| G4 | 唯一 Worker `create_agent` 能否在多次 invocation 中保持上下文隔离？ | Graph 构建只调用一次 `create_agent`；每个 Worker 只看到自己的 Todo/可信投影；无父消息或兄弟结果泄漏 | 修订 Worker 装配/投影边界；不得用全局变量或 thread-scoped 私有 memory 补洞 |
| G5 | blocked、retry、replan、finish 能否只由 Supervisor + Todo/result state 表达？ | blocked 正常 join；A/B completed 保留；重试 C 不重跑 A/B；C→D 后 A/B 结果仍在；最终无 ToolCall 直接 END | 修订最小 state/reducer；若必须恢复 generation/ledger，A-lite 不成立 |
| G6 | operational exception 能否原样交给 LangGraph pending writes 恢复？ | A/B 成功、C 首次抛错时不运行 join；`ainvoke(None)` 后仅 C 重跑；A/B 写入复用；随后完整 join | 硬阻断 A-lite 生产迁移；先确认当前 LangGraph/checkpointer 契约或修改异常模型 |
| G7 | 原生 stream 能否观察父 Graph 与嵌套 Worker agent？ | graph 可见 supervisor/controls/worker/join；`subgraphs=True` 流中可区分 Worker namespace 及其 model/tools 子节点；无自建事件协议 | 修订 Worker subgraph 装配方式，优先静态可发现边界；不得新增 shadow trace tree |

只有 G1–G7 全部通过，结论才允许为“进入生产实施计划”。G6 是一票否决项；G7 失败时可以保留其他实验事实，但不能批准当前 wrapper 形态。

## 文件结构

实验 worktree 中只创建以下目录：

```text
tests/tdd/supervisor-todo-planning-alite-experiment/
├── README.md                         # 范围、命令、Gate 结果和删除条件
├── experiment_graph.py              # 最小 A-lite StateGraph、schema、reducer 与路由
├── probes.py                        # scripted Supervisor/Worker model、barrier 与 read Tool
├── test_supervisor_protocol.py      # G1、G2
├── test_parallel_join.py            # G3、G4
├── test_replan.py                   # G5
├── test_pending_writes.py           # G6
└── test_observability.py            # G7
```

`experiment_graph.py` 是刻意隔离的 spike，不得被生产代码导入。实验通过后不得直接把该文件移动或重命名成生产实现；后续必须基于实验结论另写生产实施计划，逐步迁移当前 Graph 并同步 authority/core invariant。

---

### Task 1: 建立最小 state、reducer 与 Todo 不变量（G2 基础）

**Files:**
- Create: `tests/tdd/supervisor-todo-planning-alite-experiment/experiment_graph.py`
- Create: `tests/tdd/supervisor-todo-planning-alite-experiment/test_supervisor_protocol.py`

**Interfaces:**
- Consumes: `MessagesState`、官方 message reducer、`ToolRuntime`、`Command`、`Overwrite`。
- Produces: `PlanningTodo`、`WorkerResult`、`WorkerWrite`、`ExperimentPlanningState`、`merge_worker_results()`、`replace_todos()`、`create_write_todos_tool()`。

- [ ] **Step 1: 写 Todo 与 result reducer 的 RED 测试**

```python
from experiment_graph import (
    WorkerResult,
    merge_worker_results,
    replace_todos,
)


def test_completed_todo_and_result_are_monotonic() -> None:
    current = [
        {"todo_id": "A", "content": "alpha", "status": "completed"},
        {"todo_id": "B", "content": "beta", "status": "pending"},
    ]
    results = {
        "A": WorkerResult(todo_id="A", status="succeeded", summary="a-result")
    }

    updated = replace_todos(
        current,
        [
            {"todo_id": "A", "content": "alpha", "status": "completed"},
            {"todo_id": "C", "content": "gamma", "status": "pending"},
        ],
        worker_results=results,
    )

    assert [item["todo_id"] for item in updated] == ["A", "C"]
    assert updated[0]["status"] == "completed"


def test_completed_todo_cannot_be_removed_or_downgraded() -> None:
    current = [{"todo_id": "A", "content": "alpha", "status": "completed"}]
    results = {
        "A": WorkerResult(todo_id="A", status="succeeded", summary="a-result")
    }

    with pytest.raises(ValueError, match="completed todo A"):
        replace_todos(current, [], worker_results=results)
    with pytest.raises(ValueError, match="completed todo A"):
        replace_todos(
            current,
            [{"todo_id": "A", "content": "alpha", "status": "pending"}],
            worker_results=results,
        )


def test_worker_result_merge_rejects_conflicting_success() -> None:
    first = {"A": WorkerResult(todo_id="A", status="succeeded", summary="one")}
    same = {"A": WorkerResult(todo_id="A", status="succeeded", summary="one")}
    conflict = {"A": WorkerResult(todo_id="A", status="succeeded", summary="two")}
    blocked = {"B": WorkerResult(todo_id="B", status="blocked", summary="blocked")}
    retried = {"B": WorkerResult(todo_id="B", status="succeeded", summary="done")}

    assert merge_worker_results(first, same) == first
    assert merge_worker_results(blocked, retried) == retried
    with pytest.raises(ValueError, match="conflicting worker result A"):
        merge_worker_results(first, conflict)
```

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-experiment/test_supervisor_protocol.py
```

Expected: collection fails because `experiment_graph.py` and the contracts do not exist.

- [ ] **Step 3: 实现最小 checkpoint-safe contract**

在 `experiment_graph.py` 定义：

```python
from __future__ import annotations

import json
import operator
from typing import Annotated, Literal, NotRequired, TypedDict

from langgraph.graph import MessagesState


class PlanningTodo(TypedDict):
    todo_id: str
    content: str
    status: Literal["pending", "completed"]


class WorkerResult(TypedDict):
    todo_id: str
    status: Literal["succeeded", "blocked"]
    summary: str


class WorkerWrite(TypedDict):
    task_call_id: str
    result: WorkerResult


def merge_worker_results(
    left: dict[str, WorkerResult] | None,
    right: dict[str, WorkerResult] | None,
) -> dict[str, WorkerResult]:
    merged = dict(left or {})
    for todo_id, result in (right or {}).items():
        previous = merged.get(todo_id)
        if previous is not None and previous["status"] == "succeeded" and previous != result:
            raise ValueError(f"conflicting worker result {todo_id}")
        merged[todo_id] = result
    return merged


class ExperimentPlanningState(MessagesState):
    todos: list[PlanningTodo]
    worker_results: Annotated[dict[str, WorkerResult], merge_worker_results]
    worker_writes: Annotated[list[WorkerWrite], operator.add]
    loaded_skills: list[str]
    trusted_context: NotRequired[dict[str, str]]
    join_count: int
```

`replace_todos()` 必须按 `todo_id` 拒绝重复项；允许移除或改写 pending Todo；任何已有 `succeeded` result 对应的 completed Todo必须仍存在、仍为 completed，且 content 不变。不得引入 dependency、generation、attempt、budget 或 replacement ledger。

- [ ] **Step 4: 用标准 ToolNode 路径实现 `write_todos`**

```python
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command


def create_write_todos_tool():
    @tool("write_todos")
    def write_todos(
        todos: list[PlanningTodo],
        runtime: ToolRuntime,
    ) -> Command:
        """Replace pending todos while preserving completed work."""
        updated = replace_todos(
            list(runtime.state.get("todos", ())),
            todos,
            worker_results=dict(runtime.state.get("worker_results", {})),
        )
        return Command(
            update={
                "todos": updated,
                "messages": [
                    ToolMessage(
                        content=json.dumps(
                            {"updated_todo_ids": [item["todo_id"] for item in updated]},
                            sort_keys=True,
                        ),
                        name="write_todos",
                        tool_call_id=runtime.tool_call_id or "missing-tool-call-id",
                    )
                ],
            }
        )

    return write_todos
```

测试通过真实 `ToolNode([create_write_todos_tool()])` 调用，断言 `runtime` 不出现在模型可见 schema、ToolMessage 的 `tool_call_id` 与 AIMessage ToolCall 配对，并覆盖 completed 删除/降级的错误路径。

- [ ] **Step 5: 运行 GREEN 并提交实验 contract**

Run: 使用 Step 2 相同命令。
Expected: PASS。

```bash
git add tests/tdd/supervisor-todo-planning-alite-experiment/experiment_graph.py \
  tests/tdd/supervisor-todo-planning-alite-experiment/test_supervisor_protocol.py
git commit -m "experiment: define alite todo planning contracts"
```

---

### Task 2: 验证 Supervisor 输出分类与 Graph 控制流（G1）

**Files:**
- Modify: `tests/tdd/supervisor-todo-planning-alite-experiment/experiment_graph.py`
- Create: `tests/tdd/supervisor-todo-planning-alite-experiment/probes.py`
- Modify: `tests/tdd/supervisor-todo-planning-alite-experiment/test_supervisor_protocol.py`

**Interfaces:**
- Consumes: Task 1 contracts、`AIMessage.tool_calls`、`ToolNode`。
- Produces: `create_task_tool()`、`classify_supervisor_action()`、`build_experiment_graph()` 的 supervisor/controls/final 路径。

- [ ] **Step 1: 写四类合法输出和 fail-closed 输出的 RED 测试**

```python
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (AIMessage(content="done"), "final"),
        (_calls(("write_todos", {"todos": []}, "write-1")), "controls"),
        (_calls(("task", {"todo_id": "A"}, "task-a")), "tasks"),
        (
            _calls(
                ("task", {"todo_id": "A"}, "task-a"),
                ("task", {"todo_id": "B"}, "task-b"),
            ),
            "tasks",
        ),
    ],
)
def test_supervisor_action_classification(message: AIMessage, expected: str) -> None:
    assert classify_supervisor_action(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        _calls(
            ("write_todos", {"todos": []}, "write-1"),
            ("task", {"todo_id": "A"}, "task-a"),
        ),
        _calls(("unknown", {}, "unknown-1"),),
        _calls(
            ("task", {"todo_id": "A"}, "task-a-1"),
            ("task", {"todo_id": "A"}, "task-a-2"),
        ),
    ],
)
def test_supervisor_action_rejects_ambiguous_calls(message: AIMessage) -> None:
    with pytest.raises(ValueError, match="supervisor action"):
        classify_supervisor_action(message)
```

- [ ] **Step 2: 运行测试确认 RED**

Run: Task 1 的定向测试命令。
Expected: FAIL，因为 Supervisor action classifier 和 Graph builder 尚不存在。

- [ ] **Step 3: 实现普通 Supervisor node 和严格 action classifier**

```python
def create_task_tool():
    @tool("task")
    def task(todo_id: str) -> str:
        """Delegate exactly one existing pending Todo to a Worker."""
        raise AssertionError("task is a graph routing protocol, not an executable tool")

    return task


def classify_supervisor_action(message: AIMessage) -> Literal[
    "controls", "tasks", "final"
]:
    calls = list(message.tool_calls)
    if not calls:
        return "final"
    names = [call["name"] for call in calls]
    if len(calls) == 1 and names == ["write_todos"]:
        return "controls"
    if all(name == "task" for name in names):
        todo_ids = [str(call["args"].get("todo_id", "")) for call in calls]
        if "" in todo_ids or len(todo_ids) != len(set(todo_ids)):
            raise ValueError("invalid supervisor action: duplicate or empty task todo_id")
        return "tasks"
    raise ValueError("invalid supervisor action: mixed or unknown tool calls")
```

实验 classifier 的 control allowlist 刻意只有 `write_todos`，与本轮实际装配的 `ToolNode` 保持一致；生产迁移时才把已经静态装配的 `load_skill`、`load_skill_reference` 加入同一 allowlist。`supervisor` node 只执行 `await supervisor_model.ainvoke(state["messages"])` 并返回一个 `AIMessage`；构建时对模型调用 `bind_tools([write_todos, task])`。不得调用 `create_agent`，不得生成 structured plan，也不得在 Python 中推断用户意图。

- [ ] **Step 4: 接线 controls 与 final 路径**

`build_experiment_graph()` 先只接线：

```text
START -> supervisor
supervisor --controls--> ToolNode([write_todos]) -> supervisor
supervisor --no tool_calls--> END
```

测试使用 scripted Supervisor 依次输出 `write_todos(A)` 和无 ToolCall 的 `AIMessage("final-sentinel")`，断言：

```python
assert [item["todo_id"] for item in result["todos"]] == ["A"]
assert isinstance(result["messages"][-1], AIMessage)
assert result["messages"][-1].tool_calls == []
assert supervisor_model.create_agent_calls == 0
```

- [ ] **Step 5: 运行 G1/G2 GREEN 并提交**

Run: Task 1 的定向测试命令。
Expected: PASS。

```bash
git add tests/tdd/supervisor-todo-planning-alite-experiment
git commit -m "experiment: validate alite supervisor protocol"
```

---

### Task 3: 验证唯一 Worker Agent、scoped input 与并行 join（G3、G4）

**Files:**
- Modify: `tests/tdd/supervisor-todo-planning-alite-experiment/experiment_graph.py`
- Modify: `tests/tdd/supervisor-todo-planning-alite-experiment/probes.py`
- Create: `tests/tdd/supervisor-todo-planning-alite-experiment/test_parallel_join.py`

**Interfaces:**
- Consumes: Task 2 Graph、`Send`、`create_agent`、`WorkerWrite` reducer。
- Produces: `WorkerInput`、`dispatch_tasks()`、`worker_wrapper()`、`join_workers()`，以及完整 `supervisor -> Send(worker)*N -> join -> supervisor` 闭环。

- [ ] **Step 1: 写三 Worker barrier 和上下文隔离的 RED 测试**

```python
def test_three_tasks_run_in_parallel_and_join_once() -> None:
    supervisor = ScriptedSupervisor.parallel_wave(("A", "B", "C"))
    worker_model = BarrierWorkerModel(expected_todos={"A", "B", "C"})
    graph = build_experiment_graph(supervisor, worker_model)

    result = asyncio.run(graph.ainvoke(_initial_input("parent-secret-sentinel")))

    assert worker_model.max_concurrency == 3
    assert worker_model.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1})
    assert result["join_count"] == 1
    assert set(result["worker_results"]) == {"A", "B", "C"}
    assert {
        message.tool_call_id
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == "task"
    } == {"task-A", "task-B", "task-C"}
    assert all("parent-secret-sentinel" not in call.text for call in worker_model.calls)
```

另加一个真实 Worker Agent loop 测试：`ToolCallingWorkerModel` 先调用 `read_probe`，看到私有 `ToolMessage` 后再提交 `WorkerResult`。断言 read probe 恰好执行一次、结果 summary 含 `read-probe-result-sentinel`，且父级 `messages` 只收到 join 生成的 `name="task"` ToolMessage，不泄漏 Worker 内部的业务 ToolMessage：

```python
def test_worker_uses_private_create_agent_tool_loop() -> None:
    recorder: list[str] = []
    graph = build_experiment_graph(
        ScriptedSupervisor.single_success("A"),
        ToolCallingWorkerModel(todo_id="A"),
        read_probe_tool=create_read_probe_tool(recorder),
    )

    result = asyncio.run(graph.ainvoke(_initial_input("parent-secret-sentinel")))

    assert recorder == ["A"]
    assert result["worker_results"]["A"]["summary"] == (
        "read-probe-result-sentinel"
    )
    assert not any(
        isinstance(message, ToolMessage) and message.name == "read_probe"
        for message in result["messages"]
    )
```

再加构建期 probe，monkeypatch `experiment_graph.create_agent` 并断言 `build_experiment_graph()` 只调用一次；Supervisor 仍为普通 node。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-experiment/test_parallel_join.py
```

Expected: FAIL，因为 task dispatch、Worker agent 和 join 尚未接线。

- [ ] **Step 3: 实现 `task` → `Send` 的薄 dispatch**

```python
class WorkerInput(TypedDict):
    todo_id: str
    content: str
    task_call_id: str
    loaded_skills: tuple[str, ...]
    trusted_context: dict[str, str]


def dispatch_tasks(state: ExperimentPlanningState) -> list[Send]:
    message = _last_ai_message(state)
    pending = {
        item["todo_id"]: item
        for item in state["todos"]
        if item["status"] == "pending"
    }
    sends: list[Send] = []
    for call in message.tool_calls:
        todo_id = str(call["args"]["todo_id"])
        if todo_id not in pending:
            raise ValueError(f"task references non-pending todo {todo_id}")
        sends.append(
            Send(
                "worker",
                WorkerInput(
                    todo_id=todo_id,
                    content=pending[todo_id]["content"],
                    task_call_id=call["id"],
                    loaded_skills=tuple(state.get("loaded_skills", ())),
                    trusted_context=dict(state.get("trusted_context", {})),
                ),
            )
        )
    return sends
```

不得添加 ready-set、depends_on、execution ID、attempt 或 budget 判断。

- [ ] **Step 4: 构建唯一 Worker `create_agent` 与 wrapper**

```python
worker_agent = create_agent(
    model=worker_model,
    tools=[read_probe_tool],
    response_format=WorkerResult,
    name="planning_worker_experiment",
)


async def worker_wrapper(worker_input: WorkerInput) -> dict[str, object]:
    private_payload = {
        "todo_id": worker_input["todo_id"],
        "content": worker_input["content"],
        "loaded_skills": list(worker_input["loaded_skills"]),
        "trusted_context": worker_input["trusted_context"],
    }
    result = await worker_agent.ainvoke(
        {"messages": [HumanMessage(content=json.dumps(private_payload, sort_keys=True))]}
    )
    worker_result = WorkerResult(**result["structured_response"])
    if worker_result["todo_id"] != worker_input["todo_id"]:
        raise ValueError("worker result todo_id mismatch")
    return {
        "worker_writes": [
            WorkerWrite(
                task_call_id=worker_input["task_call_id"],
                result=worker_result,
            )
        ]
    }
```

父级 `messages` 不出现在 `WorkerInput`，Worker 每次 invocation 从一个新 `HumanMessage` 开始。Worker agent 使用默认 per-invocation subgraph persistence，不设置 `checkpointer=True` 或独立 thread memory。

- [ ] **Step 5: 实现确定性 join**

```python
from langgraph.types import Overwrite


def join_workers(state: ExperimentPlanningState) -> dict[str, object]:
    writes = sorted(state["worker_writes"], key=lambda item: item["result"]["todo_id"])
    results = {item["result"]["todo_id"]: item["result"] for item in writes}
    completed = {
        todo_id
        for todo_id, result in results.items()
        if result["status"] == "succeeded"
    }
    todos = [
        {**item, "status": "completed" if item["todo_id"] in completed else item["status"]}
        for item in state["todos"]
    ]
    messages = [
        ToolMessage(
            content=json.dumps(item["result"], sort_keys=True),
            name="task",
            tool_call_id=item["task_call_id"],
        )
        for item in writes
    ]
    return {
        "todos": todos,
        "worker_results": results,
        "worker_writes": Overwrite([]),
        "messages": messages,
        "join_count": state.get("join_count", 0) + 1,
    }
```

排序只用于稳定生成 ToolMessage，不将完成顺序解释为业务依赖。Graph 添加 `worker -> join -> supervisor`；LangGraph 负责等待同一 wave 的全部 worker 后再执行 join。

- [ ] **Step 6: 运行 G3/G4 GREEN 并提交**

Run: 使用 Step 2 相同命令。
Expected: PASS，且 barrier 证明真实重叠而非仅有三次调用。

```bash
git add tests/tdd/supervisor-todo-planning-alite-experiment
git commit -m "experiment: validate alite send fanout and worker isolation"
```

---

### Task 4: 验证 blocked、retry、replan 与直接 finish（G5）

**Files:**
- Modify: `tests/tdd/supervisor-todo-planning-alite-experiment/probes.py`
- Create: `tests/tdd/supervisor-todo-planning-alite-experiment/test_replan.py`

**Interfaces:**
- Consumes: Task 3 完整 Graph、标准 parent `ToolMessage`、Todo/result state。
- Produces: blocked→retry、blocked→replace、blocked→finish 三条 scripted Supervisor trajectory 的实验证据。

- [ ] **Step 1: 写业务 blocked 后只重试 C 的测试**

```python
def test_blocked_c_can_retry_without_replaying_a_or_b() -> None:
    supervisor = ScriptedSupervisor.blocked_then_retry("C")
    worker = ScenarioWorkerModel(
        outcomes={"A": ["succeeded"], "B": ["succeeded"], "C": ["blocked", "succeeded"]}
    )
    graph = build_experiment_graph(supervisor, worker)

    result = asyncio.run(graph.ainvoke(_initial_input()))

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 2})
    assert {item["todo_id"] for item in result["todos"] if item["status"] == "completed"} == {"A", "B", "C"}
    assert result["worker_results"]["A"]["summary"] == "A-success-sentinel"
    assert result["worker_results"]["B"]["summary"] == "B-success-sentinel"
```

- [ ] **Step 2: 写 C→D replan 保留 A/B 的测试**

```python
def test_replan_replaces_pending_c_and_preserves_completed_results() -> None:
    supervisor = ScriptedSupervisor.blocked_c_then_replace_with_d()
    worker = ScenarioWorkerModel(
        outcomes={"A": ["succeeded"], "B": ["succeeded"], "C": ["blocked"], "D": ["succeeded"]}
    )

    result = asyncio.run(build_experiment_graph(supervisor, worker).ainvoke(_initial_input()))

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1, "D": 1})
    assert [item["todo_id"] for item in result["todos"]] == ["A", "B", "D"]
    assert set(result["worker_results"]) == {"A", "B", "C", "D"}
    assert result["worker_results"]["A"]["status"] == "succeeded"
    assert result["worker_results"]["B"]["status"] == "succeeded"
```

`worker_results["C"]` 作为历史消息/结果证据可保留，但 C 已不是当前 Todo；不得为此增加 generation 或 replacement ledger。

- [ ] **Step 3: 写基于 A/B 直接 finish 的测试**

```python
def test_supervisor_can_finish_after_blocked_c() -> None:
    supervisor = ScriptedSupervisor.blocked_c_then_finish()
    worker = ScenarioWorkerModel(
        outcomes={"A": ["succeeded"], "B": ["succeeded"], "C": ["blocked"]}
    )

    result = asyncio.run(build_experiment_graph(supervisor, worker).ainvoke(_initial_input()))

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1})
    assert result["todos"][-1] == {
        "todo_id": "C", "content": "todo-C", "status": "pending"
    }
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].tool_calls == []
```

- [ ] **Step 4: 运行 G5 测试并提交**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-experiment/test_replan.py
```

Expected: PASS。

```bash
git add tests/tdd/supervisor-todo-planning-alite-experiment
git commit -m "experiment: validate alite retry replan and finish"
```

---

### Task 5: 验证 operational exception 的 pending writes 恢复（G6）

**Files:**
- Modify: `tests/tdd/supervisor-todo-planning-alite-experiment/probes.py`
- Create: `tests/tdd/supervisor-todo-planning-alite-experiment/test_pending_writes.py`

**Interfaces:**
- Consumes: Task 3 Graph、`InMemorySaver`、相同 `thread_id` 下的 `ainvoke(None)` 恢复语义。
- Produces: A/B 成功写入不重跑、C 单独重跑、join 在完整 super-step 后执行的硬 Gate 证据。

- [ ] **Step 1: 写首次 C 抛错、恢复后成功的 RED 测试**

```python
def test_pending_writes_resume_only_failed_worker() -> None:
    saver = InMemorySaver()
    supervisor = ScriptedSupervisor.parallel_wave(("A", "B", "C"))
    worker = OperationalFailureWorkerModel(fail_once_for="C")
    graph = build_experiment_graph(supervisor, worker, checkpointer=saver)
    config = {"configurable": {"thread_id": "alite-pending-writes"}}

    with pytest.raises(TimeoutError, match="C-operational-sentinel"):
        asyncio.run(graph.ainvoke(_initial_input(), config=config))

    failed_snapshot = graph.get_state(config)
    assert failed_snapshot.next == ("worker",)
    assert failed_snapshot.values.get("join_count", 0) == 0
    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1})

    result = asyncio.run(graph.ainvoke(None, config=config))

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 2})
    assert result["join_count"] == 1
    assert set(result["worker_results"]) == {"A", "B", "C"}
    assert all(item["status"] == "completed" for item in result["todos"])
```

如果当前版本的 `StateSnapshot.next` 对 push task 暴露多个 task 标识而不是单个 `"worker"`，只把该断言改为检查 `failed_snapshot.tasks` 中恰有 C 的 error、A/B task 已有 result；不得删除调用计数和 resume 后 A/B 不重跑的核心断言。

- [ ] **Step 2: 确保 Worker wrapper 不吞 operational exception**

检查 `worker_wrapper()` 只校验正常 `structured_response`，不得包含以下模式：

```python
try:
    ...
except Exception:
    return WorkerResult(todo_id=todo_id, status="blocked", summary="...")
```

`OperationalFailureWorkerModel` 在 C 第一次模型调用抛 `TimeoutError("C-operational-sentinel")`，第二次返回 succeeded；A/B 使用与 Task 3 相同 barrier，确保它们确实与失败 C 位于同一并行 super-step。

- [ ] **Step 3: 运行 G6 硬 Gate**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-experiment/test_pending_writes.py -vv
```

Expected: PASS；首次 run 抛出 operational exception，resume 后只有 C 的调用次数增加。

若 FAIL：保存 pytest failure summary 和结构化 snapshot/task 字段到实验 README，结论标记为“停止 A-lite 生产迁移”；不得在本实验中加入自定义 recovery router、attempt ledger 或 operational outcome 来让测试转绿。

- [ ] **Step 4: 提交 pending writes 证据**

```bash
git add tests/tdd/supervisor-todo-planning-alite-experiment
git commit -m "experiment: verify alite pending writes recovery"
```

---

### Task 6: 验证原生 Graph/subgraph 可观察性（G7）

**Files:**
- Create: `tests/tdd/supervisor-todo-planning-alite-experiment/test_observability.py`
- Create: `tests/tdd/supervisor-todo-planning-alite-experiment/README.md`

**Interfaces:**
- Consumes: Task 3 Graph、LangGraph `get_graph()`、`astream(..., subgraphs=True, version="v2")`。
- Produces: 父节点拓扑、动态 Worker namespace、Worker 内部 model/tools 事件的结构化证据和实验运行说明。

- [ ] **Step 1: 写父 Graph 拓扑断言**

```python
def test_parent_graph_contains_only_alite_runtime_roles() -> None:
    graph = build_experiment_graph(
        ScriptedSupervisor.single_success("A"),
        ScenarioWorkerModel(outcomes={"A": ["succeeded"]}),
    )
    nodes = set(graph.get_graph().nodes)

    assert {"supervisor", "controls", "worker", "join"} <= nodes
    assert nodes.isdisjoint(
        {"planner", "scheduler", "finalizer", "recovery", "reserve_wave_budget"}
    )
```

- [ ] **Step 2: 写 v2 subgraph stream namespace 断言**

```python
def test_worker_create_agent_is_visible_in_native_subgraph_stream() -> None:
    async def collect():
        return [
            part
            async for part in graph.astream(
                _initial_input(),
                stream_mode=["updates", "messages"],
                subgraphs=True,
                version="v2",
            )
        ]

    parts = asyncio.run(collect())
    worker_parts = [
        part for part in parts
        if part["ns"] and part["ns"][0].startswith("worker:")
    ]

    assert worker_parts
    assert any(
        part["type"] == "updates"
        and set(part["data"]).intersection({"model", "tools"})
        for part in worker_parts
    )
```

若 wrapper 形态只暴露 `worker` 而看不到内部 `model/tools`，记录 G7 失败并尝试一次公开 API 变体：把 compiled Worker agent 作为显式可发现 subgraph node，通过相同 `Send` 输入适配 state。该变体仍不得把 Worker 变成 Tool，也不得访问私有 trace API。若变体通过，后续生产计划采用显式 subgraph adapter；若仍失败，停止批准当前 observability 设计。

- [ ] **Step 3: 运行 G7 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-experiment/test_observability.py -vv
```

Expected: PASS，且不需要 custom stream、shadow span 或自建事件树。

- [ ] **Step 4: 写实验 README 的固定协议**

README 必须包含：

```markdown
# Supervisor Todo Planning A-lite Experiment

- Scope: offline Graph semantics only
- Provider mode: mock
- LangGraph tested version: 1.2.11
- Production wiring: none
- Temporary tests: user may delete this whole directory manually

## Commands

列出 Task 7 的三个实际命令。

## Gate results

逐项记录 G1-G7 的 PASS/FAIL、对应 pytest item 和一句结构化证据。

## Decision

只能写以下三种结论之一：
- proceed_to_production_plan：G1-G7 全部通过；
- revise_spec：G1-G5 中存在可通过收窄契约修正的问题，且 G6 已通过；
- stop_migration：G6 失败，或 G7 的 wrapper/显式 subgraph 两种公开 API 形态均失败。
```

`LangGraph tested version` 必须由运行时 `importlib.metadata.version("langgraph")` 生成后写入，不得只复制计划中的版本。

- [ ] **Step 5: 提交 observability 与运行协议**

```bash
git add tests/tdd/supervisor-todo-planning-alite-experiment
git commit -m "experiment: validate alite native observability"
```

---

### Task 7: 执行完整实验、记录结论并决定是否生成生产实施计划

**Files:**
- Modify: `tests/tdd/supervisor-todo-planning-alite-experiment/README.md`
- No production files.

**Interfaces:**
- Consumes: G1-G7 的全部 pytest 与当前依赖版本。
- Produces: 可复现的实验结果、单一决策和后续迁移边界。

- [ ] **Step 1: 运行静态与版本预检**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  tests/tdd/supervisor-todo-planning-alite-experiment
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -c \
  "import importlib.metadata as m; print(m.version('langchain')); print(m.version('langgraph')); print(m.version('langgraph-checkpoint'))"
```

Expected: compileall exit 0；版本仍满足 `pyproject.toml` 的范围。把实际版本写入 README。

- [ ] **Step 2: 运行全部临时实验测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-experiment
```

Expected: PASS；这是批准 `proceed_to_production_plan` 的必要条件，但不是生产迁移已经完成的证据。

- [ ] **Step 3: 确认默认 core 收集不包含实验目录**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest --collect-only -q
```

Expected: 输出只包含 `tests/core/**`，不包含 `tests/tdd/supervisor-todo-planning-alite-experiment/**`。本步骤只验证收集边界，不运行裸 pytest。

- [ ] **Step 4: 逐项完成 Gate 决策记录**

在 README 中为 G1-G7 各记录：pytest item、PASS/FAIL、关键结构化证据。最终 decision 严格按 Task 6 的三值规则生成，不得用“部分通过”替代明确结论。

若 decision 为 `proceed_to_production_plan`，后续生产计划至少必须覆盖：

1. 重写 `src/assistant_agent/native_agent/planning_graph.py` 为 supervisor/controls/worker/join 四类动作；
2. 收敛 `state.py` 与 `models.py`，删除 A-lite 明确废弃的 plan/admission/scheduler/budget/recovery ledger；
3. 删除不再使用的 `planning_phase.py`、`planning_recovery.py`、`planning_budget.py`，但先用 `rg` 核清全部生产引用；
4. 保留现有通用 Tool middleware、read retry、planning 非 read HITL、Skill loader 与静态 Tool inventory；
5. 更新 `docs/runtime-event-stream-architecture.md` 与必要的 `docs/tool-calling-architecture.md`；
6. 重写 `LOOP-001`、`CTX-001` 的结构化契约并更新现有负责 core 测试，不创建平行 invariant；
7. 运行 authority validator，并连接唯一 `8089` 服务等待 hot reload 后做 mock/Studio 原生 trace 验证；
8. 单独评审旧 checkpoint 的兼容/拒绝策略，不能默认让旧 planning checkpoint 进入新 state schema。

若 decision 为 `revise_spec` 或 `stop_migration`，只修改设计规格和实验 README；不触碰生产 planning 实现。

- [ ] **Step 5: 提交实验结论**

```bash
git add tests/tdd/supervisor-todo-planning-alite-experiment/README.md
git commit -m "experiment: record alite planning decision"
```

---

## 实验结束后的汇报格式

执行者最终必须同时报告：

```text
Decision: <proceed_to_production_plan|revise_spec|stop_migration>
Gates: G1=<PASS|FAIL>, G2=<PASS|FAIL>, G3=<PASS|FAIL>, G4=<PASS|FAIL>, G5=<PASS|FAIL>, G6=<PASS|FAIL>, G7=<PASS|FAIL>.
Core invariant: unchanged; this isolated experiment does not modify production contracts.
Tests: added tests/tdd/supervisor-todo-planning-alite-experiment for temporary RED/GREEN; user may delete the directory manually.
Provider: mock/offline only; no real Provider call was made.
Commands: <列出实际执行的三个 Task 7 命令及 exit status>.
Limitations: no production wiring, no external side-effect replay validation, no old-checkpoint migration validation.
```

实验通过只表示 A-lite 的核心框架假设在记录的 LangGraph 版本上成立，不表示设计已完成生产迁移、真实 Provider 质量已验证或现有 planning checkpoint 可直接兼容。

## 参考资料

- LangGraph Graph API / `Send` 与并行 super-step：`https://docs.langchain.com/oss/python/langgraph/use-graph-api`
- LangGraph persistence / pending writes：`https://docs.langchain.com/oss/python/langgraph/persistence`
- LangGraph subgraph persistence：`https://docs.langchain.com/oss/python/langgraph/use-subgraphs`
- LangGraph streaming / subgraph namespace：`https://docs.langchain.com/oss/python/langgraph/streaming`
- LangGraph `Send` reference：`https://reference.langchain.com/python/langgraph/types/Send`
