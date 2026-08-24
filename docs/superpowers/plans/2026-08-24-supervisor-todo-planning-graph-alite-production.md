# Supervisor Todo Planning Graph A-lite Production Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将生产 planning 模式从 Planner / Scheduler / Finalizer 与 planning ledger 重构为 Supervisor / Controls / Worker / Join 的 A-lite 原生 LangGraph 循环，并通过 `assistant-native-v3` 明确隔离旧 checkpoint。

**Architecture:** `AssistantRootGraph` 的 planning 分支仍是显式 `StateGraph`。Supervisor 是绑定 `load_skill`、`load_skill_reference`、`write_todos`、`task` 的普通 LLM node；控制 Tool 进入标准 `ToolNode`，`task(todo_id)` 由 conditional edge 转换为并行 `Send("worker", ...)`；Worker 复用生产唯一 `AssistantFastAgent` 的 `create_agent` loop，但通过 worker phase 只暴露业务 Tool 并输出严格 `WorkerResult`；join 确定性合并本轮结果、生成父级 `ToolMessage` 并回到 Supervisor。业务 blocked 作为数据，operational exception 原样传播并使用 LangGraph pending writes 恢复。

**Tech Stack:** Python 3.11、LangChain `create_agent` / middleware / `ToolStrategy`、LangGraph `StateGraph` / `ToolNode` / `Send` / checkpoint、Pydantic v2、pytest、LangGraph Agent Server / Studio。

---

## Task 1：建立生产 A-lite 合约的 RED 测试

**Files:**

- Create: `tests/tdd/supervisor-todo-planning-alite-production/README.md`
- Create: `tests/tdd/supervisor-todo-planning-alite-production/test_supervisor_contract.py`
- Create: `tests/tdd/supervisor-todo-planning-alite-production/test_worker_lifecycle.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`
- Modify: `tests/core/INVARIANTS.md`

1. 从已通过的 experiment probe 复用 scripted model 思路，但测试必须导入生产 `build_planning_graph`、生产 state/model/tool factory；不得把 experiment graph 当被测对象。
2. 把 `LOOP-001` 改为稳定结构化契约：planning 显式图包含 `supervisor/controls/worker/join`，Supervisor 直接结束或以 task ToolCall 动态 fan-out，Worker 是共享 fast `create_agent` 的 scoped invocation，join 回写标准 messages；不再登记 Planner/Scheduler/Finalizer、admission、budget/recovery ledger。
3. 把 `CTX-001` 改为：父 conversation、Memory/TrustedRuntimeFacts 与 Skill state 有界投影到 Worker；Worker 私有 transcript 不写回父 messages；planning 非 read 业务 Tool 仍走原生 HITL；completed Todo 与 succeeded result 单调保护。
4. 覆盖以下真实行为：
   - Supervisor control / tasks / final 三类输出；control+task、未知 call、重复 task、未知或 completed todo fail closed；
   - `write_todos` 可替换 pending、新增/放弃 pending，但不得删除、改写或降级 completed Todo；
   - 一次多个 task 真实并行，join 生成匹配原 `tool_call_id` 的 `ToolMessage`；
   - blocked 保持 Todo pending，succeeded 标为 completed；retry 只重跑指定 Todo；
   - Worker 输入不包含完整父 messages，Worker 内业务 ToolMessage 不泄漏父 messages；
   - operational exception 首次失败、同 thread `ainvoke(None)` 恢复后只重跑失败 Worker；
   - planning 非 read Worker Tool 在执行前 interrupt，resume 后已完成分支不重放。
5. 先运行最小新 TDD 与两个 core 文件并确认因旧生产 API/拓扑失败，而非 fixture 或 import 错误：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-production \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py
```

Expected: FAIL，至少明确显示旧 graph nodes / legacy state contract 不满足 A-lite。

## Task 2：收敛 Planning model、state 与 reducer

**Files:**

- Modify: `src/assistant_agent/native_agent/models.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `tests/tdd/supervisor-todo-planning-alite-production/test_supervisor_contract.py`

1. 定义严格 Pydantic 模型：

```python
class PlanningTodo(BaseModel):
    todo_id: str
    content: str
    status: Literal["pending", "completed"]

class WorkerResult(BaseModel):
    todo_id: str
    status: Literal["succeeded", "blocked"]
    summary: str

class WorkerWrite(BaseModel):
    task_call_id: str
    result: WorkerResult
```

