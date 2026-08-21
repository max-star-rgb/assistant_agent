# Planning Graph 统一恢复路由 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 planner 预算耗尽和 worker 执行失败改造成有界、可 checkpoint、可 replan 的原生 LangGraph 恢复流，同时冻结复用成功结果并提供受控终态。

**Architecture:** 保留唯一 `AssistantRootGraph`、共享 `AssistantFastAgent` 与确定性 DAG scheduler；新增 phase-aware 预算 middleware、严格 execution outcome、generation-aware recovery state 和显式 recovery nodes。预期内失败进入 state/conditional edge，取消、权限和程序契约错误继续原生传播。

**Tech Stack:** Python 3.12、Pydantic v2、LangChain `create_agent`/middleware/structured output、LangGraph StateGraph/Send/checkpoint/stream、pytest。

**Spec:** `docs/superpowers/specs/2026-08-21-planning-recovery-routing-design.md`

## Global Constraints

- 默认测试环境固定为 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，pytest 不读取真实 `.env`、不访问网络或真实 Provider。
- 继续复用同一个 `AssistantFastAgent`；不得新建 planner/worker Agent、supervisor Runtime、旁路 queue 或关键词路由。
- 以 `config.max_tool_iterations` 为基础额度 `B`：fast Tool/model=`B/B+1`，planner=`2B/2B+1`，worker=`B/B+1`，finalizer=`0/1`。
- planner operational attempt 最多 2 次，worker operational attempt 最多 3 次，执行期 replan 最多 2 次。
- 全图 Tool/model/node-attempt 上限分别为 `8B`、`10B`、`32`；recovery history 最多 32 条。
- 成功 `WorkerResult` 默认冻结，任何 generation 都不得覆盖或自动重放。
- recovery state、checkpoint、prompt 和 stream 只保存稳定 code 与有界结构化摘要，不保存 exception、Provider body、Tool 参数/原始结果或凭据。
- `GraphBubbleUp`、interrupt、cancel、Permission、schema/type/assertion 与未分类不可重试 HTTP 4xx 不进入 recovery 降级。
- 详细 RED/GREEN 测试只进入可删除的 `tests/tdd/planning-recovery-routing/`；核心只更新已登记的 `LOOP-001` 与 `CTX-001` 负责文件。
- 当前工作树已有用户改动；每次 commit 只暂存本任务源码与测试，不提交 `docs/superpowers/**` 设计/计划文件，不碰无关修改。

---

## File Structure

- Create `src/assistant_agent/native_agent/planning_budget.py`：可信预算 policy、phase-aware middleware、预算终止消息与 usage 投影。
- Create `src/assistant_agent/native_agent/planning_recovery.py`：错误分类、outcome assessment、冻结/合并、recovery context、wave reservation 与安全 custom event。
- Modify `src/assistant_agent/native_agent/models.py`：严格预算、失败、planner/worker outcome、recovery decision、plan v2 schema。
- Modify `src/assistant_agent/native_agent/state.py`：FastAgent/Planning/Worker recovery channel 与确定性 reducer。
- Modify `src/assistant_agent/native_agent/planning_phase.py`：worker structured response 与 plan v2 phase projection。
- Modify `src/assistant_agent/native_agent/fast_agent.py`：以 phase-aware middleware 替换统一官方 run limit，保留 ToolNode/retry/HITL/summarization。
- Modify `src/assistant_agent/native_agent/planning_graph.py`：接入 assess/retry/replan/reserve/finalize 拓扑及 generation-aware scheduler。
- Modify `src/assistant_agent/agent_server/services.py`：从 `max_tool_iterations` 一次构造并注入同一预算 policy。
- Create `tests/tdd/planning-recovery-routing/`：本功能全部细粒度 RED/GREEN。
- Modify `tests/tdd/native-high-agency-planner/`：将既有计划 schema fixture 迁移到 v2，并保持既有行为覆盖。
- Modify `tests/core/INVARIANTS.md`、`tests/core/integration/test_runtime_lifecycle.py`、`tests/core/integration/test_context_lifecycle.py`：更新 `LOOP-001`、`CTX-001` 的稳定结构化契约。
- Modify `docs/runtime-event-stream-architecture.md`、`docs/context_engineering_status.md`、`docs/tool-calling-architecture.md`：同步当前 owner authority。

---

### Task 1: 恢复领域模型与确定性 reducer

**Files:**
- Modify: `src/assistant_agent/native_agent/models.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Create: `tests/tdd/planning-recovery-routing/test_recovery_models.py`

**Interfaces:**
- Consumes: 现有 `WorkerResult`、`PlannerEvidence`、`AgentState`。
- Produces: `BudgetUsage`、`FailureFact`、`PlannerOutcome`、`WorkerCompletion`、`WorkerOutcome`、`RecoveryDecision`、`merge_worker_outcomes()`、`merge_frozen_worker_results()`、`add_budget_usage()`。

- [ ] **Step 1: 创建模型和 reducer 的失败测试**

```python
def test_worker_outcome_requires_result_only_for_success() -> None:
    with pytest.raises(ValidationError):
        WorkerOutcome(
            execution_id="g0:route:a1",
            plan_generation=0,
            work_item_id="route",
            attempt=1,
            status="succeeded",
            usage=BudgetUsage(),
        )


def test_worker_outcome_reducer_is_idempotent_and_rejects_conflict() -> None:
    first = _successful_worker_outcome("g0:route:a1", content="route-v1")
    assert merge_worker_outcomes({}, {first.execution_id: first}) == {
        first.execution_id: first
    }
    assert merge_worker_outcomes(
        {first.execution_id: first}, {first.execution_id: first}
    ) == {first.execution_id: first}
    conflict = first.model_copy(
        update={"result": first.result.model_copy(update={"content": "route-v2"})}
    )
    with pytest.raises(ValueError, match="conflicting worker outcome"):
        merge_worker_outcomes(
            {first.execution_id: first}, {conflict.execution_id: conflict}
        )


