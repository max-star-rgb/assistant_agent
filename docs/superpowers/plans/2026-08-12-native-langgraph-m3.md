# 原生 LangGraph M3：Workflow v2 纵切实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `deep_research` 的 Workflow v2 从 planner 到并行 worker、join、verifier、最小 repair、interrupt/resume 和 publish 全部由可持久恢复的 `DurableWorkflowGraph` 原生执行，并使旧 Workflow scheduler 不再执行该 Workflow 类型。

**Architecture:** 使用 LangGraph Graph API 构建父 `StateGraph`：planning subgraph 复用 `AssistantTurnGraph.planner`，确定性 admission 把 Workflow v2 proposal 转成静态 DAG；条件边按依赖波次返回 `Send`，Pregel super-step 在 reducer 合并全部独立 branch result 后进入 join；verifier 复用 `AssistantTurnGraph.verifier` 并以 `Command` 路由 repair/publish/fail。LangGraph checkpointer 是执行位置、pending task、interrupt 与 resume 的唯一事实源，Workflow SQLite 只保留 submission、owner、artifact、审计与产品查询摘要；LangSmith 直接观测和评估实际父图/子图树。

**Tech Stack:** Python 3.12、LangGraph 1.2.4 Graph API、langgraph-checkpoint 4.1.1、M2 授权后锁定的官方 async SQLite saver、Pydantic v2、asyncio、LangSmith、pytest。

## Global Constraints

