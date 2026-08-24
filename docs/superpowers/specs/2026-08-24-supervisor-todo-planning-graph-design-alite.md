# Supervisor 驱动的 Todo Planning Graph 设计（A-lite）

状态：待用户复核

日期：2026-08-24

项目：`https://github.com/max-star-rgb/assistant_agent`

## 1. 文档定位

本文定义 `assistant_agent` planning 模式的 A-lite 重构方案。

本文基于 `2026-08-24-supervisor-todo-planning-graph-design.md` 继续收敛，核心目标不是增加新的 Planning Runtime，而是尽可能直接使用 LangGraph 的原生 Graph、`Send`、checkpoint 和 subgraph 能力。

本版最重要的架构决策是：

> **Planning Graph 本身就是 Agent；只有 Worker 使用 `create_agent`。**

因此：

- Supervisor 是普通 LLM Node，不是 `create_agent`；
- Planner 不再作为独立 Agent、节点或运行时角色存在；
- Finalizer 不再作为独立 Agent 或节点存在；
- Planning Scheduler 不再作为独立组件存在；
- Worker 是唯一使用 `create_agent` 的 Agent subgraph；
- Todo 只是 Supervisor 的显式 planning working memory，不再建模为严格依赖 DAG；
- 并行执行由 Supervisor 一次发出多个 `task` ToolCall，再由 LangGraph `Send` 动态 fan-out；
- Worker 业务失败正常返回结果，运行时异常直接交给 LangGraph checkpoint / pending writes 恢复。

本文在用户批准并实施前，不表示生产实现已经迁移。

---

## 2. 核心设计原则

### 2.1 Graph-native orchestration

Planning 不再在 LangGraph 上额外实现一套 Planner / Scheduler / Recovery Runtime。

LangGraph 负责：

- Node / Edge 控制流；
- 动态 fan-out；
- 并行 super-step；
- state reducer；
- checkpoint；
- pending writes；
- resume；
- subgraph execution 与 observability。

项目只实现必要的业务语义：

- Supervisor 如何维护 Todo；
- `task(todo_id)` 如何映射到 Worker；
- Worker 如何返回结构化结果；
- join 如何把 Worker 结果重新交给 Supervisor。

### 2.2 Supervisor thinks, Worker acts

Supervisor 负责：

- 理解用户目标；
- 加载必要 Skill / reference；
- 创建或修改 Todo；
- 决定哪些 Todo 当前值得执行；
- 决定一个还是多个 Todo 同时执行；
- 读取 Worker 结果；
- 决定 retry / replan / finish；
- 直接生成最终用户回答。

Worker 负责：

- 执行一个明确 Todo；
- 在自己的私有消息上下文中进行标准 Agent loop；
- 调用业务 Tool；
- 返回受控的 `WorkerResult`。

### 2.3 不把治理能力预先设计进 MVP

A-lite 不包含 planning 专用的：

- dependency admission；
- ready-set scheduler；
- authorization envelope；
- capability → Tool 投影；
- global budget reservation；
- execution ID / attempt ledger；
- generation / replacement ledger；
- recovery planner；
- proposal admission correction loop。

如果未来真实问题证明需要，再按独立能力增量加入。

---

## 3. 术语

| 名词 | 定义 |
| --- | --- |
| **Supervisor** | Planning Graph 中唯一负责全局推理的 LLM Node。不是 `create_agent`。 |
| **Todo** | Supervisor 的显式 planning working memory，用于记录当前还要做什么。 |
| **Task call** | Supervisor 发出的标准 `task(todo_id=...)` ToolCall，表示“执行这个 Todo”。 |
| **Worker** | 执行单个 Todo 的 Agent，由 `create_agent` 构建，是 planning 模式唯一的 `create_agent`。 |
| **Controls** | `load_skill`、`load_skill_reference`、`write_todos` 等真正的控制 Tool。 |
| **Fan-out** | 一次 Supervisor 决策产生多个 Worker 分支，并行执行。 |
| **Fan-in** | 同一 wave 的多个 Worker 分支完成后，结果汇合进入 join。 |
| **WorkerResult** | Worker 正常结束时返回的结构化业务结果。 |
| **Operational exception** | provider timeout、connection error、进程异常等不应被解释为业务结论的异常。 |