def test_budget_usage_adds_each_counter() -> None:
    assert add_budget_usage(
        BudgetUsage(model_calls=2, tool_calls=1),
        BudgetUsage(model_calls=3, node_attempts=1, replans=1),
    ) == BudgetUsage(model_calls=5, tool_calls=1, node_attempts=1, replans=1)
```

- [ ] **Step 2: 运行新测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_recovery_models.py
```

Expected: collection fails because recovery models/reducers are not defined.

- [ ] **Step 3: 在 `models.py` 增加严格模型**

```python
class BudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    node_attempts: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)


FailureCategory = Literal[
    "budget_exhausted", "operational", "business_failure",
    "authorization", "contract_bug",
]


class FailureFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    category: FailureCategory
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    phase: Literal["planner", "worker", "finalizer"]
    plan_generation: int = Field(ge=0)
    work_item_id: str | None = Field(default=None, max_length=120)
    attempt: int = Field(ge=1)


class WorkerCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    status: Literal["completed", "insufficient"]
    content: str = Field(min_length=1, max_length=100_000)
```

按 spec 增加 `PlannerOutcome`、`WorkerOutcome`、`RecoveryDecision`，用 `model_validator` 强制成功状态必须携带 candidate/result、失败状态必须携带 `FailureFact`，并同步 `__all__`。

- [ ] **Step 4: 在 `state.py` 增加公开 reducer**

```python
def merge_worker_outcomes(
    left: Mapping[str, WorkerOutcome] | None,
    right: Mapping[str, WorkerOutcome] | None,
) -> dict[str, WorkerOutcome]:
    return _merge_immutable_mapping(
        left, right, conflict_message="conflicting worker outcome"
    )


def merge_frozen_worker_results(
    left: Mapping[str, WorkerResult] | None,
    right: Mapping[str, WorkerResult] | None,
) -> dict[str, WorkerResult]:
    return _merge_immutable_mapping(
        left, right, conflict_message="conflicting frozen worker result"
    )


def add_budget_usage(left: BudgetUsage | None, right: BudgetUsage | None) -> BudgetUsage:
    lhs, rhs = left or BudgetUsage(), right or BudgetUsage()
    return BudgetUsage(
        model_calls=lhs.model_calls + rhs.model_calls,
        tool_calls=lhs.tool_calls + rhs.tool_calls,
        node_attempts=lhs.node_attempts + rhs.node_attempts,
        replans=lhs.replans + rhs.replans,
    )
```

先只导出 reducer；具体 state channel 在后续任务按消费者一起接线。

- [ ] **Step 5: 运行模型测试并确认 GREEN**

Run: same command as Step 2.  
Expected: all tests pass.

- [ ] **Step 6: 提交 Task 1**

```bash
git add src/assistant_agent/native_agent/models.py \
  src/assistant_agent/native_agent/state.py \
  tests/tdd/planning-recovery-routing/test_recovery_models.py
git commit -m "feat: add planning recovery contracts"
```

---

### Task 2: Plan v2、冻结引用与 recovery admission

**Files:**
- Modify: `src/assistant_agent/native_agent/models.py`
- Modify: `src/assistant_agent/native_agent/planning_graph.py`
- Create: `tests/tdd/planning-recovery-routing/test_recovery_admission.py`
- Modify: `tests/tdd/native-high-agency-planner/test_plan_models.py`
- Modify: all existing mock fixtures returned by `rg -l 'native_plan_v1' src tests/core tests/tdd/native-high-agency-planner`

**Interfaces:**
- Consumes: `NativePlanProposal`、`PlanningAdmissionPolicy`、frozen result IDs、historical/replannable node IDs。
- Produces: `native_plan_v2`、`NativePlanNode.replaces_node_ids`、`NativePlanNode.frozen_dependency_ids`、`PlanDeliverable.frozen_result_refs`，以及扩展后的 `admit_native_plan()`。

- [ ] **Step 1: 写 recovery admission RED**

```python
def test_recovery_plan_accepts_unique_replacement_and_frozen_dependency() -> None:
    proposal = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(NativePlanNode(
            node_id="route_replacement_g1",
            objective="replace failed route",
            replaces_node_ids=("route_g0",),
            frozen_dependency_ids=("weather_g0",),
        ),),
        deliverables=(PlanDeliverable(
            deliverable_id="answer",
            description="answer",
            producer_node_ids=("route_replacement_g1",),
            frozen_result_refs=("weather_g0",),
        ),),
    )
    assert admit_native_plan(
        proposal,
        policy=_policy(),
        evidence=(),
        active_skill_ids=(),
        plan_generation=1,
        historical_node_ids={"route_g0", "weather_g0"},
        replannable_node_ids={"route_g0"},
        frozen_result_ids={"weather_g0"},
    ) == proposal


@pytest.mark.parametrize(
    ("replacement", "code"),
    [("weather_g0", "replace_frozen_result"), ("unknown_g0", "unknown_replacement")],
)
def test_recovery_plan_rejects_illegal_replacement(replacement: str, code: str) -> None:
    with pytest.raises(NativePlanAdmissionError) as caught:
        admit_native_plan(
            _replacement_plan(replacement),
            policy=_policy(), evidence=(), active_skill_ids=(),
            plan_generation=1,
            historical_node_ids={"route_g0", "weather_g0"},
            replannable_node_ids={"route_g0"},
            frozen_result_ids={"weather_g0"},
        )
    assert caught.value.code == code
```

同时覆盖：首次 generation 禁止 replacements/frozen refs、跨 generation node ID 重用、同一旧节点被两个新节点替代、普通 `depends_on` 引用 frozen ID、deliverable frozen ref 不存在。

- [ ] **Step 2: 运行 admission 测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_recovery_admission.py
```

Expected: `native_plan_v2` and recovery fields/signature are absent.

- [ ] **Step 3: 实现 plan v2 schema**

```python
class NativePlanNode(BaseModel):
    # retain existing fields
    replaces_node_ids: tuple[str, ...] = Field(default=(), max_length=64)
    frozen_dependency_ids: tuple[str, ...] = Field(default=(), max_length=64)