- 本计划严格服从 `docs/superpowers/specs/2026-08-12-native-langgraph-graph-engineering-design.md`，只实施 M3；发生冲突时以该 spec 为准，不把当前旧代码行为反向写成目标。
- 主执行模型只能使用 Graph API：真实 `StateGraph`、严格 `State`、node、普通 edge、`START`、`END`、conditional edge、`Command`、`Send`、reducer、subgraph、Pregel super-step、compile、async invoke/stream、checkpoint、checkpointer、thread、interrupt/resume、runtime context、`RetryPolicy`、`TimeoutPolicy` 和 `error_handler`。不得用 Functional API、手写调度循环或把旧 `WorkflowRuntime.run_claim()` 包成单个 graph node 冒充迁移。
- Graph API 硬约束清单中的 Memory/Store、Streaming Modes、Time Travel、Replay、Fork 必须持续按职责判断：M3 使用 runtime context、业务 artifact/memory service 和 native stream/checkpoint；Time Travel、Replay、Fork 的产品能力仍由 M5 验收，M3 不得为其新建自研等价物。
- `WorkflowPlanV2Proposal`、`WorkflowPlanVersion`、typed acceptance contract、deliverable binding、constraint binding 和静态 DAG admission 保留领域语义；`Send`、Pregel task、checkpoint ID、checkpoint namespace 和 interrupt ID 不进入 Workflow v2 wire schema 或公共 API。
- M2 Task 2 的官方 async SQLite checkpointer 是 M3 跨进程恢复硬 gate。用户未授权安装依赖时，可以用 `InMemorySaver` 完成 Task 1–4 的 Graph 结构和确定性 RED/GREEN，但不得修改依赖、不得用自研 saver、不得以 memory fallback 宣称 Task 5 或 M3 完成。
- 生产配置缺官方 saver、数据库不可建或 async saver setup 失败时必须启动失败；不得静默回退 `InMemorySaver`。M3 workflow graph 必须与 M2 `AssistantRuntimeApp` 共用进程级 saver owner 生命周期，不能另开后台 event-loop thread。
- 每个 Durable Workflow 使用独立、稳定、owner-bound `thread_id`；每次首次 invoke 或 resume 使用新的 `run_id`。`workflow_id` 是业务身份，`thread_id/run_id` 是 graph 身份，LangSmith 只关联而不拥有这些身份。
- 并行 `Send` child 只能接收窄、不可变、可序列化的 branch input。每个 child 必须创建独立 `AgentState`、`GraphRuntimeContext`、Tool budget 和 stream writer 绑定；绝不让两个 branch 共享或原地修改同一个 mutable `AgentState`。
- worker result reducer 必须满足顺序无关、结合、交换、重放幂等；同一 `node_id + execution_generation` 的不同结果必须 fail closed。repair 使用更高 generation 覆盖旧结果，不通过删除/原地改写共享 dict 实现。
- 所有本地显式 Tool 继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`；worker/verifier profile 只能收窄 catalog，不能绕过 M2 operation barrier、安全、授权、schema 或幂等治理。
- 只有瞬时基础设施异常进入原生 `RetryPolicy`；业务拒绝、非法 plan、acceptance failure 和权限错误不自动重试。node timeout 使用 `TimeoutPolicy`，重试耗尽由 `error_handler` 转为结构化 graph failure/fallback，不吞成成功文本。
- `astream(..., version="v2", subgraphs=True, durability="sync")` 是主运行路径；按需启用 `updates/custom/tasks/checkpoints/messages`。禁止 `invoke() + asyncio.to_thread`、`ThreadPoolExecutor`、work-item poll/claim/lease heartbeat 推进 `deep_research` DAG。
- 兼容保护以真实消费者 inventory 为准：Agent-Service/media wire，以及 `scripts/media_simulator.py` 实际读取的
  workflow handle、status/progress、cursor events、result content 和 waiting-input action 保持不变。未被这些消费者
  读取的 Workflow HTTP 字段/route/internal Bundle 不受保护，允许 breaking cleanup；入口仍只能调用薄 graph
  application/service，不能读取 checkpoint 内部结构决定下一节点。
- transport disconnect 只停止订阅，不取消 Workflow；cancel 是产品终止意图；interrupt 是可恢复等待。write/publish/delivery 保持稳定 operation key 与 commit barrier。
- LangSmith 是 M3 唯一新增 trace/eval 目标；不新增 Langfuse span、runner、evaluator 或双平台抽象。M3 不删除全部 Langfuse（M5 负责），但不允许 canonical OTel/Workflow observer 为 `deep_research` 重建影子 graph tree。
- 默认测试必须 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、local/offline、无真实 Provider、无网络。所有 M3 RED/GREEN 放入可手动删除的 `tests/tdd/native-langgraph-m3/`；只有 Task 8 依据已登记 invariant 修改最小 core 测试。
- `long_horizon` 当前没有产品消费者，但其旧 scheduler 兼容只保留到 M4；M3 不因零消费者误删整个通用 Workflow 包。`src/assistant_agent/automation/durable_tasks/**` 是另一套受保护 durable task 产品能力，绝不纳入本次删除范围。

## 文件职责图

| 文件 | 单一职责 |
| --- | --- |
| `src/assistant_agent/workflows/graph_state.py` | 严格、版本化、checkpoint-safe 的 `DurableWorkflowState`、branch input/result DTO 与 reducer |
| `src/assistant_agent/workflows/graph_context.py` | 父图 runtime-only 依赖，以及为每个并行 child 创建隔离 `GraphRuntimeContext` 的 factory |
| `src/assistant_agent/workflows/planning_graph.py` | planner profile child 的窄包装、v2 parser 和确定性 admission planning subgraph |
| `src/assistant_agent/workflows/durable_graph_nodes.py` | wave 计算、worker/verifier child 包装、join、repair、publish/fail node；不负责建图 |
| `src/assistant_agent/workflows/durable_graph.py` | 唯一 `DurableWorkflowGraph` topology、原生 policy 配置和 compile |
| `src/assistant_agent/workflows/durable_graph_app.py` | thread/run identity、async stream/invoke/resume/state snapshot 与 authoritative outcome facade |
| `src/assistant_agent/workflows/graph_projection.py` | native graph/state/custom fact 到现有 Workflow business record/event/API progress 的单向投影 |
| `src/assistant_agent/workflows/graph_host.py` | 进程内 async task owner；提交、恢复非终态 graph thread、停止订阅和 shutdown，不做 DAG 调度 |
| `evals/langsmith_workflow_regression/**` | 直接运行 compiled workflow graph 的 LangSmith Dataset/Experiment/evaluator/completeness |
| `tests/tdd/native-langgraph-m3/**` | M3 临时、显式、offline RED/GREEN 证据；用户可手动整目录删除 |

## 执行前 Gate 0：Saver 与 execution engine discriminator

Gate 0 不增加第九个 implementation task；它是 Task 1 开始前记录、Task 5/6 再关闭的风险门。执行 agent
必须先把结果写入 `.superpowers/sdd/2026-08-12-native-langgraph-m3/progress.md`：

- [ ] 检查 M2 Task 2 是否已有 official async SQLite saver dependency、`open_async_checkpointer()`、跨 Runtime
  recovery test 与进程级 async owner 的已通过证据。缺任一项时标记 `persistent_saver_gate=pending`，只授权
  Task 1–4 使用 `InMemorySaver`；不得安装包或修改 dependency lock。
- [ ] 在 Task 1 首个 commit 定义唯一 execution discriminator：
  `WorkflowExecutionEngine = Literal["legacy_scheduler_v2", "langgraph_v3"]`。
  `WorkflowRecord.execution_engine` 对旧 JSON 缺字段只兼容读为 `legacy_scheduler_v2`；所有新 Deep Research
  submission 必须显式写 `langgraph_v3`，不能依赖 default。
- [ ] 在任何 production `langgraph_v3` record 可创建前，先让 legacy
  `claim_ready_work_item()`/`DurableWorkflowWorker` 接收显式 engine/type allowlist，并在 store 事务内过滤；
  `langgraph_v3` record 即使带旧式 `ready` 字段也永远不能被 claim。该负向测试必须先于 Task 6 cutover 通过。
- [ ] `DurableWorkflowState.execution_engine` 固定为 `Literal["langgraph_v3"]`；Graph app 拒绝加载
  `legacy_scheduler_v2` business record，旧 scheduler 拒绝 graph record。不存在 `auto`、按 plan kind 猜测或
  失败后跨 engine fallback。

## Graph API capability disposition

| 能力 | M3 disposition | 直接证据 / owner |
| --- | --- | --- |
| `StateGraph` / State / Node | **实施** | Tasks 1–4 的 strict state、planning/worker/verifier/join nodes 与 compiled parent graph |
| Edge / `START` / `END` | **实施** | Task 3 topology inspection；终态只经 publish/fail/cancel 到 `END` |
| Conditional Edge | **实施** | Task 3 wave router 返回 `Send`/terminal route；不得并存同目的 static edge |
| `Command` | **实施** | Task 4 verifier repair/publish/fail，Task 6 cancel；Command node 只声明 destinations，不加静态控制 edge |
| `Send` | **实施** | Task 3 按 ready wave fan-out；branch input 窄且每 branch runtime context 独立 |
| Reducer | **实施** | Task 1 `(node_id,generation)` 持久 ledger reducer 的严格交换/结合/幂等与 conflict fact tests；latest 结果纯派生 |
| Subgraph | **实施** | planning wrapper 与 planner/worker/verifier `AssistantTurnGraph` profile namespace |
| Pregel / Super-step | **实施** | Task 3 native tasks/checkpoints stream 证明同 wave 并行和全量完成后 join |
| Compile | **实施** | Task 3 唯一 builder；standalone parent attach saver，child `checkpointer=None` 继承 namespace |
| Invoke | **有限使用** | deterministic node/graph tests 可 `ainvoke`；production facade 以 async stream 为主 |
| Stream / Streaming Modes | **实施** | Tasks 3、5、6 使用 v2 `updates/custom/tasks/checkpoints`，必要 child message stream 不进入公共协议 |
| Checkpoint / Checkpointer / Thread | **实施，受 Gate 0 约束** | Task 5 official async SQLite、stable workflow thread、state snapshot/history |
| Interrupt / Resume | **实施** | Task 5 branch interrupt、multi-interrupt ID map、同 thread 新 run `Command(resume=...)` |
| Memory | **领域服务保留** | memory/artifact 正文不进 state；child 只经 branch-local Runtime Context 调既有治理服务 |
| Store | **按真实需求使用** | LangGraph checkpointer 保存执行位置；业务 SQLite 保存 owner/artifact/audit/projection；M3 不为覆盖名词强塞 `BaseStore` |
| Runtime Context | **实施** | Task 1 由 checkpoint-safe assignment/owner/capability/tool-scope facts + runtime services 纯重建；cache 不是恢复事实源 |
| Retry Policy | **实施** | Task 4 仅 transient exception 的 native `RetryPolicy` |
| Timeout | **实施** | Task 4 node `TimeoutPolicy`，不以线程 join timeout 模拟 |
| Fallback | **实施** | Task 4 native `error_handler(NodeError)` 输出结构化 failure route |
| Time Travel / Replay / Fork | **M5 产品化** | M3 只保留 native state history 和 reducer replay-safety，不创建自研 time-travel/fork API |

---

### Task 1: 严格 Workflow graph state、版本化 reducer 与隔离 Runtime Context

**Files:**
- Create: `src/assistant_agent/workflows/graph_state.py`
- Create: `src/assistant_agent/workflows/graph_context.py`
- Modify: `src/assistant_agent/workflows/models.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_graph_state.py`
- Create: `tests/tdd/native-langgraph-m3/state_inventory.md`

**Interfaces:**
- Consumes: `WorkflowSubmission`、`WorkflowPlanVersion`、`WorkflowBudget`、M2 `AssistantTurnGraphApp.graph_for_profile()`、`profile_input_adapter()` 和 `profile_output_adapter()`。
- Produces: `WorkflowExecutionEngine`、`DurableWorkflowState`、`WorkflowProfileAssignment`、`WorkflowNodeResult`、`WorkflowResultSlot`、`WorkflowResultConflict`、`WorkflowGraphError`、`merge_result_ledger(left, right)`、`latest_results(ledger, generations)`、`merge_graph_errors(left, right)`、`initial_workflow_graph_state(...)`、`validate_durable_workflow_state(...)`。
- Produces: `BranchProfileContextFactory.context_for_state(child_state) -> GraphRuntimeContext` 与 immutable `WorkflowGraphRuntimeContext`；factory 只依赖 checkpoint-safe child facts 和 process-owned runtime services。可选 cache 仅优化同进程重复构造，清空或跨进程缺失时结果等价。
- Changes: `GraphRuntimeContext.child_context_resolver` 是 M3 唯一新增的 child 隔离 hook；M2 `bind_checkpointed_runtime_node()` 在读取 `agent_state/tool_executor` 前先以当前 child checkpoint state 解析 branch-local context，因此 compiled `AssistantTurnGraph` 可以作为真实 subgraph node，而不是在 wrapper 内手工 `ainvoke()`。
- Persistent identity: `graph_name="DurableWorkflowGraph"`、`graph_version="3"`、`state_schema_version=1`。

- [ ] **Step 1: 写严格 state 与 reducer RED**

```python
def result(node_id: str, generation: int, summary: str) -> WorkflowNodeResult:
    return WorkflowNodeResult(
        node_id=node_id,
        execution_generation=generation,
        profile="worker",
        status="succeeded",
        summary=summary,
        artifact_refs=(f"artifact://{node_id}/{generation}",),
    )


def test_result_ledger_reducer_is_associative_commutative_and_idempotent():
    a0 = result("a", 0, "a0")
    b0 = result("b", 0, "b0")
    conflicting_a0 = result("a", 0, "conflict")
    updates = [ledger_update(a0), ledger_update(b0), ledger_update(conflicting_a0)]

    outcomes = {
        canonical_ledger(functools.reduce(merge_result_ledger, order, {}))
        for order in itertools.permutations(updates)
    }
    assert len(outcomes) == 1
    merged = functools.reduce(merge_result_ledger, updates, {})
    assert merge_result_ledger(merged, merged) == merged
    assert result_conflicts(merged) == (
        WorkflowResultConflict(node_id="a", execution_generation=0, ...),
    )
    with pytest.raises(WorkflowGraphStateConflict, match="a.*generation 0"):
        latest_results(merged, {"a": 0, "b": 0})
```

同时覆盖：unknown/extra state 字段被拒绝；Provider client、Registry、Executor、DB connection、event sink、callback、cancel token、绝对路径、credential、artifact/media 正文不能序列化入 state；`WorkflowProfileAssignment` 只含后续 Step 4 明列的 checkpoint-safe identity、semantic input、artifact/capability/tool-scope refs 和 budget slice。

- [ ] **Step 2: 运行 RED 并确认缺少 graph state**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_graph_state.py
```

Expected: collection/import FAIL，缺少 `assistant_agent.workflows.graph_state`。

- [ ] **Step 3: 实现严格 DTO、TypedDict channel 和 reducer**

```python
class WorkflowNodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    execution_generation: int = Field(ge=0, le=64)
    profile: Literal["worker", "verifier"]
    status: Literal[
        "succeeded", "retryable_failed", "repair", "waiting_input", "failed"
    ]
    summary: str = Field(default="", max_length=4_000)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    error_code: str | None = Field(default=None, max_length=160)
    repair_node_ids: tuple[str, ...] = Field(default=(), max_length=64)
    input_request: PersistedWorkflowInputRequest | None = None
    model_calls_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)


class WorkflowResultSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    execution_generation: int = Field(ge=0, le=64)
    variants_by_digest: dict[str, WorkflowNodeResult] = Field(max_length=2)
    conflict: WorkflowResultConflict | None = None


class DurableWorkflowState(TypedDict):
    graph_name: Literal["DurableWorkflowGraph"]
    graph_version: Literal["3"]
    state_schema_version: Literal[1]
    execution_engine: Literal["langgraph_v3"]
    workflow_id: str
    workflow_type: str
    workflow_thread_id: str
    invocation_run_id: str
    submission: PersistedWorkflowSubmission
    admitted_plan: PersistedAdmittedWorkflowPlan | None
    status: WorkflowStatus
    phase: WorkflowPhase
    execution_generation_by_node: dict[str, int]
    active_wave: tuple[WorkflowProfileAssignment, ...]
    result_ledger: Annotated[dict[str, WorkflowResultSlot], merge_result_ledger]
    pending_inputs_by_node: dict[str, PersistedWorkflowInputRequest]
    repair_round: int
    budget: PersistedWorkflowBudget
    result_artifact_refs: tuple[str, ...]
    errors: Annotated[tuple[WorkflowGraphError, ...], merge_graph_errors]
```

`PersistedWorkflowSubmission` 和 admitted plan 必须逐字段复制既有领域 DTO 的 primitive/enum/有界 tuple/稳定 ref；不得保留任意 `dict[str, JsonValue]` 逃生舱。`workflow_inputs` 只允许当前 Workflow v2 已登记的 typed input DTO；兼容旧 record 的任意 dict 留在 business store，不进入新 checkpoint。

同一步在 `models.py` 增加 `WorkflowRecord.execution_engine`：legacy 反序列化缺字段时迁移成
`legacy_scheduler_v2`；新 graph-backed record 的 constructor 必须显式传 `langgraph_v3`。测试证明 graph
state 拒绝 legacy engine、graph app 拒绝 legacy record，且旧 fixture 仍能读取但不会被隐式升级。

`result_ledger` 的 canonical key 是 `WorkflowResultKey(node_id, execution_generation).encode()`，key 同时在
slot 内逐字段复核，因 JSON checkpoint 不使用 tuple key。`merge_result_ledger` 对每个 `(node_id, generation)`
 只做内容摘要集合的有界 union，并按 digest 排序后保留字典序最小的两个 variant；相同 DTO replay 不增加
variant，两个或更多不同 variant 固定形成同一个 `WorkflowResultConflict`。`top2(A ∪ B)` 保证任意重放和
parenthesization 得到同一 bounded slot；reducer 本身
不得 raise、不得按到达顺序覆盖、不得删除旧 generation，必须严格满足 associative/commutative/idempotent。
`latest_results()` 是 join/router 使用的纯派生函数：先拒绝 ledger 中任何 conflict，再按
`execution_generation_by_node` 选择 current result；repair 只推进 generation map，不改写 ledger。使用至少三个
update 的所有排列和两种 parenthesization 验证代数性质。

- [ ] **Step 4: 实现每 branch 独立 runtime context factory**

```python
class BranchProfileContextFactory:
    def context_for_state(
        self, child_state: AssistantTurnState
    ) -> GraphRuntimeContext: ...


@dataclass(frozen=True)
class WorkflowGraphRuntimeContext(GraphRuntimeContext):
    assistant_graph_app: AssistantTurnGraphApp
    artifact_store: LocalWorkflowArtifactStore
    context_compiler: WorkflowContextCompiler
    branch_context_factory: BranchProfileContextFactory
```

`WorkflowProfileAssignment` 必须把 `profile_input_adapter()` 所需事实完整保存为 checkpoint-safe DTO：

- owner/identity：`user_id/session_id/agent_id/workflow_id/node_id/execution_generation`；
- invocation identity：稳定派生的 `assignment_ref`、该 child 的 `run_id/trace_id`；
- semantic input：`objective/constraints/input_artifact_refs/acceptance_contract`；
- capability/tool scope：`capability_refs`、`explicit_tool_allowlist`、`available_tool_names`、
  `tool_scope_ref`（catalog digest）；
- runtime-only source：registered `ToolSpec` 从当前 sealed Registry 重取，Provider adapter、ContextService、
  Tool operation store、artifact/memory service、cancel reader 和 stream writer 从 process-owned runtime services
  注入，绝不持久化。

branch adapter 用 assignment 生成 child state；`child_context_resolver=factory.context_for_state` 从 child 的
persisted request/run/profile/context refs/capability refs/catalog 重建全新 `AgentState`、ToolExecutor 和
`GraphRuntimeContext`，并以当前 Registry specs 验证 `tool_scope_ref`。factory 可以按完整 assignment fingerprint
cache context template，但 cache miss/清空必须走相同纯构造路径，cache value 不能拥有可变 `AgentState`。
测试并发解析两个 branch，断言 AgentState/ToolExecutor/counters/errors 不共享；关闭 app、创建全新 factory 后从
planning/worker/verifier checkpoint 分别恢复，Provider trajectory/tool scope/output 与不中断 baseline 等价；
Registry/capability/tool scope 变化、未知 assignment ref 或 owner mismatch 在任何 child node 前 fail closed。

- [ ] **Step 5: 写 state inventory 并运行 GREEN**

`state_inventory.md` 逐字段列明 checkpoint 必需性、上限、恢复消费者与禁止内容；明确业务 artifact 正文、Tool raw result、Provider raw response、lease/CAS token 和 API resume token 均不进入 graph state。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_graph_state.py
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/workflows/graph_state.py \
  src/assistant_agent/workflows/graph_context.py \
  src/assistant_agent/workflows/models.py \
  src/assistant_agent/runtime/graph_runtime.py \
  tests/tdd/native-langgraph-m3/test_workflow_graph_state.py \
  tests/tdd/native-langgraph-m3/state_inventory.md
git commit -m "feat(workflows): define durable graph state and reducers"
```

---

### Task 2: Planner AssistantTurnGraph 与确定性 v2 admission subgraph

**Files:**
- Create: `src/assistant_agent/workflows/planning_graph.py`
- Modify: `src/assistant_agent/workflows/agent_runtime.py`
- Modify: `src/assistant_agent/workflows/definitions.py`
- Modify: `src/assistant_agent/workflows/transitions.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_planning_subgraph.py`

**Interfaces:**
- Consumes: Task 1 `DurableWorkflowState` / `WorkflowGraphRuntimeContext`，M2 `AssistantTurnGraphApp.graph_for_profile("planner")`，Workflow v2 `parse_workflow_plan_response()`、`materialize_runtime_plan()`、`validate_plan_dag()`。
- Produces: `PlanningSubgraphState`（只组合 parent planning fields、严格 AssistantTurnState channels 和 bounded planner result）、`build_workflow_planning_subgraph(*, planner_graph) -> CompiledStateGraph`、`prepare_planner_profile_node(...)`、`project_planner_profile_node(...)`、`admit_planner_result_node(...)`。
- Produces: strict `PlannerProfileResult(plan_proposal, model_calls_used, tool_calls_used)`；非法 proposal 只产生 `workflow_plan_rejected` graph error，不进入 worker routing。

- [ ] **Step 1: 写真实 planning subgraph RED**

```python
@pytest.mark.asyncio
async def test_planner_child_is_native_subgraph_and_admission_is_deterministic():
    app, context, initial = planning_probe(
        proposal=valid_parallel_workflow_v2_plan()
    )
    parts = [
        part
        async for part in app.astream(
            initial,
            context=context,
            stream_mode=["updates", "tasks", "checkpoints"],
            subgraphs=True,
            version="v2",
        )
    ]
    snapshot = await app.aget_state(probe_config(initial["workflow_thread_id"]))

    assert snapshot.values["admitted_plan"]["version"] == 2
    assert native_namespaces(parts).contains_path(
        "workflow_planning", "AssistantTurnGraph.planner"
    )
    assert snapshot.values["phase"] == "admitted"
```

另写非法 cycle、未知 dependency、漏 deliverable、非 terminal producer、required constraint 无 verifier、verifier 不在所有 owner 下游、planner 试图创建 `plan` node 的用例；断言这些 proposal 不产生任何 worker task。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_planning_subgraph.py
```

Expected: FAIL，planning graph builder 和 profile wrapper 尚不存在。

- [ ] **Step 3: 实现窄 planner wrapper 与 planning subgraph**

```python
def build_workflow_planning_subgraph(*, planner_graph: Any) -> Any:
    graph = StateGraph(
        PlanningSubgraphState,
        context_schema=WorkflowGraphRuntimeContext,
    )
    graph.add_node("prepare_planner", prepare_planner_profile_node)
    graph.add_node("planner_profile", planner_graph)
    graph.add_node("project_planner", project_planner_profile_node)
    graph.add_node("admit_plan", admit_planner_result_node)
    graph.add_edge(START, "prepare_planner")
    graph.add_edge("prepare_planner", "planner_profile")
    graph.add_edge("planner_profile", "project_planner")
    graph.add_edge("project_planner", "admit_plan")
    graph.add_edge("admit_plan", END)
    return graph.compile(checkpointer=None, name="WorkflowPlanningSubgraph")
```

`prepare_planner_profile_node` 只把 `workflow_id/objective/deliverables/constraints/typed inputs/budget` 转为
`ProfileInvocationInput(profile="planner", explicit_tool_allowlist=())`，生成 strict child channels 并注册
branch-local context。`planner_graph` 必须作为 builder 的真实 compiled subgraph node，不能在 Python wrapper
里手工 `planner_graph.ainvoke()`；因此 parent saver 能继承 namespace，`subgraphs=True` 能看到真实 child
checkpoint/interrupt。`project_planner_profile_node` 只调用 `profile_output_adapter()`，输出 bounded response、usage
和 parse 后 proposal；父 `DurableWorkflowState`、business DB record 和 Store connection 均不传入 child。

`admit_planner_result_node` 只调用 v2 parser、definition materializer 和 `validate_plan_dag()`；成功写入 admitted plan、每 node generation=0、phase=`admitted`，失败写入结构化 error/status=`failed`。Planner 输出不可信，不允许 prompt 或节点名称决定 admission。

- [ ] **Step 4: 验证 profile 工具空间和 checkpoint namespace**

测试 planner child 的 Provider catalog 为空、Executor allowed set 为空；动态 subgraph task UUID 只出现在 native stream namespace，不进入 `DurableWorkflowState`、Workflow plan、business event 或 API payload。重新编译 planning subgraph 后 graph name 和 node name 稳定。

- [ ] **Step 5: 运行 GREEN 与 Workflow v2 兼容集合**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_planning_subgraph.py \
  tests/tdd/workflow-plan-v2
```

Expected: PASS；若 `tests/tdd/workflow-plan-v2` 在执行 worktree 中已按用户要求清理，则只运行存在的 M3 planning test，并在报告中记录该历史 feature 目录不存在，不能伪造结果。

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/workflows/planning_graph.py \
  src/assistant_agent/workflows/agent_runtime.py \
  src/assistant_agent/workflows/definitions.py \
  src/assistant_agent/workflows/transitions.py \
  tests/tdd/native-langgraph-m3/test_workflow_planning_subgraph.py
git commit -m "feat(workflows): compose planner admission subgraph"
```

---

### Task 3: Conditional Send 任意 DAG waves、Pregel join 与 worker subgraph

**Files:**
- Create: `src/assistant_agent/workflows/durable_graph_nodes.py`
- Create: `src/assistant_agent/workflows/durable_graph.py`
- Modify: `src/assistant_agent/workflows/graph_state.py`
- Modify: `src/assistant_agent/workflows/graph_context.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_send_join.py`

**Interfaces:**
- Consumes: Task 2 `WorkflowPlanningSubgraph`、Task 1 reducer/context，M2 `AssistantTurnGraph.worker` child。
- Produces: `build_durable_workflow_graph(*, planning_subgraph, checkpointer, store=None) -> CompiledStateGraph`。
- Produces: `WorkflowProfileBranchState` / `WorkflowBranchOutput`、`build_worker_branch_subgraph(worker_graph) -> CompiledStateGraph`、`prepare_next_wave_node(state) -> dict`、`route_next_wave(state) -> list[Send] | Literal["publish", "fail"]`、`project_worker_result_node(...) -> dict`、`join_wave_node(state) -> dict`。
- Graph topology: `START -> workflow_planning -> prepare_wave -(Conditional Send)-> run_worker -> join_wave -> prepare_wave|publish|fail -> END`；Task 4 在同一 builder 中加入 verifier/repair routing。

- [ ] **Step 1: 写并行 super-step 与任意 DAG RED**

```python
@pytest.mark.asyncio
async def test_send_runs_all_ready_nodes_in_one_superstep_and_join_waits_for_all():
    barrier = ParallelChildBarrier(expected={"a", "b"})
    graph, context, initial = workflow_probe(
        plan=dag({"a": [], "b": [], "c": ["a", "b"]}),
        child_barrier=barrier,
    )
    task = asyncio.create_task(graph.ainvoke(initial, context=context))
    await barrier.wait_until_all_started()
    assert not task.done()
    barrier.release_all()
    final = await task

    assert barrier.max_concurrency == 2
    assert wave_history(final) == [("a", "b"), ("c",)]
    assert latest_results(
        final["result_ledger"], final["execution_generation_by_node"]
    )["c"].status == "succeeded"
```

增加一个 `a,b -> c` 且 `b -> d -> e` 的非对称 DAG，断言波次为 `(a,b) -> (c,d) -> (e)`；输入 node 顺序随机化 20 次，结果和 wave partition 不变。增加 replay 同一 ledger update、同 `(node_id,generation)` 两个 variant 形成稳定 conflict fact、worker 完成前 join 不运行、无 ready 且未完成时 `workflow_dag_stalled` fail-closed。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_send_join.py
```

Expected: FAIL，`DurableWorkflowGraph` builder 和 `Send` router 尚不存在。

- [ ] **Step 3: 实现 deterministic wave 与 `Send` router**

```python
def route_next_wave(
    state: DurableWorkflowState,
) -> list[Send] | Literal["publish", "fail"]:
    if state["status"] == "failed":
        return "fail"
    branches = tuple(state["active_wave"])
    if branches:
        return [Send("run_worker", branch.profile_subgraph_input) for branch in branches]
    if all_current_generation_nodes_succeeded(state):
        return "publish"
    return "fail"
```

`prepare_next_wave_node` 只从 admitted static DAG、current-generation result 和显式 dependency 计算全部 ready node，按 `node_id` 排序后一次写入 `active_wave`；不 claim、不 lease、不轮询、不写 DB。`route_next_wave` 只返回 `Send` 或终态 route；**不得再添加 `prepare_wave -> run_worker` 静态 edge**，否则同一 node 会被重复调度。

`build_worker_branch_subgraph()` 的 `StateGraph` 使用 `input_schema=WorkflowProfileBranchState`、
`output_schema=WorkflowBranchOutput`，拓扑固定为
`START -> AssistantTurnGraph.worker -> project_worker_result -> END`。worker compiled graph 是真实 subgraph
node，不由 Python wrapper 手工 invoke。每个 `Send` PUSH task 收到已经由 Task 1 adapter 从 checkpoint-safe
assignment 构造的独立窄 child state，不收到整个父 state；outer branch checkpoint 保留完整
assignment/node/generation，inner assistant child
checkpoint 只含 AssistantTurnState channels。`project_worker_result` 经 `profile_output_adapter()` 和 artifact store
adapter 形成一个 keyed ledger update：

```python
return {"result_ledger": ledger_update(node_result)}
```

parent budget 只在 `join_wave_node` 先调用 `latest_results()`、确认本 wave 无 conflict 且每个
current-generation result 唯一后汇总扣减；parallel child 不并发修改一个 `WorkflowBudget`。

- [ ] **Step 4: 编译真实父图并验证 Pregel 事实**

```python
builder = StateGraph(
    DurableWorkflowState,
    context_schema=WorkflowGraphRuntimeContext,
)
builder.add_node("workflow_planning", planning_subgraph)
builder.add_node("prepare_wave", prepare_next_wave_node)
builder.add_node("run_worker", worker_branch_subgraph)
builder.add_node("join_wave", join_wave_node)
builder.add_node("publish", publish_node)
builder.add_node("fail", fail_node)
builder.add_edge(START, "workflow_planning")
builder.add_edge("workflow_planning", "prepare_wave")
builder.add_conditional_edges("prepare_wave", route_next_wave)
builder.add_edge("run_worker", "join_wave")
builder.add_conditional_edges(
    "join_wave",
    route_after_join,
    {"next_wave": "prepare_wave", "publish": "publish", "fail": "fail"},
)
builder.add_edge("publish", END)
builder.add_edge("fail", END)
```

使用真实 `astream(stream_mode=["updates", "tasks", "checkpoints"], subgraphs=True, version="v2")` 断言两个 root worker task 位于同一 super-step、join update 只在两者 task result 后出现、worker child namespace 可见；不能通过自定义 barrier 的调用计数单独证明 Graph 接入。

- [ ] **Step 5: 运行 GREEN 和 no-scheduler gate**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_send_join.py \
  tests/tdd/native-langgraph-m3/test_workflow_graph_state.py

rg -n "ThreadPoolExecutor|claim_ready_work_item|renew_work_item_lease|run_claim" \
  src/assistant_agent/workflows/durable_graph.py \
  src/assistant_agent/workflows/durable_graph_nodes.py \
  src/assistant_agent/workflows/graph_context.py
```

Expected: pytest PASS；`rg` 无输出。

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/workflows/durable_graph_nodes.py \
  src/assistant_agent/workflows/durable_graph.py \
  src/assistant_agent/workflows/graph_state.py \
  src/assistant_agent/workflows/graph_context.py \
  tests/tdd/native-langgraph-m3/test_workflow_send_join.py
git commit -m "feat(workflows): execute dag waves with native send and join"
```

---

### Task 4: Verifier Command、最小 repair、原生 retry/timeout/error fallback

**Files:**
- Modify: `src/assistant_agent/workflows/durable_graph.py`
- Modify: `src/assistant_agent/workflows/durable_graph_nodes.py`
- Modify: `src/assistant_agent/workflows/graph_state.py`
- Modify: `src/assistant_agent/workflows/graph_context.py`
- Modify: `src/assistant_agent/workflows/constraints.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_verify_repair.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_node_policies.py`

**Interfaces:**
- Consumes: admitted plan 的 explicit constraint verifier、Task 3 wave router、M2 `AssistantTurnGraph.verifier`。
- Produces: `run_verifier_profile_node(...)`、`decide_verification_node(...) -> Command[Literal["prepare_wave", "publish", "await_input", "fail"]]`、`minimal_repair_closure(plan, requested_ids, verifier_id) -> frozenset[str]`。
- Produces: `WORKFLOW_TRANSIENT_RETRY_POLICY = RetryPolicy(...)`、`WORKFLOW_NODE_TIMEOUT = TimeoutPolicy(...)`、`workflow_node_error_handler(state, error: NodeError) -> Command`。

- [ ] **Step 1: 写 verifier/repair RED**

构造 `a,b -> synthesize -> verify`，第一次 verifier 返回 repair `("a",)`，第二次 verified。断言 generation 变化为：`a/synthesize/verify == 1`、`b == 0`；`b` 的 child invocation 和 artifact ref 不重复；repair 后 verifier 必须重新执行；最终只发布 current-generation deliverable。

```python
assert final["execution_generation_by_node"] == {
    "a": 1,
    "b": 0,
    "synthesize": 1,
    "verify": 1,
}
assert child_runs("b") == 1
assert latest_results(
    final["result_ledger"], final["execution_generation_by_node"]
)["verify"].status == "succeeded"
```

增加非法 repair：非祖先、未知 node、空 scope、超过 repair round/budget；断言进入 `invalid_repair_scope` 或 `repair_budget_exhausted`，不能全 DAG 重跑或自然语言兜底成功。

- [ ] **Step 2: 写 policy RED**

使用 worker probe 依次抛两个 `OSError` 后成功，断言 native task attempt 为 3 且只有一个 committed result；对 `WorkflowPlanRejected` 断言不 retry；使用超过 node timeout 的 async probe，断言 retry 耗尽后 `error_handler` 写入 `workflow_node_timeout` 并 `Command(goto="fail")`。检查 compiled graph node spec 的 `retry_policy/timeout/error_handler_node`，而不是测试项目自写循环。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_verify_repair.py \
  tests/tdd/native-langgraph-m3/test_workflow_node_policies.py
```

Expected: FAIL，verifier route、generation repair 和原生 policies 尚未注册。

- [ ] **Step 4: 实现 verifier profile 与 `Command` 路由**

`prepare_next_wave_node` 根据 constraint binding 的 `verifier_work_item_id` 把 ready branch 的 profile 设置为
`verifier`，router 分发到 `run_verifier`。`run_verifier` 与 worker 一样由
`build_verifier_branch_subgraph(verifier_graph)` 把 M2 compiled verifier graph 直接注册为 child node，再由 bounded
projector 输出 result；不得在 wrapper 手工 invoke。verifier child 只能看到 owner artifact refs、assigned
constraints、acceptance contract 和 read-only catalog。

```python
def decide_verification_node(
    state: DurableWorkflowState,
) -> Command[Literal["prepare_wave", "publish", "await_input", "fail"]]:
    decision = current_verifier_decision(state)
    if decision.status == "repair":
        return Command(
            update=repair_generation_update(state, decision.repair_node_ids),
            goto="prepare_wave",
        )
    if decision.status == "waiting_input":
        return Command(update=pending_input_update(decision), goto="await_input")
    if decision.status == "succeeded" and all_deliverables_verified(state):
        return Command(goto="publish")
    return Command(update=failure_update(decision), goto="fail")
```

`decide_verification` 通过 `destinations=("prepare_wave", "publish", "await_input", "fail")` 注册；它的控制流只由返回的 `Command` 决定，**不得为该 node 再添加静态 edge**。

`minimal_repair_closure` 先验证 requested ID 均为 verifier 的祖先，再取“requested node + 所有依赖它们且已产出 current-generation result 的后代 + verifier/deliverable downstream”闭包；只为该闭包 generation +1。无关 branch 结果仍 current，不重跑。

- [ ] **Step 5: 注册原生 retry、timeout 与 error handler**

```python
WORKFLOW_TRANSIENT_RETRY_POLICY = RetryPolicy(
    initial_interval=0.1,
    backoff_factor=2.0,
    max_interval=1.0,
    max_attempts=3,
    jitter=False,
    retry_on=is_transient_workflow_node_error,
)
WORKFLOW_NODE_TIMEOUT = TimeoutPolicy(
    run_timeout=30.0,
    idle_timeout=10.0,
    refresh_on="auto",
)

builder.add_node(
    "run_worker",
    worker_branch_subgraph,
    retry_policy=WORKFLOW_TRANSIENT_RETRY_POLICY,
    timeout=WORKFLOW_NODE_TIMEOUT,
    error_handler=workflow_node_error_handler,
)
```

`is_transient_workflow_node_error` 只允许 `OSError`、明确 Provider transient error 和 `NodeTimeoutError`；Validator/admission/permission/state conflict 不 retry。handler 接收 LangGraph `NodeError`，返回结构化 failure result 和 `Command(goto="join_wave"|"fail")`，不得调用旧 `WorkflowRuntime` fallback。

- [ ] **Step 6: 运行 GREEN 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_verify_repair.py \
  tests/tdd/native-langgraph-m3/test_workflow_node_policies.py \
  tests/tdd/native-langgraph-m3/test_workflow_send_join.py

git add src/assistant_agent/workflows/durable_graph.py \
  src/assistant_agent/workflows/durable_graph_nodes.py \
  src/assistant_agent/workflows/graph_state.py \
  src/assistant_agent/workflows/graph_context.py \
  src/assistant_agent/workflows/constraints.py \
  tests/tdd/native-langgraph-m3/test_workflow_verify_repair.py \
  tests/tdd/native-langgraph-m3/test_workflow_node_policies.py
git commit -m "feat(workflows): verify and repair durable graph natively"
```

---

### Task 5: Native interrupt/resume、官方 persistent checkpoint 与跨进程恢复

**Files:**
- Create: `src/assistant_agent/workflows/durable_graph_app.py`
- Modify: `src/assistant_agent/workflows/durable_graph.py`
- Modify: `src/assistant_agent/workflows/durable_graph_nodes.py`
- Modify: `src/assistant_agent/workflows/graph_state.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_profiles.py`
- Modify: `src/assistant_agent/runtime/assistant_interrupts.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `src/assistant_agent/runtime/checkpointer.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_interrupt_resume.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_persistent_recovery.py`

**Interfaces:**
- Depends on: M2 Task 2 `open_async_checkpointer(...)`、official `AsyncSqliteSaver` handle 和进程级 `AssistantRuntimeApp.astart()/aclose()` owner。该依赖门未完成时，本任务只能运行 `InMemorySaver` 的同进程 interrupt RED/GREEN，persistent/cross-process acceptance 必须保持 pending。
- Produces: `WorkflowGraphExecutionIdentity.for_workflow(...)`、`WorkflowInterrupt(action_ref, ...)`、`WorkflowResume(values_by_action_ref)`、`DurableWorkflowGraphApp.arun(...)`、`.aresume(...)`、`.aget_state(...)`、`.aget_state_history(...)`。
- Resume contract: 相同 `thread_id`、新 `run_id`；app 从 fresh native snapshot 将业务 `action_ref` 映射为当前
  `Interrupt.id`，再调用 `Command(resume={interrupt_id: value, ...})`。native interrupt ID 不进入外部协议或业务 DB。

- [ ] **Step 1: 写单个和多个 native interrupt RED**

使用一个 worker blocked 和两个并行 worker 同时 blocked 的真实 `Send` 图。断言 interrupt 只由
`AssistantTurnGraph.worker/verifier` 内部 `await_input` node 产生，snapshot 的 native subgraph `tasks` 中分别有
1/2 个 interrupt；outer worker/verifier wrapper 不调用 `interrupt()`。状态为 `waiting_input` 且无 terminal
artifact；以业务 `action_ref` 提交完整/部分 multi-resume，app 从当前 snapshot 映射 native IDs，只继续已提供的
pending child，已成功 sibling 不重跑。

```python
result = await app.aresume(
    identity=resume_identity,
    context=context,
    resume=WorkflowResume(
        values_by_action_ref={
            "workflow:wf-1:node:a:generation:0": {"answer": "A"},
            "workflow:wf-1:node:b:generation:0": {"answer": "B"},
        },
    ),
)
assert result.status == "completed"
assert child_runs("already_done") == 1
assert result.final_state["invocation_run_id"] == resume_identity.run_id
```

- [ ] **Step 2: 让 AssistantTurn child 成为唯一 interrupt owner**

worker/verifier profile 的 validated structured control 产生 `blocked` 时，由 child graph 内部 profile result adapter
设置 strict `pending_interrupt`，其 action ref 固定派生为
`workflow:{workflow_id}:node:{node_id}:generation:{generation}`；child 自己的 conditional edge 随后进入既有
`await_input` node，且**只有这个 child node 调用 `interrupt()`**。payload 只含 action ref、
`workflow_id/node_id/generation/required_fields/prompt_code`。outer branch wrapper 既不调用 interrupt，也不把
`waiting_user` 转成第二个 parent interrupt，只允许 native pending subgraph task 向上冒泡。

`DurableWorkflowGraphApp` 递归读取 root/task/subgraph snapshot，收集 native `Interrupt.id` 和 payload，以
`action_ref` 为业务 key 建立本次内存映射；同一 action ref 对应不同 payload/ID、未知 node、generation 不匹配、
owner 不匹配全部 fail closed。`WorkflowResume.values_by_action_ref` 可以只恢复部分并行 child；app 只对当前
snapshot 中匹配的 action ref 构造 `{native_interrupt_id: value}`，未提供 child 保持 pending。native
interrupt/checkpoint ID 不保存到 Workflow v2 model、业务 projection 或公共 API。

如保留媒体消费者所需的 opaque `resume_token`，它只作为 owner-bound action token 指向一个 action ref，不能
缓存 native interrupt ID；每次恢复仍必须重读 snapshot 映射。一次请求可以提交一个 token/value，内部
`WorkflowResume` 和 graph API 必须支持多 action map，以便 eval/operator 一次恢复多个 pending child。

- [ ] **Step 3: 主 async stream 使用同步 durability**

```python
async for raw in self.graph.astream(
    input_or_command,
    config=identity.runnable_config(),
    context=context,
    stream_mode=["values", "updates", "custom", "tasks", "checkpoints"],
    subgraphs=True,
    durability="sync",
    version="v2",
):
    yield WorkflowGraphStreamPart.from_v2(raw)
```

run outcome 必须在 stream 结束后调用 `aget_state(..., subgraphs=True)` 判断：pending task + interrupt=`interrupted`；pending task 无 interrupt=`infrastructure_error`；无 pending task且 state terminal=`completed|failed|cancelled`。不得从最后一个自定义 event 猜终态。

- [ ] **Step 4: 在依赖授权门通过后写并运行 SQLite 跨进程恢复 RED/GREEN**

测试流程必须真实关闭 app/saver/SQLite connection并丢弃 branch context cache，再新建
`AssistantRuntimeApp`、official saver、全新 `BranchProfileContextFactory` 和 `DurableWorkflowGraphApp`，使用同一
DB path/thread resume。分别在 planning 后、第一 wave 后、worker/verifier child interrupt 和 multi-interrupt
时重建；新的 pure factory 只从 checkpoint assignment/owner/capability/tool-scope facts + runtime services
重建 child context，恢复结果与不中断 baseline 的 current-generation ledger、artifact refs、budget 和 terminal
state 等价，已完成 child 与 write operation 不重复。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_interrupt_resume.py \
  tests/tdd/native-langgraph-m3/test_workflow_persistent_recovery.py
```

Expected: 授权门通过并完成 M2 Task 2 后 PASS。若门未通过，只可报告 `test_workflow_interrupt_resume.py` 的 InMemory 结果；不得 skip persistent test 后声称完成。

- [ ] **Step 5: 验证 checkpoint version 和身份 fail-closed**

覆盖不同 user/agent、不同 workflow thread、复用旧 run ID、已消费 resume token、旧 graph/state schema、未知
interrupt/action ref 和 generation mismatch；这些情况在任何 child/tool/publish 前失败，且 business record 不
伪造 completed。部分 multi-resume 是合法非终态：只恢复命中的 child，其余 interrupt 继续存在；重复提供已完成
action ref 才 fail closed。

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/workflows/durable_graph_app.py \
  src/assistant_agent/workflows/durable_graph.py \
  src/assistant_agent/workflows/durable_graph_nodes.py \
  src/assistant_agent/workflows/graph_state.py \
  src/assistant_agent/runtime/assistant_loop_graph.py \
  src/assistant_agent/runtime/assistant_loop_nodes.py \
  src/assistant_agent/runtime/assistant_graph_profiles.py \
  src/assistant_agent/runtime/assistant_interrupts.py \
  src/assistant_agent/runtime/assistant_runtime_app.py \
  src/assistant_agent/runtime/checkpointer.py \
  tests/tdd/native-langgraph-m3/test_workflow_interrupt_resume.py \
  tests/tdd/native-langgraph-m3/test_workflow_persistent_recovery.py
git commit -m "feat(workflows): persist and resume durable graph threads"
```

---

### Task 6: Async composition root、产品投影、API 兼容与 Deep Research cutover

**Files:**
- Create: `src/assistant_agent/workflows/graph_projection.py`
- Create: `src/assistant_agent/workflows/graph_publish.py`
- Create: `src/assistant_agent/workflows/graph_host.py`
- Modify: `src/assistant_agent/workflows/graph_state.py`
- Modify: `src/assistant_agent/workflows/durable_graph.py`
- Modify: `src/assistant_agent/workflows/durable_graph_nodes.py`
- Modify: `src/assistant_agent/workflows/service.py`
- Modify: `src/assistant_agent/workflows/progress.py`
- Modify: `src/assistant_agent/workflows/store.py`
- Modify: `src/assistant_agent/workflows/sqlite_store.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/api/routes_workflows.py`
- Modify: `src/assistant_agent/api/models.py`
- Modify: `src/assistant_agent/gateway/runtime_pool.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_graph_host.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_api_cutover.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_product_projection.py`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_publish_barrier.py`
- Create: `tests/tdd/native-langgraph-m3/workflow_consumer_inventory.md`
- Create: `tests/tdd/native-langgraph-m3/test_workflow_consumer_contract.py`

**Interfaces:**
- Consumes: Task 5 `DurableWorkflowGraphApp` 和进程级 async saver owner，existing owner/idempotency/artifact/audit business services；不消费或返回旧 execution `WorkflowBundle`。
- Produces: `WorkflowGraphHost.astart()/submit()/resume()/cancel()/recover_nonterminal()/aclose()`；其公共结果是严格 `WorkflowProductSnapshot` / `WorkflowHandle`，它只拥有 per-workflow asyncio task 和订阅，不计算 ready node。
- Produces: `WorkflowGraphProjector.project_stream_part(...)` / `.project_snapshot(...)`；单向写 `WorkflowProductSnapshot/WorkflowProductEvent`，不能驱动 graph。
- Produces: `WorkflowPublishOperation`、`PublishCommitRef`、`WorkflowPublishLedger.prepare()/commit()/get()` 与 `SQLiteWorkflowPublishLedger`；`commit()` 在一个 business SQLite transaction 内提交 operation outcome、terminal product snapshot、唯一 completed event 和可选 delivery outbox。
- Compatibility: 只保护 consumer inventory 证明被 Agent-Service/media 使用的 handle/status/progress/cursor event/result content/waiting-input action；未消费的 `plan/work_items/lease/revision`、旧 `WorkflowBundle` shape 和 `/cancel` route 不形成 M3 兼容约束。

- [ ] **Step 1: 先完成真实消费者 inventory 与契约 RED**

用 `rg` 和 import/call-site inspection 记录所有非历史、非 TDD 的消费者。当前事实基线必须至少包含：

```text
Agent-Service -> run.end.output_refs -> workflow://<id>
scripts/media_simulator.py -> GET status/progress
scripts/media_simulator.py -> GET cursor events
scripts/media_simulator.py -> GET final result.content
scripts/media_simulator.py -> POST waiting-input token/value
```

`workflow_consumer_inventory.md` 对每个字段标明 consumer/file/line、是否 hard-protected、替代投影；没有 call
site 的 `/cancel`、response `plan`、work item lease/attempt/revision 和 raw `WorkflowBundle` 标记
`unconsumed-breaking-cleanup-allowed`。`test_workflow_consumer_contract.py` 用 strict fixture 只断言受保护字段，且
断言投影不含 plan/checkpoint/task/lease/CAS。若执行时发现其他真实 Agent-Service/media consumer，先加入
inventory 和 narrow contract，再改实现；不得猜测兼容范围。

- [ ] **Step 2: 写 Deep Research cutover RED**

从真实 `UserRequest(assistant_mode="deep_research")` 经过 runtime/service 提交，断言：返回受保护的
`workflow://` handle；后台只有一个 `DurableWorkflowGraph` task；旧 `DurableWorkflowWorker.run_once`、
`WorkflowStore.claim_ready_work_item`、`renew_work_item_lease`、`WorkflowRuntime.run_claim` 均未调用；最终
product snapshot/result 满足 Step 1 consumer contract，不要求旧 Bundle 等价。

另以 `workflow_type="long_horizon"` 构造内部兼容记录，证明 M3 没有把旧 scheduler 全局删掉；它仍只能由显式 legacy composition root 处理，且产品入口不会自动选择该类型。

- [ ] **Step 3: 写投影、API 和 publish barrier RED**

使用真实 graph `custom/updates/tasks/checkpoints` stream，断言 projector 只发布/持久化：accepted/planning/worker
progress/waiting input/completed/cancelled/failed 和 artifact refs；不投影完整 state、task ID、checkpoint config、
namespace、Tool raw body、Provider response。只对 inventory 标为 hard-protected 的 status/events/result/input 路径
验证字段和状态码；未消费 route/field 可删除或改成窄 snapshot。

publish barrier 单独覆盖：

1. `WorkflowPublishOperation` key 固定为
   `workflow:{workflow_id}:publish:{plan_version}:{current_generation_digest}`，DTO 只含 owner、deliverable artifact refs、
   result digest、`prepared|committed` 和安全错误；
2. crash before `commit()`：无 terminal snapshot/event/outbox；replay 使用同 key 可重新提交；
3. SQLite transaction 已 commit、graph node 返回前 crash：重建 app 后 replay 从 ledger 读取同一
   `PublishCommitRef`，不重复 completed event/outbox/artifact publish；
4. 同 key 不同 digest/refs、prepared outcome 不确定或 owner mismatch fail closed；
5. native `checkpoints` 与 business ledger 证明顺序恒为 publish committed → graph state completed → terminal
   projector/delivery；任何 checkpoint 不得先出现 completed。

- [ ] **Step 4: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_graph_host.py \
  tests/tdd/native-langgraph-m3/test_workflow_api_cutover.py \
  tests/tdd/native-langgraph-m3/test_workflow_product_projection.py \
  tests/tdd/native-langgraph-m3/test_workflow_publish_barrier.py \
  tests/tdd/native-langgraph-m3/test_workflow_consumer_contract.py
```

Expected: FAIL，当前 API/worker 仍由 claim/lease/CAS scheduler 推进 Deep Research。

- [ ] **Step 5: 实现薄 graph host 与 lifecycle**

```python
class WorkflowGraphHost:
    async def submit(
        self, *, identity: RequestIdentity, ingress_run_id: str,
        submission: WorkflowSubmission
    ) -> WorkflowHandle: ...

    async def resume(
        self, *, identity: RequestIdentity, workflow_id: str,
        resume_token: str, values: dict[str, JsonValue]
    ) -> WorkflowProductSnapshot: ...

    async def recover_nonterminal(self) -> None: ...
    async def aclose(self) -> None: ...
```

`submit()` 先经 narrow business repository 完成 owner/idempotency admission，再以稳定 workflow thread 启动
`graph_app.arun()`；它不创建旧 bootstrap plan/Bundle。`recover_nonterminal()` 只枚举 `langgraph_v3`
Deep Research 非终态 handle/snapshot，逐条调用 `graph_app.aget_state()/arun(None)`，由 checkpoint 决定位置。
它不读取 business plan 计算 ready node，不 claim/lease，不重建 dependency wave。

同一 workflow task 由 async lock/idempotent task map 去重；disconnect 只取消订阅；application shutdown 等待/取消本进程订阅但不把 Workflow 标记 cancelled。cancel 通过 graph `Command(update=..., goto="cancel")` 或 native state update/continue 落到 graph terminal node，不能只改 DB summary。

- [ ] **Step 6: 实现 publish operation barrier、单向投影和窄 API adapter**

`publish` node 先从 conflict-free `latest_results()` 解析 deliverable refs/digest，`prepare()` stable operation，再调用
`commit()`；SQLite `commit()` 必须原子写 committed operation、completed product snapshot、唯一 completed event 和
可选 idempotent delivery outbox。只有返回 `PublishCommitRef(status="committed")` 后 node 才更新 graph
`status="completed"/result_artifact_refs`。若 commit 已存在则校验完整 DTO 后短路；prepared ambiguous 或不同
payload fail closed。artifact/content-addressable write 与 delivery 使用同 operation key，不能在 barrier 前执行
不可重复副作用。

projector 以 `(workflow_id, graph run, node_id, generation, fact kind)` 稳定幂等键写业务 event/audit；business
SQLite 保存 narrow `WorkflowProductSnapshot`，不为 graph record制造 ready/running lease/CAS execution view。
`project_workflow_progress()` 只读投影的 `completed_items/active_items/wave/phase`，不调用
`next_ready_work_item()` 推进状态；terminal projector 发现 committed publish 已在同 transaction 落库时只发布
stream/delivery view，不重复 DB event。

`routes_workflows.py` 只保留/重写 inventory 证明被消费的 GET status/progress、events、result 与 POST input；
`/cancel` 若 inventory 仍无真实 consumer 可删除，不为历史测试保留。routes 返回 strict product models，不返回
旧 `WorkflowBundle`/plan。`AgentGraphRuntime._start_deep_research_workflow` 收窄为调用注入的 graph host，不创建旧
bootstrap planner work item。FastAPI lifespan 先启动 shared `AssistantRuntimeApp` saver，再启动 workflow graph
host；shutdown 反序关闭 host、runtime app、saver。

- [ ] **Step 7: 从旧 worker 排除 Deep Research**

为 legacy worker/store claim 增加显式 `included_workflow_types`，production legacy worker 固定不包含 `deep_research`；没有 allowlist 时 fail closed，不以文本、kind 或节点名猜测。测试使用 spy 证明 Deep Research 从提交到完成对 `claim_ready_work_item/renew_work_item_lease/save(expected_revision=...)` 的执行控制调用数均为 0；business projector 的幂等 save 不计为 scheduler CAS，必须用独立明确方法名 `save_projection(...)`。

- [ ] **Step 8: 运行 GREEN、Gateway/媒体兼容与提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_workflow_graph_host.py \
  tests/tdd/native-langgraph-m3/test_workflow_api_cutover.py \
  tests/tdd/native-langgraph-m3/test_workflow_product_projection.py \
  tests/tdd/native-langgraph-m3/test_workflow_publish_barrier.py \
  tests/tdd/native-langgraph-m3/test_workflow_consumer_contract.py \
  tests/tdd/deep-research-mode \
  tests/core/contract/test_gateway_contract.py

git add src/assistant_agent/workflows/graph_projection.py \
  src/assistant_agent/workflows/graph_publish.py \
  src/assistant_agent/workflows/graph_host.py \
  src/assistant_agent/workflows/graph_state.py \
  src/assistant_agent/workflows/durable_graph.py \
  src/assistant_agent/workflows/durable_graph_nodes.py \
  src/assistant_agent/workflows/service.py \
  src/assistant_agent/workflows/progress.py \
  src/assistant_agent/workflows/store.py \
  src/assistant_agent/workflows/sqlite_store.py \
  src/assistant_agent/runtime/runtime.py \
  src/assistant_agent/runtime/assistant_runtime_app.py \
  src/assistant_agent/api/app.py \
  src/assistant_agent/api/routes_workflows.py \
  src/assistant_agent/api/models.py \
  src/assistant_agent/gateway/runtime_pool.py \
  tests/tdd/native-langgraph-m3/test_workflow_graph_host.py \
  tests/tdd/native-langgraph-m3/test_workflow_api_cutover.py \
  tests/tdd/native-langgraph-m3/test_workflow_product_projection.py \
  tests/tdd/native-langgraph-m3/test_workflow_publish_barrier.py \
  tests/tdd/native-langgraph-m3/test_workflow_consumer_contract.py \
  tests/tdd/native-langgraph-m3/workflow_consumer_inventory.md
git commit -m "refactor(workflows): cut deep research over to durable graph"
```

Expected: PASS；Agent-Service/媒体 wire 没有新字段，Deep Research 不触发 legacy worker。

---

### Task 7: LangSmith Workflow Dataset、Experiment 与原生 graph 完整性

**Files:**
- Create: `evals/langsmith_workflow_regression/__init__.py`
- Create: `evals/langsmith_workflow_regression/contracts.py`
- Create: `evals/langsmith_workflow_regression/evaluators.py`
- Create: `evals/langsmith_workflow_regression/experiment.py`
- Create: `evals/langsmith_workflow_regression/cli.py`
- Create: `scripts/run_langsmith_workflow_regressions.py`
- Modify: `scripts/README.md`
- Modify: `evals/README.md`
- Create: `tests/tdd/native-langgraph-m3/test_langsmith_workflow_experiment.py`
- Create: `tests/tdd/native-langgraph-m3/test_langsmith_workflow_evaluators.py`

**Interfaces:**
- Dataset: fixed `assistant-agent-durable-workflow-regressions`，Example input 是 typed Workflow submission，reference output 是 expected terminal/trajectory contract，metadata 用 `active/risk/source_trace_id`。
- Target: `run_workflow_example(example) -> dict` 只调用 Task 6 `WorkflowGraphHost`/Task 5 `DurableWorkflowGraphApp`，不装配 `WorkflowRuntime`、旧 worker 或 OTel parent bridge。
- Feedback keys: `assistant_agent.workflow.plan_admission`、`assistant_agent.workflow.dag_trajectory`、`assistant_agent.workflow.constraint_artifact_quality`、`assistant_agent.workflow.repair_resume`。
- Completeness: 每个 active Example 恰有一个 root run，父子树含 `DurableWorkflowGraph -> WorkflowPlanningSubgraph -> AssistantTurnGraph.planner`，以及实际 worker/verifier child；repair/resume case 还必须出现对应 node generation/run。

- [ ] **Step 1: 写 offline contract/evaluator RED**

定义四类最小 Example：parallel join、constraint verifier、minimal repair、interrupt/resume equivalence。测试严格拒绝 stringified input、缺 deliverable/constraints、truncated input、未知 metadata、无 active Example。local evaluator 从 actual structured evidence 检查：所有 admitted node 恰执行 current generation、每条 dependency 在 child 前完成、每个 deliverable 有 artifact、repair 没重跑无关 branch、resume 与 uninterrupted terminal 等价。

- [ ] **Step 2: 写 fake LangSmith 完整性 RED**

仿照现有 `evals/langsmith_runtime_regression` 的 fake client，但断言真实 graph 层级和 `reference_example_id/trace_id/parent_run_id`；缺 planner child、孤立 worker trace、缺 verifier、手工 canonical OTel graph、Feedback 缺一项或重复 root 均返回 infrastructure failure。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_langsmith_workflow_experiment.py \
  tests/tdd/native-langgraph-m3/test_langsmith_workflow_evaluators.py
```

Expected: FAIL，workflow LangSmith experiment package 尚不存在。

- [ ] **Step 4: 实现 direct compiled-graph target 和 evaluator**

runner 使用 `Client.aevaluate()`，在当前 LangSmith `RunTree` 中 async 创建/checkout shared Runtime App，直接 await `DurableWorkflowGraph`，最终 actual output 固定包含：

```python
{
    "workflow_id": "...",
    "terminal_status": "completed|failed|cancelled|interrupted",
    "plan": {"node_ids": [...], "dependencies": {...}},
    "trajectory": [{"node_id": "...", "generation": 0, "profile": "worker"}],
    "result_artifact_refs": [...],
    "evaluation_evidence": {
        "constraint_ids": [...],
        "repair_scope": [...],
        "resume_equivalent": True,
    },
}
```

actual output 必须脱敏、有界；artifact 正文、Provider raw response、Tool body、checkpoint/config 不进入 LangSmith Example output 或 metadata。单节点 planner/verifier evaluator 读取同一 trace 子树，不再次调用复制的 planner/verifier runtime。

- [ ] **Step 5: 实现 CLI 三阶段和真实 operator gate**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langsmith_workflow_regressions.py --inspect

MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langsmith_workflow_regressions.py --preflight \
  --allow-real-provider --allow-workflow-side-effects

MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langsmith_workflow_regressions.py --run \
  --run-name <unique-run-name> \
  --allow-real-provider --allow-workflow-side-effects
```

`--inspect` 只读本地 schema，不联网；preflight/run 必须同时满足 real mode、Provider/LangSmith/saver/artifact readiness 和 operator flags，任何缺失 fail-closed，不回退 mock。正式 run 完成后从 LangSmith API 分页回查 trace tree 和四项 Feedback；SDK 内存对象不算远端证据。

- [ ] **Step 6: 运行 GREEN、inspect 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_langsmith_workflow_experiment.py \
  tests/tdd/native-langgraph-m3/test_langsmith_workflow_evaluators.py

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langsmith_workflow_regressions.py --inspect

git add evals/langsmith_workflow_regression \
  scripts/run_langsmith_workflow_regressions.py \
  scripts/README.md evals/README.md \
  tests/tdd/native-langgraph-m3/test_langsmith_workflow_experiment.py \
  tests/tdd/native-langgraph-m3/test_langsmith_workflow_evaluators.py
git commit -m "feat(evals): evaluate durable workflow graph in LangSmith"
```

Expected: offline pytest/inspect PASS。真实 Experiment 必须由 operator 明确授权运行；缺真实远端证据时 M3 报告只能写 offline implementation ready，不能写 LangSmith acceptance complete。

---

### Task 8: 删除 Deep Research 影子 scheduler 路径、回补 core/authority 与 M3 验收

**Files:**
- Modify: `src/assistant_agent/workflows/runtime.py`
- Modify: `src/assistant_agent/workflows/worker.py`
- Modify: `src/assistant_agent/workflows/execution.py`
- Modify: `src/assistant_agent/workflows/planning.py`
- Modify: `src/assistant_agent/workflows/observed_store.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/api/app.py`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_durable_lifecycle.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `evals/README.md`
- Create: `tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py`
- Create: `.superpowers/sdd/2026-08-12-native-langgraph-m3/m3-final-report.md`

**Interfaces:**
- Removes from `deep_research`: `DurableWorkflowWorker` polling、`claim_ready_work_item`、work-item lease/heartbeat、revision CAS merge、`next_ready_work_item` execution decision、`WorkflowRuntime._refresh_ready_items/_revise_for_repair` 和 Workflow OTel shadow spans。
- Keeps through M4 only: old scheduler implementation for explicitly allowlisted legacy `long_horizon` internal records；必须在代码与 authority 标注 sunset owner=M4，不能被 Deep Research composition root 引用。
- Keeps permanently: Workflow submission/owner/idempotency business record、artifact store、audit/events，以及 consumer inventory 证明需要的窄 product query projection；不保留 raw `WorkflowBundle`。keeps all `automation/durable_tasks` code and tests untouched。

- [ ] **Step 1: 写 deletion gate RED**

`test_no_deep_research_scheduler.py` 使用 AST/import spy 与真实 Deep Research run，断言 production wiring 中 `deep_research` 不可到达以下符号：

```text
DurableWorkflowWorker.run_once
WorkflowRuntime.run_claim
WorkflowStore.claim_ready_work_item
WorkflowStore.renew_work_item_lease
claim_ready_item_in_bundle
next_ready_work_item
ObservedWorkflowStore.claim_ready_work_item
```

同时断言 `long_horizon` legacy allowlist 仍可显式构造；`assistant_agent.automation.durable_tasks` import、core durable schedule/resume 用例继续通过。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py
```

Expected: FAIL，当前 FastAPI worker 和 Runtime 仍将 Deep Research 接到旧 scheduler。

- [ ] **Step 3: 删除/隔离实际重复路径**

删除 `_start_deep_research_workflow` 的 old-service submit/worker wiring、`AgentRuntimeWorkItemExecutor` 中 Deep Research 特判和 `_READ_TOOLS_BY_KIND` 对 Deep Research 的隐式节点 kind 路由、Deep Research 的 workflow OTel observer 装配。`WorkflowRuntime`/`DurableWorkflowWorker` 若仍被 `long_horizon` compatibility 使用，则 constructor 强制 non-empty `included_workflow_types` 且拒绝 `deep_research`；所有兼容调用集中到一个 legacy composition root，不能散落 Runtime/API。

旧 lease/CAS 字段可为 M4 读取 legacy record 暂存于 model/SQLite，但 Deep Research 新 record 的 graph-backed projection 不写、不读这些字段。`planning.py` 只供 legacy projection，Deep Research progress 路径不 import。没有真实 consumer 的 old observer wrapper 直接删除；若 legacy test 仍依赖，保留文件但 production source 引用数为 0，并在 M4 删除清单登记。

- [ ] **Step 4: 更新 core invariant 与最小永久测试**

更新：

- `DUR-001`：通用 durable schedule 原有语义保持；新增 generic probe `DurableWorkflowGraph` 的 `Send`/join/checkpoint/resume 不依赖 claim/lease scheduler。
- `LOOP-001`：graph family 现在包含可嵌套 AssistantTurn profiles 和父 DurableWorkflowGraph，child runtime state 隔离。
- `IDENT-001`：workflow/thread/run 身份与 owner-bound resume。
- `OBS-001`：LangSmith 直接观察父图/子图，产品 projector 不重建 execution tree。

`tests/core/integration/test_durable_lifecycle.py` 只使用无业务语义 probe plan、`InMemorySaver` 和 scripted child；不导入 Deep Research definition/prompt/真实 Provider。`test_runtime_lifecycle.py` 只增加 runtime context 隔离/compiled graph family 的最小稳定断言，不复制 M3 feature 细节。

- [ ] **Step 5: 同步 current authority**

`docs/tool-calling-architecture.md` 记录 Workflow v2 领域模型与 Graph execution 边界、Deep Research cutover、legacy long_horizon M4 sunset、durable_tasks 非本次范围；`docs/runtime-event-stream-architecture.md` 记录父 graph async stream、Runtime Context、product projection、interrupt/resume；`docs/observability-harness.md` 和 `evals/README.md` 记录 LangSmith workflow trace/eval 是新增能力唯一目标，不声称 M5 Langfuse 已退出。

- [ ] **Step 6: 运行完整离线验收**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3 \
  tests/tdd/native-langgraph-m2 \
  tests/tdd/native-langgraph-runtime \
  tests/tdd/deep-research-mode

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/workflows \
  evals/langsmith_workflow_regression \
  scripts/run_langsmith_workflow_regressions.py

git diff --check
```

`tests/tdd/durable-workflow-e2e/` 是旧 scheduler 开发期临时测试，不作为 M3 兼容权威：Task 6 inventory 先
逐文件分类；仍覆盖 owner/artifact/真实消费者/explicit `long_horizon` legacy 的用例单独运行，直接绑定
Deep Research `WorkflowBundle/claim/lease/CAS/ready` 的用例记录为 `superseded_by_native_langgraph_m3`，不为让其
继续通过而恢复旧架构，也不擅自删除目录。若其他历史 TDD 已被用户手动删除，从命令移除路径并在 final
report 精确记录；不得为通过命令重建旧 feature test。

- [ ] **Step 7: 运行 deletion/source gates**

```bash
rg -n "deep_research" \
  src/assistant_agent/workflows/worker.py \
  src/assistant_agent/workflows/runtime.py \
  src/assistant_agent/workflows/observed_store.py

rg -n "ThreadPoolExecutor|claim_ready_work_item|renew_work_item_lease|run_claim|next_ready_work_item" \
  src/assistant_agent/workflows/durable_graph*.py \
  src/assistant_agent/workflows/graph_*.py

rg -n "automation\.durable_tasks|DurableTaskService|DurableTaskWorker" \
  src/assistant_agent tests/core
```

Expected: 前两项无 Deep Research/native graph scheduler 命中；第三项仍有受保护 durable task 实现与测试。再通过 runtime probe 检查 compiled graph 含 stable nodes/edges/conditional routes、worker `Send`、reducer、child namespace、retry/timeout/error handler 和 checkpointer，而不是只做文本搜索。

- [ ] **Step 8: 写 M3 final report、独立审查并提交**

报告逐项列出：Task 1–8 commit、Graph API 实际证据、SQLite 跨进程 gate、LangSmith真实 operator evidence、Agent-Service/媒体兼容、删除清单、core invariant、测试命令/计数、未完成项。以下任一缺失时状态必须是 `offline implementation ready; M3 acceptance pending`：

1. M2 Task 2 official async SQLite saver 未完成；
2. persistent cross-process tests 未通过；
3. LangSmith真实 Experiment/tree/Feedback 未由 operator 验收；
4. Deep Research 仍可达旧 scheduler/lease/CAS/ready-node/OTel 路径；
5. Agent-Service/media consumer inventory 的受保护投影或 Tool 治理兼容未证明。

完成 spec compliance review 与 code-quality review，修复后重新运行受影响验收，再提交：

```bash
git add src/assistant_agent/workflows src/assistant_agent/runtime/runtime.py \
  src/assistant_agent/api/app.py \
  tests/core/INVARIANTS.md \
  tests/core/integration/test_durable_lifecycle.py \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py \
  docs/tool-calling-architecture.md \
  docs/runtime-event-stream-architecture.md \
  docs/observability-harness.md evals/README.md \
  .superpowers/sdd/2026-08-12-native-langgraph-m3/m3-final-report.md
git commit -m "refactor(workflows): retire deep research scheduler path"
```

## M3 完成判据映射

| Master spec / Graph API 要求 | 权威证据 |
| --- | --- |
| strict State / reducer / Runtime Context | Task 1 state schema、inventory、algebraic reducer tests、parallel context isolation |
| planner AssistantTurnGraph + v2 admission | Task 2 native planning subgraph stream/snapshot 与非法 proposal tests |
| Conditional Edge + `Send` + Pregel super-step + join | Task 3真实 compiled graph task/checkpoint stream、barrier 与 arbitrary DAG waves |
| verifier subgraph + `Command` repair/publish | Task 4 destinations/无静态 edge、generation closure 与 unrelated branch 不重跑 |
| Retry Policy / Timeout / Fallback | Task 4 compiled node policy、transient retry、timeout/error handler tests |
| Checkpoint / Checkpointer / Thread / Interrupt / Resume | Task 5 official async SQLite、`durability="sync"`、multi-interrupt map、跨进程恢复 |
| Compile / Invoke / Stream / Streaming Modes / Subgraph | Tasks 2–7 native v2 stream、subgraph namespaces 和 direct compiled target |
| business Memory/Store 边界 | Tasks 1、6：服务只在 Runtime Context；SQLite 只保存业务事实和产品投影 |
| Agent-Service / media / Tool governance 不变 | Task 6 compatibility 与 governed child context tests |
| LangSmith 原生 trace/eval | Task 7真实 Dataset/Experiment/tree/Feedback operator evidence |
| 旧 Deep Research scheduler/影子 trace 退出 | Task 8 runtime spies、source gates、production composition audit |
| Time Travel / Replay / Fork 不造自研替代 | Global Constraints；M5 承接产品化，本计划只保留 native state history 能力 |

## 执行顺序与并行边界

- Task 1 是 state/context 基础；通过 review 后 Task 2 才能绑定稳定接口。
- Task 2 通过 review 后，Task 3 实现完整 worker topology；Task 4 依赖 Task 3 的 wave/join。
- M2 Task 2 未授权时，Task 1–4 可在 `InMemorySaver` 下完成并分别 review；Task 5 persistent gate 不可越过。
- Task 5 通过后，Task 6 才能切 production Deep Research；不能先把 API 指向非持久 graph。
- Task 7 可在 Task 6 接口冻结后与 Task 8 的文档/删除 inventory 研究并行，但两个任务修改 `evals/README.md` 时必须由一个 owner 串行合并。
- 每个 Task 使用独立 implementer，并在下一 Task 开始前完成 spec compliance + code quality review；并行 agent 不共享同一 worktree 的未提交文件。