本设计中不再使用 Planner、Planning Scheduler、Finalizer 作为生产运行时角色。

---

## 4. 目标与非目标

### 4.1 目标

1. 形成最小、清晰的 LangGraph 原生 planning loop。
2. 只有 Worker 使用 `create_agent`。
3. Supervisor 使用 Todo 显式维护计划，而不是一次性生成严格完整 DAG。
4. 支持一个 Todo 串行执行，也支持多个 Todo 动态并行执行。
5. Worker 结果重新进入 Supervisor 上下文，形成 `Plan → Act → Observe → Replan` 循环。
6. A/B 成功、C 业务失败时，保留 A/B 结果并由 Supervisor 决定下一步。
7. A/B 成功、C 发生运行时异常时，使用 LangGraph 原生 pending writes / resume，避免恢复时重跑 A/B。
8. Worker 使用独立 Agent 上下文，不把完整父 messages 无界传入 Worker。
9. Studio / trace 能看到 Planning Graph 与 Worker subgraph。

### 4.2 非目标

- 不实现严格 upfront DAG planner。
- 不要求 Todo 显式声明 `depends_on`。
- 不实现 deterministic ready-task scheduler。
- 不做 planning 专用预算系统。
- 不做 planning 专用授权 envelope。
- 不做 execution attempt ledger。
- 不实现第二套 checkpoint / recovery manager。
- 不因为 planning 模式重新实现 Tool runtime。
- 不保证 replay 能撤销外部副作用。

---

## 5. 总体 Graph

```text
                         ┌────────────────────────────┐
                         │                            │
                         v                            │
START ─────────────> supervisor                       │
                      │                               │
              ┌───────┼──────────────┐                │
              │       │              │                │
              v       v              v                │
          controls   task(s)      no tool_calls       │
              │       │              │                │
              │       │              └──────────> END │
              │       │                               │
              │       v                               │
              │   dynamic Send                        │
              │    /   |   \                          │
              │   v    v    v                         │
              │ worker worker worker                  │
              │    \   |   /                          │
              │       join                            │
              │        │                              │
              └────────┴──────────────────────────────┘
```

逻辑上只有四类动作：

1. `supervisor`：思考与决策；
2. `controls`：执行 planning 控制 Tool；
3. `worker`：执行 Todo；
4. `join`：聚合结果并返回 Supervisor。

`dispatch` 可以是一个薄路由函数，而不必成为独立运行时组件。它的职责只是把一个或多个 `task` ToolCall 转换为一个或多个 `Send("worker", ...)`。

---

## 6. Supervisor 契约

### 6.1 Supervisor 是普通 LLM Node

Supervisor 不使用 `create_agent`。

概念上：

```python
supervisor_model = model.bind_tools(
    [
        load_skill,
        load_skill_reference,
        write_todos,
        task,
    ]
)

async def supervisor(state, runtime):
    response = await supervisor_model.ainvoke(...)
    return {"messages": [response]}
```

Supervisor 的 Agent loop 由外层 StateGraph 表达，而不是由 `create_agent` 封装。

### 6.2 Supervisor 的四类输出

单次 Supervisor `AIMessage` 应属于以下一种行为：

1. 加载 Skill / reference；
2. `write_todos`；
3. 一个或多个 `task` ToolCall；
4. 无 ToolCall 的最终自然语言回答。

为了保持控制流简单，A-lite 不允许同一 `AIMessage` 同时混合 Todo 修改和 Task 派发。

例如下面是合法的一轮：

```text
AIMessage:
  task(todo_id="weather")
  task(todo_id="hotel")
  task(todo_id="poi")
```

而下面不推荐：