class PlanDeliverable(BaseModel):
    # retain existing fields
    frozen_result_refs: tuple[str, ...] = Field(default=(), max_length=64)


class NativePlanProposal(BaseModel):
    schema_version: Literal["native_plan_v2"]
```

扩展 tuple normalization/unique validators；deliverable producer 条件改为三者至少一个：current producer、planner evidence、frozen result。

- [ ] **Step 4: 扩展 `admit_native_plan` 的确定性参数和错误码**

```python
def admit_native_plan(
    proposal: NativePlanProposal,
    *,
    policy: PlanningAdmissionPolicy,
    evidence: Sequence[PlannerEvidence],
    active_skill_ids: Collection[str],
    plan_generation: int = 0,
    historical_node_ids: Collection[str] = (),
    replannable_node_ids: Collection[str] = (),
    frozen_result_ids: Collection[str] = (),
) -> NativePlanProposal:
```

校验顺序保持稳定：generation/node identity → replacement → frozen dependency/ref → 原有 Skill/Tool/evidence/DAG/depth。新增稳定 code：`reused_node_id`、`initial_replacement_forbidden`、`unknown_replacement`、`replace_frozen_result`、`duplicate_replacement`、`unknown_frozen_dependency`、`unknown_frozen_deliverable_ref`。

- [ ] **Step 5: 将现有 v1 fixture 机械迁移到 v2**

先运行：

```bash
rg -n "native_plan_v1" src tests/core tests/tdd/native-high-agency-planner
```

使用 `apply_patch` 将列出的 schema literal、mock Provider payload 和 planner prompt 全部改为 `native_plan_v2`；不得修改无关 fixture 内容。再次运行 `rg`，Expected: no matches in `src`, `tests/core`, or `tests/tdd/native-high-agency-planner`.

- [ ] **Step 6: 运行 admission 与既有 plan tests**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_recovery_admission.py \
  tests/tdd/native-high-agency-planner/test_plan_models.py \
  tests/tdd/native-high-agency-planner/test_plan_admission.py
```

Expected: all pass.

- [ ] **Step 7: 提交 Task 2**

```bash
git add src/assistant_agent/native_agent/models.py \
  src/assistant_agent/native_agent/planning_graph.py \
  src/assistant_agent/native_agent/planning_phase.py \
  src/assistant_agent/native_agent/providers.py \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_memory_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py \
  tests/tdd/native-high-agency-planner \
  tests/tdd/planning-recovery-routing/test_recovery_admission.py
git commit -m "feat: admit generation-aware recovery plans"
```

---

### Task 3: Phase-aware Tool/model 预算与 worker structured completion

**Files:**
- Create: `src/assistant_agent/native_agent/planning_budget.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `src/assistant_agent/native_agent/planning_phase.py`
- Create: `tests/tdd/planning-recovery-routing/test_phase_budget.py`
- Modify: `tests/tdd/native-high-agency-planner/test_planning_phase.py`

**Interfaces:**
- Consumes: `agent_phase`、基础额度 `B`、标准 `AIMessage.tool_calls`、`ToolMessage`。
- Produces: `PlanningBudgetPolicy.from_base(base)`、`PhaseBudgetMiddleware`、FastAgent state 的 `phase_budget_status/phase_budget_usage`、`worker_response_format()`。

- [ ] **Step 1: 写 policy 与 middleware RED**

```python
def test_budget_policy_derives_approved_limits() -> None:
    policy = PlanningBudgetPolicy.from_base(8)
    assert policy.phase_limits("fast") == PhaseLimits(8, 9)
    assert policy.phase_limits("planner") == PhaseLimits(16, 17)
    assert policy.phase_limits("worker") == PhaseLimits(8, 9)
    assert policy.phase_limits("finalizer") == PhaseLimits(0, 1)
    assert policy.graph_tool_limit == 64
    assert policy.graph_model_limit == 80
    assert policy.graph_node_attempt_limit == 32
    assert policy.max_replans == 2


def test_planner_tool_budget_returns_closed_messages_instead_of_raising() -> None:
    result = asyncio.run(_run_budget_loop(phase="planner", base=1))
    assert result["phase_budget_status"] == "exhausted"
    assert result["phase_budget_usage"].tool_calls == 2
    assert _successful_tool_message_ids(result["messages"]) == ["call-1", "call-2"]
    blocked = _tool_message(result["messages"], "call-3")
    assert blocked.status == "error"
    assert result["messages"][-1].type == "ai"
```

该 scripted model 连续申请 3 个 Probe Tool；`base=1` 时 planner ceiling 为 2，前两次真实执行，第 3 次被标准 error ToolMessage 闭合且 Agent 正常返回。

- [ ] **Step 2: 运行 phase budget tests 确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_phase_budget.py \
  tests/tdd/native-high-agency-planner/test_planning_phase.py
```

Expected: missing policy/middleware and worker response format.

- [ ] **Step 3: 实现可信预算 policy**

```python
@dataclass(frozen=True)
class PhaseLimits:
    tool_calls: int
    model_calls: int


@dataclass(frozen=True)
class PlanningBudgetPolicy:
    base: int
    graph_tool_limit: int
    graph_model_limit: int
    graph_node_attempt_limit: int
    planner_attempts: int = 2
    worker_attempts: int = 3
    max_replans: int = 2
    recovery_history_limit: int = 32

    @classmethod
    def from_base(cls, base: int) -> "PlanningBudgetPolicy":
        if base <= 0:
            raise ValueError("planning budget base must be positive")
        return cls(base, 8 * base, 10 * base, 32)

    def phase_limits(self, phase: AgentPhase) -> PhaseLimits:
        return {
            "fast": PhaseLimits(self.base, self.base + 1),
            "planner": PhaseLimits(2 * self.base, 2 * self.base + 1),
            "worker": PhaseLimits(self.base, self.base + 1),
            "finalizer": PhaseLimits(0, 1),
        }[phase]
```

- [ ] **Step 4: 实现 `PhaseBudgetMiddleware`**