2. 将 `PlanningState` 收敛为父级必要字段：`messages`、`memory_context`、`memory_status`、`trusted_runtime_facts`、`todos`、`worker_results`、`worker_writes`、`active_skill_ids`、`skill_reference_grants`。`worker_writes` 只服务一次 fan-in，join 后使用 `Overwrite([])` 清空。
3. 将 `WorkerState` 收敛为 `todo_id/content/task_call_id`、Skill/reference ID、Memory/TrustedRuntimeFacts 和 Agent 运行所需字段；不携带父级完整 messages、plan generation、attempt、execution ID、dependency、budget 或 allowlist。
4. `merge_worker_results` 允许 blocked 被后续结果替换，禁止 succeeded 被不同结果覆盖；相同 succeeded replay 幂等。Todo 替换函数单独校验 ID 唯一、completed 不得删除/改写/降级。
5. 删除生产不再使用的 planning 类型与 reducer：`NativePlanProposal`、plan/deliverable/node、PlannerEvidence/Outcome、WorkerOutcome/Completion、BudgetUsage、authorization/replacement/recovery/wave ledger。先用 `rg` 确认剩余引用仅位于明确过期的临时 TDD 目录。
6. 运行模型/state 定向测试直到 GREEN。

## Task 3：实现 Supervisor、Controls 与 Todo Tool

**Files:**

- Rewrite: `src/assistant_agent/native_agent/planning_graph.py`
- Modify: `tests/tdd/supervisor-todo-planning-alite-production/test_supervisor_contract.py`

1. 从静态 Tool inventory 精确解析 `load_skill` 和 `load_skill_reference`，构造本地标准 `write_todos` 与不可执行路由 schema `task`；缺失或重复 control Tool 时 composition fail closed。
2. Supervisor 使用原始 `BaseChatModel.bind_tools`，不调用 `create_agent`。Prompt 必须包括：用户目标、Todo 工作记忆、已加载 Skill 正文索引、Worker 结果、四类输出约束、禁止混合 control/task。
3. Supervisor 只读取父级标准 messages；Skill/reference 的正文从受信 `SkillCatalog` 与 state 中的 ID 机械投影，不能由模型结果扩权。Supervisor 固定关闭 Provider-native search，业务联网只由 Worker 完成。
4. `classify_supervisor_action` 接受：单个 control call（`load_skill|load_skill_reference|write_todos`）、一个或多个纯 task call、无 ToolCall final；其余均抛稳定本地 contract error。
5. `ToolNode` 只注册三个 Controls，执行后回到 Supervisor。`task` 永不进入 ToolNode。
6. no-tool-call 的 Supervisor `AIMessage` 直接进入 `END`，不设置 Finalizer。
7. 运行 Supervisor/Todo 测试直到 GREEN。

## Task 4：实现 scoped Worker、动态 fan-out 与确定性 join

**Files:**

- Modify: `src/assistant_agent/native_agent/planning_graph.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Create: `src/assistant_agent/native_agent/planning_worker.py`
- Modify: `tests/tdd/supervisor-todo-planning-alite-production/test_worker_lifecycle.py`

1. 保持 `build_fast_agent` 是 production 唯一 `create_agent` 构造入口；移除 Planner/Finalizer projection，增加窄 `PlanningWorkerMiddleware`：`agent_phase=worker` 时隐藏 planning Controls、设置 `ToolStrategy(WorkerResult)`、保留 Progressive/Conditional exposure、read retry、per-Tool limit、summarization、Memory/TrustedRuntimeFacts 与 planning 非 read HITL。
2. 用官方 `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware` 取代 planning 自建 phase budget，仅作为每个 fast/worker agent invocation 的通用安全上限；不向 planning 父 state 写 usage ledger。
3. `dispatch_tasks` 从最新 Supervisor `AIMessage.tool_calls` 与当前 pending Todo 生成多个 `Send("worker", WorkerState)`；拒绝未知、completed、重复 Todo。Worker state 只携带当前 Todo、原 task call ID、必要 Skill/reference ID、Memory 和 TrustedRuntimeFacts。
4. Worker wrapper 为共享 fast agent 生成一个私有 `HumanMessage`，显式设置 `execution_mode=planning`、`agent_phase=worker`、active Skill/reference state；调用结果严格校验 `structured_response` 和 todo ID。不要 catch `Exception` 并转 blocked。
5. Worker 仅返回 `worker_writes=[WorkerWrite(...)]`；同一 wave 由 reducer fan-in。join 按 todo ID 稳定排序：更新 todos、合并最新 result、为每个原 task call ID 生成标准 `ToolMessage(name="task")`、清空 worker_writes，然后回 Supervisor。
6. 运行并行、blocked/retry、pending-writes、私有上下文与 HITL 测试直到 GREEN。

## Task 5：删除旧 Planning Runtime 并更新 composition

**Files:**

- Delete: `src/assistant_agent/native_agent/planning_phase.py`
- Delete: `src/assistant_agent/native_agent/planning_recovery.py`
- Delete: `src/assistant_agent/native_agent/planning_budget.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/native_agent/providers.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`

1. `AgentServerExecutionOwner.compose()` 不再创建/注入 `PlanningBudgetPolicy`。同一 `fast_agent` 注入 planning graph；配置的 `max_tool_iterations` 仅传给通用 fast/worker model/tool call limit。
2. 删除 Mock Provider 对 `NativePlanProposal` / `WorkerCompletion` 的旧 structured schema 特判，新增或调整 `WorkerResult` 的离线响应支持，但不根据用户关键词决定生产路由。
3. `rg` 全仓确认生产源码不再引用 Planner/Scheduler/Finalizer、planning phase/budget/recovery、NativePlanProposal、authorization envelope 与 ledger 字段。
4. 过期 `tests/tdd/native-high-agency-planner`、`tests/tdd/planning-recovery-routing` 不删除；在新 feature README 说明它们绑定已退役实现、只由用户手动删除，且不属于默认收集。
5. 运行新 TDD 与两个 core invariant 测试直到 GREEN。

## Task 6：以 `assistant-native-v3` 隔离旧 checkpoint

**Files:**

- Modify: `langgraph.json`
- Modify: `src/assistant_agent/agent_server/config.py`
- Modify: `tests/core/contract/test_gateway_contract.py`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/observability-harness.md`