```text
AIMessage:
  write_todos(...)
  task(todo_id="weather")
```

Supervisor 应先更新计划，下一轮再派发。

### 6.3 最终回答

不再设置独立 Finalizer。

当 Supervisor 返回：

```text
AIMessage(text=..., tool_calls=[])
```

Graph 直接进入 `END`。

因此 finalization 只是 Supervisor 的一种自然行为，而不是一个额外 Agent / phase。

---

## 7. Todo 模型

### 7.1 Todo 是 working memory，不是严格 DAG

A-lite 中 Todo 的主要作用是让 Supervisor 显式记录：

> 当前还需要完成哪些工作。

Todo 不负责表达 Runtime 调度协议。

推荐最小模型：

```python
class PlanningTodo(TypedDict):
    todo_id: str
    content: str
    status: Literal["pending", "completed"]
```

第一版不包含：

```text
depends_on
required_capabilities
execution_id
attempt
budget
priority
replacement ids
```

### 7.2 Todo 与执行顺序

执行顺序由 Supervisor 决定，而不是代码根据依赖字段计算。

如果 B 逻辑上依赖 A，Supervisor 应先：

```text
task(A)
```

收到 A 的结果后，再：

```text
task(B)
```

如果 A/B/C 可以独立执行，Supervisor 可以同一轮输出：

```text
task(A)
task(B)
task(C)
```

因此：

- Todo list 本身不是一张经过验证的 dependency DAG；
- 具体一次执行仍然会由 LangGraph 动态展开成 execution DAG。

### 7.3 Replan

Worker 返回后，Supervisor 可以：

- 保留已经完成的 Todo；
- 修改尚未完成的 Todo；
- 新增 Todo；
- 放弃不再需要的 Todo；
- 重试原 Todo；
- 直接结束。

最小一致性规则：已 completed 的 Todo 和对应成功结果不应被 `write_todos` 静默覆盖。

---

## 8. `task` 协议

### 8.1 Task ToolCall

Supervisor 通过标准 ToolCall 表达委派：

```json
{
  "name": "task",
  "args": {
    "todo_id": "weather"
  },
  "id": "call_123"
}
```

`task` 不需要携带：

```text
execution_id
budget
capability
Tool allowlist
dependency proof
arbitrary new plan
```

Worker 的目标直接来自对应 Todo。

### 8.2 为什么保留 `todo_id`

`task(todo_id)` 保持一个很轻但重要的契约：

```text
write_todos
   ↓
形成显式计划
   ↓
task(todo_id)
   ↓
执行计划项
```

这样 Todo 不会退化成纯装饰。

### 8.3 Task ToolCall 不进入普通业务 ToolNode

`task` 虽然使用 ToolCall schema 与模型交互，但它不是一个普通业务 Tool。

路由层识别 `task` 后，直接生成 Worker `Send`。

Worker 完成后，由 join 使用原 `tool_call_id` 生成对应父级 `ToolMessage`。

父级消息轨迹保持：

```text
Supervisor AIMessage
  ├─ task(A)
  └─ task(B)

        ↓

ToolMessage(A result)
ToolMessage(B result)

        ↓

Supervisor AIMessage
```

---

## 9. 并行：dynamic fan-out / fan-in

### 9.1 Fan-out

如果 Supervisor 一次输出多个 Task ToolCall：

```text
task(A)
task(B)
task(C)
```

路由函数返回多个 `Send`：

```python
[
    Send("worker", worker_input_A),
    Send("worker", worker_input_B),
    Send("worker", worker_input_C),
]
```

LangGraph 会把这些 Worker 调度到同一个后续 super-step 中并行执行。

```text
          supervisor
              │
          task A/B/C
              │
          fan-out
        /     |     \
       v      v      v
   worker A worker B worker C
```

### 9.2 Fan-in

三个 Worker 完成后，结果通过 reducer 汇总，下一步进入 join：

```text
worker A ─┐
worker B ─┼──> join ──> supervisor
worker C ─┘
```

这就是 fan-in。