middleware 使用公开 AgentMiddleware hooks：`before_model` 增加 model count并在下一次调用会超限时返回 `jump_to="end"`；`after_model` 检查最后一个 AIMessage 的 Tool calls。超限批次不执行任何 pending Tool，为全部 pending call 构造配对 `ToolMessage(status="error")`，追加一个无 Tool call 的安全 `AIMessage`，写入 `phase_budget_status="exhausted"`、对应的明确 `BudgetUsage` 计数并跳到 end。未超限只更新计数。

```python
class PhaseBudgetMiddleware(AgentMiddleware):
    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        phase = _agent_phase(state)
        limit = self.policy.phase_limits(phase).model_calls
        current = int(state.get("phase_model_call_count", 0))
        if current + 1 > limit:
            return _model_budget_end_update(phase=phase, current=current)
        return {
            "phase_model_call_count": current + 1,
            "phase_budget_usage": BudgetUsage(model_calls=1),
        }

    @hook_config(can_jump_to=["end"])
    def after_model(self, state, runtime) -> dict[str, Any] | None:
        calls = _last_ai_tool_calls(state.get("messages", ()))
        if not calls:
            return None
        phase = _agent_phase(state)
        limit = self.policy.phase_limits(phase).tool_calls
        current = int(state.get("phase_tool_call_count", 0))
        if current + len(calls) > limit:
            return _tool_budget_end_update(
                phase=phase, current=current, pending_calls=calls
            )
        return {
            "phase_tool_call_count": current + len(calls),
            "phase_budget_usage": BudgetUsage(tool_calls=len(calls)),
        }
```

`_model_budget_end_update()` 和 `_tool_budget_end_update()` 都返回 `jump_to="end"`、`phase_budget_status="exhausted"` 和无敏感正文的标准消息；Tool helper 为 `pending_calls` 中每个 ID 恰好生成一个配对 `ToolMessage`。

为 `FastAgentState` 增加严格 channel：`phase_model_call_count`、`phase_tool_call_count`、`phase_budget_status`、`phase_budget_usage`。每次 `fast_agent.ainvoke` 输入不携带旧 phase count，因此计数作用域是当前 phase attempt，不是 Agent Server root run。

- [ ] **Step 5: 替换共享 fast agent 的两个统一 limit middleware**

在现有 `build_fast_agent` keyword-only 参数中增加 `budget_policy: PlanningBudgetPolicy | None = None`，使用 `budget_policy or PlanningBudgetPolicy.from_base(8)`；移除 `ModelCallLimitMiddleware` 和全局 `ToolCallLimitMiddleware`，在原 middleware 顺序相同位置加入 `PhaseBudgetMiddleware`。保留 `live_view_inspect` 专项官方 limit、ToolRetry、HITL、summarization 与 `ToolProgressMiddleware`。

- [ ] **Step 6: 给 worker phase 增加严格 structured response**

```python
def worker_response_format() -> ToolStrategy:
    return ToolStrategy(WorkerCompletion)
```

`PlanningPhaseMiddleware._project()` 的 worker 分支设置 `response_format=worker_response_format()`；planner 使用 plan v2，finalizer 仍为空 Tool/无 structured response。扩展 phase test 断言 worker response schema 存在且 allowlist 仍 fail closed。

- [ ] **Step 7: 运行 phase tests 并确认 GREEN**

Run: same command as Step 2.  
Expected: all pass, and budget exhaustion does not raise.

- [ ] **Step 8: 提交 Task 3**

```bash
git add src/assistant_agent/native_agent/planning_budget.py \
  src/assistant_agent/native_agent/state.py \
  src/assistant_agent/native_agent/fast_agent.py \
  src/assistant_agent/native_agent/planning_phase.py \
  tests/tdd/planning-recovery-routing/test_phase_budget.py \
  tests/tdd/native-high-agency-planner/test_planning_phase.py
git commit -m "feat: enforce phase-aware planning budgets"
```

---

### Task 4: Planner outcome、预算恢复与 admission revision

**Files:**
- Create: `src/assistant_agent/native_agent/planning_recovery.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/planning_graph.py`
- Create: `tests/tdd/planning-recovery-routing/test_planner_recovery.py`

**Interfaces:**
- Consumes: FastAgent `phase_budget_status/usage`、`PlannerEvidence`、plan v2 admission、`PlanningBudgetPolicy`。
- Produces: `classify_operational_failure()`、`assess_planner_node()`、`route_after_planner_assessment()`、`prepare_replan_node()`、`planner_failure_node()`，以及先保证拓扑可编译的最小 `controlled_finalize_node()`。

- [ ] **Step 1: 写真实 trace 对应的 planner RED**

```python
def test_planner_budget_exhaustion_preserves_evidence_and_replans() -> None:
    graph, probe = _budget_exhausting_planner_graph(base=1)
    result = asyncio.run(graph.ainvoke(_planning_input(), context=AssistantRunContext()))

    assert probe.planner_attempts == 2
    assert [item.evidence_id for item in result["planner_evidence"]] == [
        "call-1", "call-2"
    ]
    assert result["plan_generation"] == 1
    assert result["budget_usage"].replans == 1
    assert result["recovery_history"][0].action == "replan"
    assert result["recovery_history"][0].reason_code == "planner_tool_budget_exhausted"
```

第二次 planner scripted response 必须直接复用 recovery context 中的 evidence IDs 并生成零 worker plan；断言前两次 Tool 不再执行。

- [ ] **Step 2: 写异常分类 RED**

```python
@pytest.mark.parametrize(
    "error", [TimeoutError(), ConnectionError(), _http_error(status_code=503)]
)
def test_operational_classifier_accepts_only_retryable_errors(error) -> None:
    assert classify_operational_failure(error)


@pytest.mark.parametrize(
    "error", [PermissionError(), TypeError(), ValueError(), GraphInterrupt()]
)
def test_operational_classifier_rejects_control_and_contract_errors(error) -> None:
    assert not classify_operational_failure(error)
```

HTTP case 使用本地 fake response/status，不访问网络；覆盖 408/409/425/429/5xx 为真，400/401/403/404 为假。