1. 将唯一生产 chat graph ID 更新为 `assistant-native-v3`，不注册 v2 alias；保持 `PLANNING_ASSISTANT_ID=4cf38057-6071-50ca-a565-98b7854d763e`，name 更新为 `assistant-native-v3-planning`，启动时由既有 auth normalization 改绑到 v3。
2. 更新 `GATE-001`：v1/v2/unknown thread 与 legacy checkpoint 只读，不能进入 v3 run/resume/replay/stream。新 thread metadata 精确绑定 v3。
3. 文档明确部署顺序：枚举并 drain/cancel v2 pending/interrupt runs；Studio 继续选择固定 planning assistant，但必须创建新 thread；旧链接中的 v2 thread 不可恢复到 A-lite。completed 历史只读，不做 state migration。
4. 更新 gateway 测试并运行：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/contract/test_gateway_contract.py
```

Expected: PASS，且 graph endpoint / thread guard 均为 v3。

## Task 7：同步 authority 并执行完整离线验证

**Files:**

- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/agent-server-architecture.md`
- Modify: `tests/tdd/supervisor-todo-planning-alite-production/README.md`

1. 重写旧 planning 章节，只描述 A-lite 生产事实：Supervisor 普通 LLM node、Controls ToolNode、task Send、共享 fast Worker、join、business blocked 与 operational pending writes、父/Worker context 边界、v3 checkpoint 隔离。
2. 删除 authority 中 Planner/Finalizer/authorization envelope/global planning budget/recovery custom event 的现行声明；保留通用 Tool governance、read retry、per-tool limiter、HITL、Memory、coding 与媒体边界。
3. 运行 authority validator：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

4. 运行定向与默认 core：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-production

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

5. 运行静态编译：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent tests/tdd/supervisor-todo-planning-alite-production
```

6. 记录全部 exit status、pytest item 数与 provider mode。不得调用真实 Provider。

## Task 8：验证唯一 8089 服务 hot reload 与 Studio graph

**Files:**

- Modify: `tests/tdd/supervisor-todo-planning-alite-production/README.md`

1. 将实现分支提交并合入当前 `cqy` 后，等待 PyCharm 管理的唯一 `8089` 服务 hot reload；不得启动第二套 dev server。
2. 查询 `/assistants/{PLANNING_ASSISTANT_ID}` 与 graph endpoint，确认 assistant name/graph_id 已是 v3，父 graph 展开包含 A-lite planning subgraph，且旧 planning runtime node 不再出现。
3. 用 mock/offline 新建 v3 thread 发起 planning run，订阅 native updates/messages 且启用 subgraph stream；确认 `supervisor/controls/worker/join` 与 Worker 内 model/tools namespace 可观察。
4. 不在旧 v2 thread 上创建或恢复 run。若用户原链接继续指向旧 thread，说明需在同一 assistant 下新建 thread。
5. 在 README 记录 endpoint、结构化 node 集合、mock run 状态与限制；不记录 token、用户数据或完整 Provider 响应。

## 完成标准

- 生产 planning graph 只保留 A-lite 四类动作；旧 Planner/Scheduler/Finalizer 与 planning ledger 源码已删除。
- Supervisor 不是 `create_agent`；Worker 复用唯一生产 fast `create_agent`。
- 并行、blocked/retry、operational pending writes、scoped context、HITL 均由生产测试证明。
- `assistant-native-v3` 阻止旧 v2 checkpoint 进入新拓扑。
- 新 TDD、全部 core、compileall、authority validator 与 8089 mock/Studio 结构化检查均通过。
- 未调用真实 Provider；未删除历史临时 TDD 目录；未 push、merge PR 或写入秘密。