### 9.3 DAG 的准确含义

A-lite 仍然满足 DAG 的执行思想，但需要区分：

- 编译后的 Planning Graph 本身允许循环，因为 `join → supervisor` 会反复发生；
- 单个 wave 的执行展开是一个有向无环结构；
- 多个 `Send(worker)` 构成动态并行分支；
- join 是 fan-in 汇合点；
- Supervisor 之后可以再展开下一轮新的 execution DAG。

因此可以理解为：

> **循环 Graph 中不断动态展开局部 execution DAG。**

---

## 10. Worker Agent

### 10.1 Worker 是唯一 `create_agent`

Worker 使用标准 LangChain Agent：

```python
planning_worker_agent = create_agent(
    model=worker_model,
    tools=business_tools,
    middleware=worker_middleware,
    response_format=WorkerResult,
    name="planning_worker",
)
```

它内部负责标准：

```text
LLM
 ↓
ToolCall
 ↓
Tool
 ↓
ToolMessage
 ↓
LLM
 ...
 ↓
WorkerResult
```

### 10.2 Worker 输入必须 scoped

Worker 不读取完整父级 conversation。

推荐 Worker 输入只包含：

- 当前 Todo 内容；
- 当前 Todo 必要的 Skill / reference 内容；
- 必要的可信上下文；
- 当前 Worker 的私有 messages。

父层与 Worker state schema 不同，因此推荐使用一个薄 wrapper node 做输入输出映射：

```text
Parent PlanningState
       ↓
worker wrapper
       ↓ transform
Worker AgentState
       ↓
create_agent
       ↓ transform
WorkerResult
       ↓
Parent PlanningState update
```

这样既隔离上下文，也保留 Worker 作为可观察的嵌套 graph execution。

### 10.3 Worker persistence

Worker 默认使用 per-invocation subgraph 语义，不要求跨多个 Todo invocation 保留自己的多轮私有 memory。

因此不为 Worker 额外创建独立 thread-scoped checkpointer。

父 Planning Graph / Agent Server 负责整体 persistence，Worker invocation 继承原生 subgraph durability。

---

## 11. WorkerResult

推荐最小结果：

```python
class WorkerResult(TypedDict):
    todo_id: str
    status: Literal["succeeded", "blocked"]
    summary: str
```

这里刻意不用 `failed` 表示所有异常，因为需要区分：

### `blocked`

Worker 正常完成 Agent loop，但业务上无法继续，例如：

- 没找到足够证据；
- 数据为空；
- 用户条件无法满足；
- Tool 正常返回“无结果”；
- 继续调用没有合理收益。

这是一个正常的业务结果。

### Operational exception

例如：

- model/provider timeout；
- connection error；
- 未被 retry middleware 恢复的基础设施错误；
- 进程异常；
- Graph control-flow exception。

这类异常不转换成 `WorkerResult(blocked)`，而是让异常继续传播给 LangGraph。

---

## 12. Join 契约

Join 不做 Scheduler 决策。

只负责：

1. 汇总本轮正常完成的 WorkerResult；
2. 对 `succeeded` Todo 标记 completed；
3. 对 `blocked` Todo 保持未完成状态；
4. 使用原始 `task` ToolCall 的 `tool_call_id` 生成父级 `ToolMessage`；
5. 返回 Supervisor。

例如：

```text
A succeeded
B succeeded
C blocked
```

join 之后：

```text
todos:
  A completed
  B completed
  C pending

worker_results:
  A -> result_A
  B -> result_B
  C -> blocked_result

messages:
  ToolMessage(A)
  ToolMessage(B)
  ToolMessage(C blocked)
```

Supervisor 下一轮再决定 C 怎么处理。

---

## 13. Retry / Replan / Finish

A-lite 不写死失败后的代码策略。

Supervisor 在观察结果后自行选择：

### 13.1 Retry

```text
A ✓
B ✓
C blocked

Supervisor
  ↓
task(C)
```