- [ ] **Step 3: 运行 planner recovery tests 确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_planner_recovery.py
```

Expected: recovery nodes and state channels are absent.

- [ ] **Step 4: 接入 `PlanningState` 的 planner recovery channels**

```python
class PlanningState(AgentState):
    plan_generation: NotRequired[int]
    planner_outcome: NotRequired[PlannerOutcome | None]
    recovery_decision: NotRequired[RecoveryDecision | None]
    recovery_context: NotRequired[dict[str, JsonValue] | None]
    recovery_history: NotRequired[list[RecoveryDecision]]
    budget_usage: NotRequired[Annotated[BudgetUsage, add_budget_usage]]
    historical_node_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
    superseded_work_item_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
```

recovery history 只在顺序节点中整体替换为 `_bounded_history(existing, decision, limit=policy.recovery_history_limit)`，不使用并行 append reducer。

- [ ] **Step 5: 让 `planner_node` 始终先捕获 evidence 再产生 outcome**

正常 structured candidate 返回 status 为 `succeeded` 且携带该 candidate 的 `PlannerOutcome`；candidate 缺失且 `phase_budget_status=="exhausted"` 返回 status 为 `budget_exhausted` 的 outcome。只有既非 candidate 又非预算终止才抛 contract error。

`planner_failure_node` 仅把 `classify_operational_failure(error.error)==True` 转为 `operational_failed`；其他异常原样 raise。planner node 的 `RetryPolicy` 使用同一个窄 classifier 和 `policy.planner_attempts`。

- [ ] **Step 6: 添加 planner assessment/replan topology**

```python
builder.add_node("assess_planner", partial(assess_planner_node, policy=budget_policy))
builder.add_node("prepare_replan", partial(prepare_replan_node, policy=budget_policy))
builder.add_edge(START, "planner")
builder.add_edge("planner", "assess_planner")
builder.add_conditional_edges(
    "assess_planner",
    route_after_planner_assessment,
    ["admit_plan", "planner", "prepare_replan", "controlled_finalize"],
)
builder.add_edge("prepare_replan", "planner")
```

`prepare_replan_node` 首版处理 planner failure 时保留 evidence/Skill grant，构造固定字段 recovery context，增加 generation/replan usage；达到 `max_replans` 时 assessment 选择 controlled finalizer。

同一任务先加入最小 `controlled_finalize_node`：从当前 recovery decision 的稳定 `reason_code` 构造标准 `AIMessage`，不调用模型。Task 7 再扩展其 completed/missing 投影、metadata 与 stream 验证；这样 Task 4 结束时 graph 的所有 conditional destinations 已存在并可独立编译。

- [ ] **Step 7: 运行 planner recovery 与既有 revision tests**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_planner_recovery.py \
  tests/tdd/native-high-agency-planner/test_native_revision.py \
  tests/tdd/native-high-agency-planner/test_planner_execution.py
```

Expected: all pass; admission revision remains generation 0, execution recovery increments generation.

- [ ] **Step 8: 提交 Task 4**

```bash
git add src/assistant_agent/native_agent/planning_recovery.py \
  src/assistant_agent/native_agent/state.py \
  src/assistant_agent/native_agent/planning_graph.py \
  tests/tdd/planning-recovery-routing/test_planner_recovery.py
git commit -m "feat: route planner failures through recovery state"
```

---

### Task 5: Worker outcome、成功冻结与失败驱动 replan

**Files:**
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/planning_graph.py`
- Modify: `src/assistant_agent/native_agent/planning_recovery.py`
- Create: `tests/tdd/planning-recovery-routing/test_worker_recovery.py`

**Interfaces:**
- Consumes: `WorkerCompletion`、`WorkerOutcome`、plan generation、现有 worker Tool/Skill projection。
- Produces: generation-aware `worker_node()`、`worker_failure_node()`、`assess_workers_node()`、`freeze_successful_worker_results()` 和 worker recovery route。

- [ ] **Step 1: 写并行 worker 冻结/replan RED**

```python
def test_failed_worker_replans_without_replaying_successful_sibling() -> None:
    graph, model = _one_success_one_failure_graph()
    result = asyncio.run(graph.ainvoke(_planning_input(), context=AssistantRunContext()))

    assert model.calls_by_objective["weather_g0"] == 1
    assert model.calls_by_objective["route_g0"] == 3
    assert model.calls_by_objective["route_replacement_g1"] == 1
    assert result["frozen_worker_results"]["weather_g0"].content == "weather-ok"
    assert "route_g0" in result["superseded_work_item_ids"]
    assert result["plan_generation"] == 1
    assert result["messages"][-1].type == "ai"
```

初始 plan 同 wave 派发 weather/route；weather 成功，route 连续 Timeout。第二次 plan 通过 `frozen_dependency_ids=("weather_g0",)` 新建 `route_replacement_g1`。

- [ ] **Step 2: 写 business failure 与 fail-closed RED**

```python
def test_worker_insufficient_replans_without_same_input_retry() -> None:
    result, calls = _run_insufficient_worker()
    assert calls["insufficient_g0"] == 1
    assert result["recovery_history"][0].reason_code == "worker_business_insufficient"


@pytest.mark.parametrize("failure", [PermissionError("denied"), TypeError("bug")])
def test_worker_contract_or_permission_failure_propagates(failure: Exception) -> None:
    with pytest.raises(type(failure), match=str(failure)):
        _run_direct_worker_failure(failure)
```

- [ ] **Step 3: 运行 worker recovery tests 确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_worker_recovery.py
```

Expected: worker still returns/accumulates legacy `WorkerResult` and finalizes failures.

- [ ] **Step 4: 改造 WorkerState/PlanningState**

为 `WorkerState` 增加 required `execution_id`、`plan_generation`、`attempt`、`tool_call_allowance`；为 `PlanningState` 增加：

```python
worker_outcomes: NotRequired[
    Annotated[dict[str, WorkerOutcome], merge_worker_outcomes]
]
frozen_worker_results: NotRequired[
    Annotated[dict[str, WorkerResult], merge_frozen_worker_results]
]
worker_attempts: NotRequired[dict[str, int]]
```

`worker_attempts` 只由顺序 scheduler 更新，worker 并行节点不写同一个 key。

- [ ] **Step 5: 让 worker 返回严格 outcome**

`worker_node` 校验 `result["structured_response"]` 为 `WorkerCompletion`：completed 构造成功 `WorkerResult`；insufficient 构造 `business_failed`。FastAgent budget exhausted 构造 `budget_exhausted`。`worker_failure_node` 只将 operational exhaustion 转为 `operational_failed`，其他错误继续 raise。

所有 outcome 写入 `{execution_id: outcome}`；不再直接向权威 `worker_results` append。

- [ ] **Step 6: 实现 worker assessment 与冻结**

`assess_workers_node` 按 current plan order 读取 current generation 最新 attempt：

- success 且仍有 pending → scheduler；
- operational 且 attempt 未满 → retry；
- operational 耗尽、budget 或 business → replan；
- 全部 current nodes 成功 → finalizer。

`prepare_replan_node` 扩展为：把所有成功 outcome 写入 frozen map；将失败、依赖失败和尚未执行节点加入 superseded；构造未完成 deliverable/replannable IDs；增加 generation。不得把失败依赖机械伪造为成功或 legacy failed result。

- [ ] **Step 7: 更新 scheduler 使用 frozen/current outcome**

当前 generation 的 completed 集合只来自成功 outcome；`frozen_dependency_ids` 从 frozen map 投影为 `dependency_results`。普通 `depends_on` 仍只读取当前 generation。旧 generation failed outcome 不参与新 generation readiness。

- [ ] **Step 8: 运行 worker recovery 与既有 scheduler tests**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_worker_recovery.py \
  tests/tdd/native-high-agency-planner/test_scheduler.py
```

Expected: all pass after将既有“失败依赖直接 finalizer”临时测试更新为“失败触发 replan；预算耗尽才 controlled finalize”。

- [ ] **Step 9: 提交 Task 5**

```bash
git add src/assistant_agent/native_agent/state.py \
  src/assistant_agent/native_agent/planning_graph.py \
  src/assistant_agent/native_agent/planning_recovery.py \
  tests/tdd/planning-recovery-routing/test_worker_recovery.py \
  tests/tdd/native-high-agency-planner/test_scheduler.py
git commit -m "feat: replan around failed planning workers"
```

---

### Task 6: 全图预算、稳定 wave reservation 与恢复上限

**Files:**
- Modify: `src/assistant_agent/native_agent/planning_budget.py`
- Modify: `src/assistant_agent/native_agent/planning_recovery.py`
- Modify: `src/assistant_agent/native_agent/planning_graph.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Create: `tests/tdd/planning-recovery-routing/test_graph_budget.py`

**Interfaces:**
- Consumes: `PlanningBudgetPolicy`、current plan order、`BudgetUsage`、worker phase ceiling。
- Produces: `reserve_wave_budget_node()`、`reconcile_wave_budget_node()`、`WaveReservation`、稳定前缀派发与 recovery exhaustion decision。

- [ ] **Step 1: 写并行 reservation RED**

```python
def test_wave_reservation_uses_stable_prefix_without_exceeding_graph_budget() -> None:
    state = _ready_wave_state(node_ids=("a", "b", "c"), remaining_tool_calls=16)
    update = reserve_wave_budget_node(state, policy=PlanningBudgetPolicy.from_base(8))
    assert list(update["wave_reservations"]) == ["g0:a:a1", "g0:b:a1"]
    assert sum(item.tool_calls for item in update["wave_reservations"].values()) == 16


def test_replan_limit_routes_to_controlled_finalize() -> None:
    decision = assess_recovery_budget(
        BudgetUsage(replans=2), PlanningBudgetPolicy.from_base(8)
    )
    assert decision.action == "finalize"
    assert decision.reason_code == "replan_budget_exhausted"
```

另外覆盖 model/tool/node-attempt 三种全图上限及重复 checkpoint replay 的 reservation 幂等。

- [ ] **Step 2: 运行 graph budget tests 确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_graph_budget.py
```

Expected: reservation/reconciliation types and nodes absent.

- [ ] **Step 3: 定义 reservation contract**

```python
class WaveReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    execution_id: str
    plan_generation: int = Field(ge=0)
    work_item_id: str
    attempt: int = Field(ge=1)
    allowance: BudgetUsage
```

`wave_reservations` 使用不可冲突 mapping reducer；execution ID 重放必须产生完全相同 reservation。

- [ ] **Step 4: 实现稳定预留与核销**

`reserve_wave_budget_node` 按 plan 顺序计算 ready nodes，为每个节点预留至多 worker phase ceiling；剩余额度不足时只选稳定前缀。`route_scheduler` 只能 Send 已预留 execution IDs。`reconcile_wave_budget_node` 在 join 后以 outcome 实际 usage 核销，未使用额度不计入累计 usage，并删除/标记已核销 reservation；不得按 worker 到达顺序累计。

- [ ] **Step 5: 将全图上限接入 planner/worker assessment**

所有 assessment 在 retry/replan/Send 前调用统一 `remaining_budget()`。任何 counter 已达 ceiling：

- 不启动新节点；
- 形成稳定 `RecoveryDecision(action="finalize", reason_code="graph_*_budget_exhausted")`；
- 进入 controlled finalizer；
- contract/cancel/authorization 仍不转换。

- [ ] **Step 6: 运行 graph budget、planner、worker tests**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_graph_budget.py \
  tests/tdd/planning-recovery-routing/test_planner_recovery.py \
  tests/tdd/planning-recovery-routing/test_worker_recovery.py
```

Expected: all pass.

- [ ] **Step 7: 提交 Task 6**

```bash
git add src/assistant_agent/native_agent/planning_budget.py \
  src/assistant_agent/native_agent/planning_recovery.py \
  src/assistant_agent/native_agent/planning_graph.py \
  src/assistant_agent/native_agent/state.py \
  tests/tdd/planning-recovery-routing/test_graph_budget.py
git commit -m "feat: bound planning recovery across graph waves"
```