A/B 的 completed Todo 和结果继续保留，不重新执行。

### 13.2 Replan

```text
A ✓
B ✓
C blocked

Supervisor
  ↓
write_todos(...修改 C / 新增 D...)
  ↓
task(D)
```

Replan 只修改未来工作，不重建整个历史计划。

### 13.3 Finish

如果 A/B 已足够回答用户：

```text
Supervisor
  ↓
AIMessage(no tool_calls)
  ↓
END
```

因此核心循环是：

```text
Plan
 ↓
Act
 ↓
Observe
 ↓
Retry / Replan / Finish
```

---

## 14. Checkpoint、pending writes 与异常恢复

这是 A-lite 中需要明确区分的两条路径。

### 14.1 业务失败：正常 fan-in

例如：

```text
A → succeeded
B → succeeded
C → blocked
```

三个 Worker 都正常结束本次 node execution，因此同一 super-step 可以正常完成。

结果进入 join，再进入 Supervisor。

这条路径不依赖 pending writes 才能保留 A/B，因为整个 wave 正常完成并进入下一步。

### 14.2 Runtime 异常：交给 LangGraph

例如：

```text
同一 worker super-step：

A → succeeded
B → succeeded
C → provider timeout exception
```

C 的异常导致该 super-step 没有完整成功结束。

此时：

- A、B 已成功完成的 node writes 可以作为 pending writes 持久化；
- C 保持失败状态；
- Graph run 进入原生失败 / 可恢复状态；
- 不运行 join；
- 不让 Supervisor 基于半提交状态自行伪造恢复决策。

resume 时：

```text
A 不重跑
B 不重跑
C 重跑
```

C 成功后，该 super-step 才完整推进，随后进入 join。

因此 A-lite 的异常原则是：

> **Business failure becomes data; operational failure remains an exception.**

这可以最大化利用 LangGraph 原生 checkpoint 和 pending writes，而不是自建 recovery router。

### 14.3 副作用

checkpoint / pending writes 只能避免不必要的节点重算，不能撤销外部副作用。

任何有副作用的业务 Tool 仍需依赖 Tool/API 自身的幂等机制或已有 HITL 策略。

---

## 15. State 与 reducer

A-lite 的父级 state 应尽量小。

推荐：

```python
class PlanningState(MessagesState):
    todos: list[PlanningTodo]
    worker_results: dict[str, WorkerResult]
    loaded_skills: list[str]
```

其中：

- `messages` 使用官方 message reducer；
- `todos` 由 `write_todos` 和 join 更新；
- `worker_results` 以 `todo_id` 保存当前有效结果；
- Worker retry 可以更新同一 todo 的最新结果；
- 历史执行轨迹由 messages / trace / checkpoint 提供，不另建 attempt ledger。

不再保存：

```text
plan_generation
proposal
replacement_claims
ready_tasks
pending_task_calls
execution_id
wave_reservations
budget_usage
authorization_envelope
recovery_decision
planner_outcome
```

`pending_task_calls` 也不需要额外保存；当前 Supervisor `AIMessage.tool_calls` 就是这一轮 Task 请求的事实来源。

---

## 16. Controls

以下仍是普通控制 Tool：

- `load_skill`
- `load_skill_reference`
- `write_todos`

它们可以由 `ToolNode` 执行。

`task` 不进入该 ToolNode，因为它的作用不是执行普通函数，而是触发 Graph fan-out。

因此 Supervisor 路由可理解为：

```text
Supervisor AIMessage

control tool calls
  → controls ToolNode
  → supervisor

task tool calls
  → Send(worker) * N
  → join
  → supervisor

no tool calls
  → END
```

Skill 仍用于提供领域流程与知识，但 A-lite 不把 Skill 再扩展成 planning 专用授权系统。

---

## 17. Studio / Stream / Observability

A-lite 应优先保留 LangGraph 原生可观察性。

父 Graph 至少能看到：

```text
supervisor
controls
worker
join
```

Worker 内部是嵌套的 `create_agent` graph，可继续看到：