---

### Task 7: Controlled finalizer、checkpoint 恢复与原生 stream

**Files:**
- Modify: `src/assistant_agent/native_agent/planning_graph.py`
- Modify: `src/assistant_agent/native_agent/planning_recovery.py`
- Create: `tests/tdd/planning-recovery-routing/test_recovery_terminal.py`
- Create: `tests/tdd/planning-recovery-routing/test_recovery_stream.py`

**Interfaces:**
- Consumes: frozen/current success、未解决 `FailureFact`、recovery history、LangGraph stream writer/checkpointer。
- Produces: `controlled_finalize_node()`、`recovery_transition_event()`、namespace-aware updates/custom stream。

- [ ] **Step 1: 写 controlled terminal RED**

```python
def test_recovery_exhaustion_returns_standard_ai_message_without_model() -> None:
    result = asyncio.run(_exhausted_graph().ainvoke(
        _planning_input(), context=AssistantRunContext()
    ))
    terminal = result["messages"][-1]
    assert isinstance(terminal, AIMessage)
    assert terminal.response_metadata["recovery_status"] == "partial"
    assert terminal.response_metadata["failure_codes"] == [
        "replan_budget_exhausted"
    ]
```

断言 content 只机械列出 completed/missing IDs，不断言完整人类文案。

- [ ] **Step 2: 写 checkpoint/no-replay 与 stream RED**

```python
async def test_checkpoint_resume_does_not_replay_frozen_worker() -> None:
    graph, counter, config = _interrupting_recovery_graph()
    first = await graph.ainvoke(_planning_input(), config=config, context=AssistantRunContext())
    assert first["__interrupt__"]
    await graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}),
                        config=config, context=AssistantRunContext())
    assert counter["frozen-success"] == 1


async def test_recovery_stream_has_updates_and_custom_subgraph_namespace() -> None:
    parts = [part async for part in _root_graph().astream(
        _planning_input(), context=AssistantRunContext(),
        stream_mode=["updates", "custom", "messages"],
        subgraphs=True, version="v2",
    )]
    recovery = [p for p in parts if p["type"] == "custom"
                and p["data"].get("type") == "recovery_transition"]
    assert recovery
    assert recovery[0]["ns"]
    assert set(recovery[0]["data"]) == {
        "type", "from", "to", "reason_code", "plan_generation"
    }
```

- [ ] **Step 3: 运行 terminal/stream tests 确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_recovery_terminal.py \
  tests/tdd/planning-recovery-routing/test_recovery_stream.py
```

Expected: controlled node/custom recovery event absent.

- [ ] **Step 4: 实现正常 finalizer 输入与 deterministic fallback**

正常 finalizer payload 按 deliverable 顺序包含 frozen/current success 和 unresolved FailureFact；Tool 保持为空。operational model retry 耗尽后由 error handler跳到 `controlled_finalize`。fallback 构造 `AIMessage`，在 `response_metadata` 写入 `recovery_status` 和排序后的 `failure_codes`，正文不包含内部 exception、Tool 参数或 runtime 状态。

- [ ] **Step 5: 实现安全 custom recovery event**

```python
def recovery_transition_event(
    *, source: str, target: str, reason_code: str, plan_generation: int
) -> dict[str, str | int]:
    return {
        "type": "recovery_transition",
        "from": source,
        "to": target,
        "reason_code": reason_code,
        "plan_generation": plan_generation,
    }
```

顺序 recovery nodes 使用 `langgraph.config.get_stream_writer()` 写出事件；不得在 Tool/model callback 复制一份。updates 依靠节点标准 state delta 自动产生。
测试内的 compiled graph 使用 `subgraphs=True`；Agent Server SDK 的等价订阅参数必须保持 `stream_subgraphs=True`，不能只选择 `messages/updates/custom` 而省略子图流。

- [ ] **Step 6: 完成 checkpoint/replay 语义**

所有 active selection 从 checkpointed generation、outcomes、frozen map 和 reservation 重算；不保存 ready/completed 平行集合。resume 时 reducer 接受完全相同 outcome/reservation，拒绝冲突值。确认完成的 HITL Tool 与 frozen worker 不重放。

- [ ] **Step 7: 运行 terminal/stream 与上下文 HITL tests**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing/test_recovery_terminal.py \
  tests/tdd/planning-recovery-routing/test_recovery_stream.py \
  tests/core/integration/test_context_lifecycle.py
```

Expected: all pass.

- [ ] **Step 8: 提交 Task 7**

```bash
git add src/assistant_agent/native_agent/planning_graph.py \
  src/assistant_agent/native_agent/planning_recovery.py \
  tests/tdd/planning-recovery-routing/test_recovery_terminal.py \
  tests/tdd/planning-recovery-routing/test_recovery_stream.py \
  tests/core/integration/test_context_lifecycle.py
git commit -m "feat: finalize exhausted planning runs safely"
```

---

### Task 8: Production composition、核心不变量与 authority 同步

**Files:**
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/tool-calling-architecture.md`

**Interfaces:**
- Consumes: `PlanningBudgetPolicy.from_base()`、更新后的 `build_fast_agent()` 与 `build_planning_graph()`。
- Produces: production 使用同一 policy 实例、更新后的 `LOOP-001`/`CTX-001` 和当前 authority。

- [ ] **Step 1: 写 production composition/core RED**

在 `test_runtime_lifecycle.py` 增加一个通用 scripted planner probe：一个 worker 成功、一个 operational failure，断言原生节点路径包含 `assess_workers -> prepare_replan -> planner`，成功 sentinel 只出现一次，最终标准 AIMessage 存在。使用 `@pytest.mark.core_invariant("LOOP-001")`。

在 `test_context_lifecycle.py` 将 middleware 结构断言更新为：存在 `PhaseBudgetMiddleware`、不存在全局 all-tools `ToolCallLimitMiddleware`，但 live-view 专项 limiter、Summarization、HITL、ToolRetry 仍存在。使用 `@pytest.mark.core_invariant("CTX-001")`。

- [ ] **Step 2: 运行两个核心文件确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py
```

Expected: production composition has not injected the shared policy and invariant text/tests still describe old topology.

- [ ] **Step 3: 让 production 只构造一次预算 policy**

在 `AgentServerExecutionOwner.create()` 和同文件的第二个 composition 路径中：

```python
planning_budget = PlanningBudgetPolicy.from_base(config.max_tool_iterations)
fast_agent = build_fast_agent(
    model,
    tools,
    budget_policy=planning_budget,
    context_window_tokens=config.context_input_token_limit,
    compaction_trigger_ratio=config.context_compaction_trigger_ratio,
    compaction_target_ratio=config.context_compaction_target_ratio,
    token_counter=(
        context_token_counter.count_messages
        if context_token_counter is not None
        else None
    ),
    visual_history_probe=tool_resources.visual_history_probe,
    live_view_resolver=tool_resources.live_view_resolver,
    skill_catalog=skill_catalog,
)
planning_graph = build_planning_graph(
    model, fast_agent, tools=tools, skill_catalog=skill_catalog,
    budget_policy=planning_budget,
)
```

删除 `model_call_limit=`/`tool_call_limit=` 重复传参。policy 是可信进程 composition 值，不进入公开 Graph input。

- [ ] **Step 4: 更新核心不变量文字与测试**

`LOOP-001` 增加：planner/worker 预期失败通过原生 recovery edge 有界 replan、成功 worker 不重放、恢复耗尽进入标准消息终态。`CTX-001` 把“官方 limit middleware”改为“phase-aware limit middleware”，同时保留官方 summarization/Tool retry/HITL 和 checkpoint resume 契约。

- [ ] **Step 5: 同步三个 authority owner**

- `runtime-event-stream-architecture.md`：更新 planning topology、generation/recovery/frozen result、controlled finalizer、recovery custom event。
- `context_engineering_status.md`：更新 phase budget、worker structured response、recovery context 的 prompt-safe 边界。
- `tool-calling-architecture.md`：说明预算 middleware 只阻止超限调用并生成标准 error ToolMessage；实际 Tool 仍由标准 ToolNode/ToolRuntime 执行，Tool retry/HITL 不变。

不得修改 `AGENTS.md` 或把本设计复制进 README。

- [ ] **Step 6: 运行 Task 8 定向验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/contract/test_tool_contract.py
```

Expected: all pass.

- [ ] **Step 7: 运行 authority validator**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: exit 0; no ownership/manifest drift.

- [ ] **Step 8: 提交 Task 8**

```bash
git add src/assistant_agent/agent_server/services.py \
  tests/core/INVARIANTS.md \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py \
  docs/runtime-event-stream-architecture.md \
  docs/context_engineering_status.md \
  docs/tool-calling-architecture.md
git commit -m "docs: define planning recovery runtime contract"
```

---

### Task 9: 全量定向回归与交付审计

**Files:**
- Verify only; fix only files already owned by Tasks 1-8 if a regression is found.

**Interfaces:**
- Consumes: completed Tasks 1-8。
- Produces: mock/offline verification evidence、clean task diff audit、final testing report。

- [ ] **Step 1: 运行新 feature 的全部临时 TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/planning-recovery-routing
```

Expected: all pass. 该目录保持可由用户手动整目录删除，不自动晋升 core。

- [ ] **Step 2: 运行既有 planner 临时回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-high-agency-planner
```

Expected: all pass.

- [ ] **Step 3: 运行受影响核心与 Tool contract**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/contract/test_tool_contract.py
```

Expected: all pass within normal offline runtime.

- [ ] **Step 4: 编译修改的 Python package**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/native_agent \
  src/assistant_agent/agent_server
```

Expected: exit 0.

- [ ] **Step 5: 复跑文档 authority validator**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: exit 0.

- [ ] **Step 6: 审计遗留 literal、危险异常重试和 task diff**

```bash
rg -n "native_plan_v1|RetryPolicy\(max_attempts=2\)|ToolCallLimitExceededError" \
  src/assistant_agent/native_agent \
  tests/core \
  tests/tdd/native-high-agency-planner \
  tests/tdd/planning-recovery-routing
git diff --check
git status --short
```

Expected:

- no `native_plan_v1`;
- planner 不再有未分类 `RetryPolicy(max_attempts=2)`；
- 生产 planning path 不依赖 `ToolCallLimitExceededError` 表示预算状态；
- `git diff --check` exit 0；
- status 中仅包含本任务文件和进入任务前已有的用户改动。

- [ ] **Step 7: 若 Step 1-6 暴露回归，按单一根因修正并复跑最小失败集合**

不得叠加多个猜测性修复。修正后先复跑直接失败文件，再复跑 Step 1-5；所有命令通过前不能声称完成。

- [ ] **Step 8: 提交验证修正（仅在确有修正时）**

```bash
git add src/assistant_agent/native_agent/models.py \
  src/assistant_agent/native_agent/state.py \
  src/assistant_agent/native_agent/planning_budget.py \
  src/assistant_agent/native_agent/planning_recovery.py \
  src/assistant_agent/native_agent/planning_phase.py \
  src/assistant_agent/native_agent/fast_agent.py \
  src/assistant_agent/native_agent/planning_graph.py \
  src/assistant_agent/agent_server/services.py \
  tests/tdd/planning-recovery-routing \
  tests/tdd/native-high-agency-planner \
  tests/core/INVARIANTS.md \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py \
  docs/runtime-event-stream-architecture.md \
  docs/context_engineering_status.md \
  docs/tool-calling-architecture.md
git commit -m "fix: close planning recovery regressions"
```

如果没有修正，不创建空 commit。

- [ ] **Step 9: 按项目格式报告**

```text
Core invariant: LOOP-001 and CTX-001 changed because planning failures now use
native bounded recovery edges and phase-scoped budgets.
Tests: added tests/tdd/planning-recovery-routing for temporary RED/GREEN; user
may delete the directory manually. Updated the existing LOOP-001/CTX-001 core tests.
Commands: <列出实际运行命令与结果>
Real Provider: not invoked.
```