- Worker 模型调用；
- Worker ToolCall；
- Worker ToolMessage；
- Worker Agent loop；
- interrupt / resume。

Worker 采用“subgraph inside node”的方式而不是隐藏在 `task` Tool 内，原因之一就是保留更清楚的 subgraph state、stream 和 trace 边界。

Planning 入口继续使用 Agent Server / LangGraph 原生 stream，不新增 planning 专用事件协议作为运行必要条件。

---

## 18. 从旧设计删除的概念

A-lite 明确删除：

- 独立 Planner Agent；
- Planner phase；
- 独立 Finalizer Agent；
- Finalizer phase；
- Planning Scheduler 组件；
- upfront `NativePlanProposal`；
- strict DAG admission；
- `depends_on` runtime admission；
- ready-task 计算；
- plan generation；
- replacement ledger；
- recovery plan；
- bounded admission correction loop；
- planning global budget reservation；
- authorization envelope；
- capability → Tool projection；
- execution ID / attempt ledger；
- operational failure → custom WorkerOutcome 的统一包装。

A-lite 保留：

- Supervisor LLM；
- Todo；
- `task` ToolCall；
- Skill loading；
- `Send` 并行 Worker；
- Worker `create_agent`；
- Worker 私有 Agent messages；
- parent ToolMessage；
- checkpoint / resume；
- pending writes；
- Agent Server / Studio 原生观测；
- 项目已有且独立于 planning orchestration 的通用安全 middleware。

---

## 19. 关键行为示例

### 19.1 串行规划

```text
User
 ↓
Supervisor
 ↓
write_todos(A, B)
 ↓
Supervisor
 ↓
task(A)
 ↓
Worker A
 ↓
join
 ↓
Supervisor
 ↓
task(B)
 ↓
Worker B
 ↓
join
 ↓
Supervisor final answer
```

### 19.2 并行规划

```text
Supervisor
  ├─ task(A)
  ├─ task(B)
  └─ task(C)
        ↓
      Send*
   ┌────┼────┐
   ↓    ↓    ↓
   A    B    C
   └────┼────┘
        ↓
       join
        ↓
   Supervisor
```

### 19.3 A/B 成功、C 业务失败

```text
A ✓
B ✓
C blocked
   ↓
join
   ↓
Supervisor
   ├─ retry C
   ├─ replan C → D
   └─ finish with A/B
```

A/B 结果继续保留。

### 19.4 A/B 成功、C Runtime 异常

```text
A ✓ ─ pending write
B ✓ ─ pending write
C ✗ ─ exception

Graph paused/failed
      ↓ resume
A skip
B skip
C rerun
      ↓
join
      ↓
Supervisor
```

这条路径由 LangGraph 原生恢复，不经过 Supervisor recovery planning。

---

## 20. 验证策略

第一阶段验证重点不是治理覆盖率，而是证明 A-lite 的核心 planning loop 成立。

### 20.1 Supervisor

- Supervisor 不使用 `create_agent`；
- Supervisor 能创建 Todo；
- Supervisor 能派发一个 Todo；
- Supervisor 能一次派发多个 Todo；
- Supervisor 能消费 Worker ToolMessage；
- Supervisor 能 retry / replan / finish；
- 无 ToolCall 时直接 END。

### 20.2 Worker

- Worker 是唯一 `create_agent`；
- Worker 拥有私有 messages；
- Worker 可以调用业务 Tool；
- Worker 正常业务失败返回 `blocked`；
- Worker operational exception 不被误包装为 `blocked`。

### 20.3 并行

- 一次三个 `task` 调用展开为三个 Worker invocation；
- 三个 Worker 可以处于同一并行 super-step；
- 正常完成后只执行一次 join；
- join 能稳定生成三个父级 ToolMessage。

### 20.4 Replan

- A/B completed 后，C retry 不重跑 A/B；
- C 被替换为 D 时，A/B 结果仍存在；
- completed Todo 不被后续 `write_todos` 静默覆盖。

### 20.5 Pending writes

构造：

```text
A success
B success
C raises exception
```

验证：

- run 在 Worker super-step 失败；
- A/B 成功 writes 已持久化；
- resume 不重新执行 A/B；
- 只重新执行 C；
- C 成功后进入 join；
- Supervisor 最终看到 A/B/C 完整结果。

### 20.6 Observability

- Studio / trace 能看到 supervisor；
- 能看到动态 worker invocations；
- 能看到 Worker 内部 create_agent 模型与 Tool 调用；
- resume 后能够区分已恢复分支与未重跑分支。

---

## 21. 验收标准

A-lite 实施完成后，应满足：

1. Planning 模式只有 Worker 使用 `create_agent`。
2. Supervisor 是普通 LLM Node。
3. Planner、Scheduler、Finalizer 不再作为独立运行时角色存在。
4. Todo 是 Supervisor working memory，而不是严格 DAG 协议。
5. `task(todo_id)` 是 Supervisor 到 Worker 的标准委派协议。
6. 一个 Supervisor AIMessage 可以通过多个 `task` 动态并行多个 Worker。
7. 并行 Worker 使用 LangGraph `Send` fan-out，并在 join fan-in。
8. Worker 业务失败以结构化结果回到 Supervisor。
9. Supervisor 可以基于成功结果和失败结果 retry / replan / finish。
10. 已成功 Todo 与 WorkerResult 在 replan 后继续保留。
11. Worker runtime exception 直接交给 LangGraph。
12. 同一 super-step 中其他已成功 Worker 利用 pending writes 在 resume 时不重跑。
13. 不再维护 planning 专用 execution / generation / budget / authorization ledger。
14. 正常结束时由 Supervisor 无 ToolCall 的 `AIMessage` 直接完成 run。
15. Studio / trace 能观察父 Planning Graph 与 Worker subgraph。

---

## 22. 最终决策摘要

- **Planning Agent 是谁？** 整张 Planning Graph。
- **Supervisor 是 Agent 吗？** 语义上承担全局 Agent 推理，但实现上只是普通 LLM Node，不使用 `create_agent`。
- **谁使用 `create_agent`？** 只有 Worker。
- **Planner 在哪里？** 不存在独立 Planner；规划就是 Supervisor 调用 `write_todos` 的行为。
- **Scheduler 在哪里？** 不存在独立 Scheduler；Task routing 只是把 ToolCall 转成 `Send(worker)`。
- **Todo 是 DAG 吗？** 不是严格 dependency DAG；它是 planning working memory。
- **还能并行吗？** 可以；同一 Supervisor AIMessage 的多个 `task` 通过 `Send` 动态 fan-out。
- **还能体现 DAG 思想吗？** 可以；每个 wave 会动态展开局部 execution DAG，并在 join fan-in。
- **A/B 成功、C 业务失败怎么办？** 正常 join，Supervisor 基于 A/B/C 结果决定 retry / replan / finish。
- **A/B 成功、C runtime exception 怎么办？** 异常冒泡；LangGraph pending writes 保留 A/B 成功 writes，resume 时只重跑 C。
- **Replan 会丢 A/B 吗？** 不会；completed Todo、WorkerResult、messages 都保留在 checkpointed state 中。
- **Finalizer 在哪里？** 不存在；Supervisor 无 ToolCall 的回答就是 final answer。

---

## 23. 参考资料

- LangGraph Graph API overview: `https://docs.langchain.com/oss/python/langgraph/graph-api`
- LangGraph Checkpointers: `https://docs.langchain.com/oss/python/langgraph/checkpointers`
- LangGraph Subgraphs: `https://docs.langchain.com/oss/python/langgraph/use-subgraphs`
- LangChain `create_agent`: `https://reference.langchain.com/python/langchain/agents/factory/create_agent`
- LangGraph `ToolNode`: `https://reference.langchain.com/python/langgraph.prebuilt/tool_node/ToolNode`
