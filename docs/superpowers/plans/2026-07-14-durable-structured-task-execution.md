# Durable Structured Task Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in durable slow path in which the existing provider-native assistant loop submits, resumes, revises, and completes validated structured tasks without adding latency to ordinary requests.

**Architecture:** Add a SQLite-backed `DurableTaskService` and a governed terminal `task_plan_submit` tool. The foreground Gateway run ends after atomic task acceptance; a lease-based worker invokes the same `AgentGraphRuntime` for one action per quantum using a prompt-safe task snapshot. Every business tool remains behind `ActionValidator -> ToolExecutor -> ToolRegistry`.

**Tech Stack:** Python 3.11+, Pydantic v2, stdlib `sqlite3`, FastAPI lifespan/routes, existing LangGraph/provider-native runtime, pytest.

## Global Constraints

- Source of truth: `docs/superpowers/specs/2026-07-14-durable-structured-task-execution-design.md`.
- Do not install dependencies; use stdlib `sqlite3` and existing packages.
- Default flags are closed: `MULTIMODAL_AGENT_DURABLE_TASKS_ENABLED=false` and `MULTIMODAL_AGENT_DURABLE_TASK_WORKER_ENABLED=false`.
- Default database path is `.local/tasks/durable_tasks.sqlite3`; lease is 30 seconds; poll interval is 1.0 second.
- A normal `auto` request adds no provider call beyond the existing ReAct call and creates no task row.
- `task_plan_submit` and every business tool call execute through validator, executor, registry, policy, budget, and audit boundaries.
- Public metadata cannot set task IDs, lease tokens, expected versions, durable confirmations, or durable idempotency keys.
- Tests and evals use mock/scripted/local paths. Real LLM validation is explicit `provider_smoke` or `pilot` work only.
- Keep `execution_strategy=plan_and_solve` as a compatibility input; explicit `task_execution_mode` wins.
- Do not commit the design document alone. Stage the design, code, tests, plan, and authority-doc updates together at an implementation checkpoint.

---

## File and Responsibility Map

**Create**

- `src/assistant_agent/schemas/durable_tasks.py`: durable task, plan version, step run, event, confirmation, snapshot, lease, and API contracts.
- `src/assistant_agent/services/durable_tasks/store.py`: `TaskStore` protocol and deterministic in-memory implementation.
- `src/assistant_agent/services/durable_tasks/sqlite_store.py`: transactional SQLite aggregate/event persistence and lease compare-and-set.
- `src/assistant_agent/services/durable_tasks/service.py`: identity-scoped state machine, plan validation, confirmations, input, cancellation, checkpoints, and snapshots.
- `src/assistant_agent/services/durable_tasks/worker.py`: one-quantum lease worker and cooperative background loop.
- `src/assistant_agent/tools/task_plan_tool.py`: governed model-facing plan submission/revision tool.
- `src/assistant_agent/api/routes_tasks.py`: task query, event replay, confirmation, input, and cancel routes.
- `tests/test_durable_task_schemas.py`
- `tests/test_durable_task_store.py`
- `tests/test_durable_task_service.py`
- `tests/test_task_plan_tool.py`
- `tests/test_durable_task_native_runtime.py`
- `tests/test_durable_task_context.py`
- `tests/test_durable_task_worker.py`
- `tests/test_durable_task_api.py`
- `tests/test_durable_task_lifespan.py`

**Modify**

- `src/assistant_agent/schemas/requests.py`: normalized `task_execution_mode`.
- `src/assistant_agent/config.py`: closed-by-default durable task settings.
- `src/assistant_agent/tools/registry.py`: conditional tool registration and policy metadata.
- `src/assistant_agent/agent/action_validator.py`: durable-mode and bound-ready-step gates.
- `src/assistant_agent/agent/tool_executor.py`: trusted task binding, confirmation, and idempotency injection.
- `src/assistant_agent/agent/runtime.py`: task service injection, standalone submission batch rule, terminal acceptance, and quantum yield.
- `src/assistant_agent/schemas/context.py`: durable task snapshot section and accounting.
- `src/assistant_agent/services/context/builder.py`: snapshot extraction and budget accounting.
- `src/assistant_agent/services/context/renderer.py`: prompt-safe durable task rendering.
- `src/assistant_agent/services/context/report.py`: redacted `durable_task_state` report section.
- `src/assistant_agent/services/assistant_run_service.py`: task service/runtime injection and trusted request preparation.
- `src/assistant_agent/api/routes_agent.py`: reserved metadata stripping and effective mode propagation.
- `src/assistant_agent/api/app.py`: route registration and worker lifespan.
- `docs/tool-calling-architecture.md`, `docs/CONTEXT_ENGINEERING_STATUS.md`, `docs/gateway-architecture.md`: authority updates.
- `tests/evals/eval_cases.json`, `tests/test_eval_suite_layering.py`: offline durable-task eval coverage.

---

### Task 1: Request, Configuration, and Domain Contracts

**Files:**
- Create: `src/assistant_agent/schemas/durable_tasks.py`
- Modify: `src/assistant_agent/schemas/requests.py`
- Modify: `src/assistant_agent/config.py`
- Create: `tests/test_durable_task_schemas.py`
- Modify: `tests/test_provider_config_validation.py`

**Interfaces:**
- Produces: `TaskExecutionMode`, `TaskStatus`, `TaskStepStatus`, `TaskRecord`, `TaskPlanVersion`, `TaskStepRun`, `TaskEvent`, `TaskConfirmation`, `TaskArtifactRef`, `DurableTaskBundle`, `DurableTaskSnapshot`, `DurableTaskLease`, `TrustedTaskBinding`, `TaskCheckpoint`, and `normalize_task_execution_mode()`.
- Produces config fields: `durable_tasks_enabled`, `durable_task_path`, `durable_task_worker_enabled`, `durable_task_lease_seconds`, `durable_task_poll_seconds`.

- [ ] **Step 1: Write failing normalization and domain-invariant tests**

```python
def test_explicit_task_mode_wins_over_legacy_strategy() -> None:
    request = UserRequest(
        user_id="u1", session_id="s1", text="x",
        execution_strategy="plan_and_solve", task_execution_mode="foreground",
    )
    assert request.task_execution_mode == "foreground"


def test_legacy_plan_and_solve_maps_only_when_feature_is_enabled() -> None:
    request = UserRequest(
        user_id="u1", session_id="s1", text="x",
        execution_strategy="plan_and_solve",
    )
    assert normalize_task_execution_mode(request, durable_tasks_enabled=True).task_execution_mode == "durable"
    assert normalize_task_execution_mode(request, durable_tasks_enabled=False).task_execution_mode == "auto"


def test_task_bundle_rejects_current_plan_version_not_present() -> None:
    with pytest.raises(ValidationError, match="current_plan_version"):
        DurableTaskBundle(
            task=TaskRecord(
                task_id="task_1", user_id="u1", session_id="s1",
                objective="research", status="queued", current_plan_version=2,
            ),
            plans=[],
        )
```

- [ ] **Step 2: Run the focused tests and confirm the missing-contract failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_schemas.py tests/test_provider_config_validation.py -q
```

Expected: collection fails because `assistant_agent.schemas.durable_tasks` and the new config/request fields do not exist.

- [ ] **Step 3: Add concrete request normalization and durable models**

Preserve field-presence information and normalize with configuration before the
runtime starts:

```python
TaskExecutionMode = Literal["auto", "durable", "foreground"]


class UserRequest(BaseModel):
    # existing fields remain unchanged
    task_execution_mode: TaskExecutionMode = "auto"


def normalize_task_execution_mode(
    request: UserRequest, *, durable_tasks_enabled: bool
) -> UserRequest:
    explicit = "task_execution_mode" in request.model_fields_set
    effective = request.task_execution_mode
    if not explicit and durable_tasks_enabled and request.execution_strategy == "plan_and_solve":
        effective = "durable"
    return request.model_copy(update={"task_execution_mode": effective})
```

Define the durable aggregate with these canonical field names; later tasks must
not introduce aliases for the same facts:

```python
TaskStatus = Literal[
    "queued", "running", "waiting_confirmation", "waiting_input",
    "replanning", "outcome_unknown", "completed", "failed", "cancelled",
]
TaskStepStatus = Literal[
    "pending", "ready", "leased", "running", "succeeded", "failed",
    "waiting_confirmation", "waiting_input", "skipped", "cancelled",
    "outcome_unknown",
]


class TaskRecord(BaseModel):
    task_id: str
    user_id: str
    session_id: str
    ingress_run_id: str
    objective: str
    active_constraints: list[str] = Field(default_factory=list)
    status: TaskStatus = "queued"
    current_plan_version: int = 1
    version: int = 1
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    remaining_budget: dict[str, int | float] = Field(default_factory=dict)
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    terminal_at: datetime | None = None


class TaskPlanVersion(BaseModel):
    task_id: str
    plan_version: int
    plan: TaskPlan
    revision_reason: str
    inherited_step_ids: list[str] = Field(default_factory=list)
    replaced_step_ids: list[str] = Field(default_factory=list)
    invalidated_confirmation_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class TaskStepRun(BaseModel):
    task_id: str
    plan_version: int
    step_id: str
    status: TaskStepStatus = "pending"
    attempt: int = 0
    idempotency_key: str
    tool_name: str | None = None
    tool_input_digest: str | None = None
    output_ref: str | None = None
    summary: str = ""
    error_code: str | None = None
    error_message: str | None = None


class TaskEvent(BaseModel):
    task_id: str
    cursor: int = 0
    event_type: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class TaskConfirmation(BaseModel):
    confirmation_id: str
    task_id: str
    plan_version: int
    step_id: str
    tool_name: str
    input_digest: str
    binding_digest: str
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    expires_at: datetime
    decided_at: datetime | None = None


class TaskArtifactRef(BaseModel):
    artifact_ref: str
    kind: str
    summary: str
    producer_plan_version: int
    producer_step_id: str
    trust: str = "tool_result"


class DurableTaskBundle(BaseModel):
    task: TaskRecord
    plans: list[TaskPlanVersion]
    step_runs: list[TaskStepRun] = Field(default_factory=list)
    confirmations: list[TaskConfirmation] = Field(default_factory=list)
    artifacts: list[TaskArtifactRef] = Field(default_factory=list)


class DurableTaskSnapshot(BaseModel):
    task_id: str
    objective: str
    active_constraints: list[str]
    task_status: TaskStatus
    plan_version: int
    plan: TaskPlan
    ready_step_ids: list[str]
    completed_steps: list[dict[str, str]]
    artifact_refs: list[TaskArtifactRef]
    wait: dict[str, Any] | None = None
    remaining_budget: dict[str, int | float]


class TrustedTaskBinding(BaseModel):
    task_id: str
    task_version: int
    plan_version: int
    lease_owner: str
    lease_token: str
    ready_step_ids: list[str]
    verified_confirmation_id: str | None = None


class DurableTaskLease(BaseModel):
    task_id: str
    task_version: int
    worker_id: str
    lease_token: str
    expires_at: datetime


class TaskCheckpoint(BaseModel):
    kind: Literal[
        "tool_succeeded", "tool_failed", "waiting_confirmation",
        "waiting_input", "plan_revised", "completed", "failed",
        "cancelled", "outcome_unknown",
    ]
    step_id: str | None = None
    output_ref: str | None = None
    summary: str = ""
    error_code: str | None = None
    error_message: str | None = None


```

Add an after-validator enforcing that the current plan exists, plan versions are
unique, step runs reference an existing plan/step, and terminal tasks have
`terminal_at`.

- [ ] **Step 4: Add closed-by-default configuration parsing**

Add these frozen dataclass fields and `from_env` mappings:

```python
durable_tasks_enabled: bool = False
durable_task_path: str = ".local/tasks/durable_tasks.sqlite3"
durable_task_worker_enabled: bool = False
durable_task_lease_seconds: int = 30
durable_task_poll_seconds: float = 1.0
```

Parse with existing `_bool_env`, `_int_env`, and `_float_env`; clamp lease to at least 5 seconds and poll interval to at least 0.1 seconds in the parser helpers or validation path.

- [ ] **Step 5: Run tests and commit the contract slice**

Run the Step 2 command. Expected: all selected tests pass.

```bash
git add src/assistant_agent/schemas/durable_tasks.py src/assistant_agent/schemas/requests.py src/assistant_agent/config.py tests/test_durable_task_schemas.py tests/test_provider_config_validation.py docs/superpowers/specs/2026-07-14-durable-structured-task-execution-design.md docs/superpowers/plans/2026-07-14-durable-structured-task-execution.md
git commit -m "feat(tasks): define durable task contracts"
```

---

### Task 2: In-memory and SQLite Task Stores

**Files:**
- Create: `src/assistant_agent/services/durable_tasks/__init__.py`
- Create: `src/assistant_agent/services/durable_tasks/store.py`
- Create: `src/assistant_agent/services/durable_tasks/sqlite_store.py`
- Create: `tests/test_durable_task_store.py`

**Interfaces:**
- Consumes: `DurableTaskBundle`, `TaskEvent`, `DurableTaskLease`.
- Produces:

```python
class TaskStore(Protocol):
    def create(self, bundle: DurableTaskBundle, events: list[TaskEvent]) -> DurableTaskBundle: ...
    def load(self, task_id: str) -> DurableTaskBundle | None: ...
    def save(self, bundle: DurableTaskBundle, *, expected_version: int, events: list[TaskEvent]) -> DurableTaskBundle: ...
    def list_events(self, task_id: str, *, after: int = 0, limit: int = 100) -> list[TaskEvent]: ...
    def claim_next(self, *, worker_id: str, now: datetime, lease_seconds: int) -> DurableTaskLease | None: ...
    def release(self, lease: DurableTaskLease, *, expected_version: int) -> None: ...
    def close(self) -> None: ...
```

- [ ] **Step 1: Write store contract tests**

Parameterize the same tests over `InMemoryTaskStore()` and `SQLiteTaskStore(tmp_path / "tasks.sqlite3")`. Assert create/load deep copies, duplicate task rejection, optimistic-version conflict, monotonic cursors, event replay after cursor, restart load, one-winner lease claims, and stale lease rejection.

```python
@pytest.mark.parametrize("factory", [memory_store, sqlite_store])
def test_save_is_atomic_and_uses_optimistic_version(factory) -> None:
    store = factory()
    created = store.create(bundle(), [event("task.accepted")])
    changed = created.model_copy(deep=True)
    changed.task.status = "running"
    saved = store.save(changed, expected_version=created.task.version, events=[event("task.started")])
    assert saved.task.version == created.task.version + 1
    with pytest.raises(TaskVersionConflict):
        store.save(changed, expected_version=created.task.version, events=[])
```

- [ ] **Step 2: Run and confirm missing store failures**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_store.py -q
```

Expected: collection fails on missing store modules.

- [ ] **Step 3: Implement the protocol and in-memory copy-on-read store**

Use an `RLock`, `model_copy(deep=True)`, a per-task event list, and a compare-and-set check. `save` assigns event cursors starting at the last committed cursor plus one and increments `bundle.task.version` exactly once per transaction.

- [ ] **Step 4: Implement SQLite aggregate/event transactions**

Create two tables and indexes in `_initialize()`:

```sql
CREATE TABLE IF NOT EXISTS durable_tasks (
  task_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  lease_owner TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  bundle_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS durable_task_events (
  task_id TEXT NOT NULL,
  cursor INTEGER NOT NULL,
  event_json TEXT NOT NULL,
  PRIMARY KEY (task_id, cursor)
);
CREATE INDEX IF NOT EXISTS idx_durable_tasks_claim
ON durable_tasks(status, lease_expires_at, updated_at);
```

Use `BEGIN IMMEDIATE`; write the aggregate and events in the same transaction; serialize with `model_dump_json()`; rollback on conflicts. `claim_next` selects only `queued`, `running`, or `replanning` tasks whose lease is absent/expired and updates one row using its current version.

- [ ] **Step 5: Run store tests twice, including restart behavior, and commit**

Run Step 2 twice. Expected: both runs pass and SQLite restart tests reload the same bundle/events.

```bash
git add src/assistant_agent/services/durable_tasks tests/test_durable_task_store.py
git commit -m "feat(tasks): add durable task stores"
```

---

### Task 3: Durable Task State Machine Service

**Files:**
- Create: `src/assistant_agent/services/durable_tasks/service.py`
- Create: `tests/test_durable_task_service.py`
- Modify: `src/assistant_agent/agent/plan_validator.py`

**Interfaces:**
- Consumes: `TaskStore`, `ToolRegistry`, `PlanValidator`, `RequestIdentity`.
- Produces `DurableTaskService` methods:

```python
def submit_plan(*, identity: RequestIdentity, ingress_run_id: str, plan: TaskPlan, revision_reason: str) -> DurableTaskBundle: ...
def revise_plan(*, binding: TrustedTaskBinding, plan: TaskPlan, revision_reason: str) -> DurableTaskBundle: ...
def get_task(*, identity: RequestIdentity, task_id: str) -> DurableTaskBundle: ...
def list_events(*, identity: RequestIdentity, task_id: str, after: int, limit: int) -> list[TaskEvent]: ...
def confirm(*, identity: RequestIdentity, task_id: str, confirmation_id: str, approved: bool) -> DurableTaskBundle: ...
def provide_input(*, identity: RequestIdentity, task_id: str, text: str) -> DurableTaskBundle: ...
def cancel(*, identity: RequestIdentity, task_id: str, reason: str) -> DurableTaskBundle: ...
def claim_next(*, worker_id: str, now: datetime) -> DurableTaskLease | None: ...
def snapshot_for_lease(lease: DurableTaskLease) -> DurableTaskSnapshot: ...
def checkpoint(lease: DurableTaskLease, transition: TaskCheckpoint) -> DurableTaskBundle: ...
```

- [ ] **Step 1: Write failing state-machine tests**

Cover create, identity isolation, revision limit, completed-step inheritance, invalid dependency/tool rejection, ready-step calculation, event ordering, confirmation digest invalidation, input resume, terminal-state immutability, cancel idempotency, stale lease, and one visible terminal event.

- [ ] **Step 2: Run the service tests and confirm missing service failures**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_service.py -q
```

- [ ] **Step 3: Implement plan submission/revision and identity gates**

Use `hmac.compare_digest` for stored SHA-256 confirmation digests, `secrets.token_urlsafe()` for lease/confirmation IDs, and explicit allowed-transition sets. Raise typed `TaskNotFound`, `TaskAccessDenied`, `TaskConflict`, and `TaskTransitionRejected` exceptions carrying stable codes.

- [ ] **Step 4: Implement leases, snapshots, checkpoints, and outcome recovery**

`checkpoint` must verify task version, lease token, and lease owner. For an expired `running` attempt, inspect `ToolPolicyInterpreter().view_for_spec()`; retry read-only policies within budget and move possible writes to `outcome_unknown`. Never infer safety from model text or plan reason.

- [ ] **Step 5: Run service + store + plan validator tests and commit**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_service.py tests/test_durable_task_store.py tests/test_plan_mode_react.py -q
```

Expected: all selected tests pass.

```bash
git add src/assistant_agent/services/durable_tasks/service.py src/assistant_agent/agent/plan_validator.py tests/test_durable_task_service.py
git commit -m "feat(tasks): add durable task state machine"
```

---

### Task 4: Governed Plan Submission Tool

**Files:**
- Create: `src/assistant_agent/tools/task_plan_tool.py`
- Modify: `src/assistant_agent/tools/registry.py`
- Modify: `src/assistant_agent/agent/action_validator.py`
- Modify: `src/assistant_agent/agent/tool_executor.py`
- Create: `tests/test_task_plan_tool.py`
- Modify: `tests/test_tool_policy_parity_integration.py`
- Modify: `tests/unit/test_native_tool_call_schema.py`

**Interfaces:**
- Consumes: `DurableTaskService`, `TaskPlan`, trusted `ToolContext.metadata["durable_task_binding"]`.
- Produces: `TaskPlanSubmitInput(plan: TaskPlan, revision_reason: str)` and `TaskPlanSubmitTool` returning `ToolResult.data["task"]`.

- [ ] **Step 1: Write failing tool and governance tests**

Assert the schema hides identity/task/version fields and preserves nested
`plan.steps[].depends_on`, `required_inputs`, and `tool_name` JSON Schema;
disabled registries omit the tool; enabled registries expose a terminal
local-write tool; ingress creates v1; trusted resume revises; forged request
metadata cannot revise; `durable` blocks business tools before task acceptance;
`foreground` blocks plan submission; ready-step validation rejects wrong
tools/dependencies.

- [ ] **Step 2: Run the focused tests and confirm failures**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_task_plan_tool.py tests/test_tool_policy_parity_integration.py -q
```

- [ ] **Step 3: Add the thin tool and conditional registry wiring**

The tool implementation must only validate its Pydantic input, obtain the injected `durable_task_service` and optional trusted binding from `ToolContext`, call `submit_plan` or `revise_plan`, and wrap the prompt-safe result:

```python
return ToolResult(
    tool_name=self.name,
    success=True,
    data={"task": task_submission_payload(bundle)},
    model_observation={"task": task_submission_payload(bundle)},
    trace_summary={"task_id": bundle.task.task_id, "status": bundle.task.status},
    output_ref=f"task://{bundle.task.task_id}",
)
```

Register only when `config.durable_tasks_enabled` and a service dependency are both present. Declare `dependency_mode="terminal"`, `resource_writes=["durable_task"]`, local-write policy, and no user confirmation for task-record creation itself.

Update registry schema construction to retain Pydantic's complete top-level
object schema, including nested `properties` and `items`, while removing
runtime-owned identity fields before it reaches `ToolSpec`. Resolve local
`#/$defs/...` references into inline schemas and remove `$defs` from the final
provider schema so OpenAI-compatible adapters do not depend on provider-specific
reference support. Keep `additionalProperties=False` on every object node.
Verify existing flat tool schemas are byte-for-byte equivalent after
`tool_spec_to_json_schema()` normalization.

- [ ] **Step 4: Add local enforcement and trusted execution injection**

`ActionValidator` checks effective mode and bound task readiness. `ToolExecutor` derives stable idempotency as `sha256(task_id, plan_version, step_id, tool_name)` and injects it before `evaluate_tool_risk`; it injects a one-quantum confirmation only after the task service supplied a verified grant. Public request metadata never sets either value.

- [ ] **Step 5: Run tool, validator, executor, and registry tests and commit**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_task_plan_tool.py tests/test_tool_policy_parity_integration.py tests/test_tool_risk_gate.py tests/unit/test_tool_spec_adapters.py tests/unit/test_native_tool_call_schema.py -q
```

Expected: all selected tests pass.

```bash
git add src/assistant_agent/tools/task_plan_tool.py src/assistant_agent/tools/registry.py src/assistant_agent/agent/action_validator.py src/assistant_agent/agent/tool_executor.py tests/test_task_plan_tool.py tests/test_tool_policy_parity_integration.py tests/unit/test_native_tool_call_schema.py
git commit -m "feat(tasks): govern structured plan submission"
```

---

### Task 5: Provider-native Activation and Terminal Handoff

**Files:**
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `src/assistant_agent/services/assistant_run_service.py`
- Modify: `src/assistant_agent/api/routes_agent.py`
- Create: `tests/test_durable_task_native_runtime.py`
- Modify: `tests/test_native_tool_call_handoff.py`
- Modify: `tests/test_api_agent_graph_runtime.py`

**Interfaces:**
- Consumes: effective task mode, `TaskPlanSubmitTool`, `DurableTaskService`.
- Produces: terminal accepted response in `AgentResponse.data["task"]`; standalone-batch validation; no second LLM call after submission.

- [ ] **Step 1: Write scripted-native failing tests**

Use a non-mock adapter returning native calls. Assert:

```python
assert simple_adapter.calls == 1
assert store.load_count == 0
assert durable_state.response.data["task"]["submission_status"] == "accepted"
assert submit_adapter.calls == 1
assert durable_state.tool_calls[0].tool_name == "task_plan_submit"
```

Also assert explicit durable + disabled feature fails before adapter invocation; a mixed `task_plan_submit`/business-tool batch executes neither; direct business call in durable mode returns a recovery observation; foreground never exposes the plan tool.

- [ ] **Step 2: Run focused native tests and confirm failures**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_native_runtime.py tests/test_native_tool_call_handoff.py -q
```

- [ ] **Step 3: Inject one task service through runtime construction**

Extend `create_runtime(..., durable_task_service=None)` and `AgentGraphRuntime.__init__(..., durable_task_service=None)`. When enabled and no explicit service is supplied, construct `SQLiteTaskStore(config.durable_task_path)` and `DurableTaskService(store, registry)`, then rebuild/register the task tool without introducing a module-global store.
Expose the resolved instance as `runtime.durable_task_service`; it is the only
service used by that runtime's plan tool, API queries, and worker.

- [ ] **Step 4: Enforce native-call and terminal response rules**

Before scheduling, reject any batch in which `task_plan_submit` is not the sole call. After a successful plan-tool result, call a helper that sets:

```python
state.set_response(AgentResponse(
    message="任务已创建，我会按计划继续处理。",
    data={"task": tool_result.data["task"], "native_runtime": True},
    output_refs=[tool_result.output_ref] if tool_result.output_ref else [],
))
```

Return immediately without another provider call. Strip reserved keys such as `durable_task_binding`, `durable_confirmation`, `durable_idempotency_key`, and `worker_lease` in `_public_request_metadata`.

- [ ] **Step 5: Run native/API regression tests and commit**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_native_runtime.py tests/test_native_tool_call_handoff.py tests/test_api_agent_graph_runtime.py tests/test_phase7c_web_productization.py -q
```

Expected: all selected tests pass; old `plan_and_solve` behavior is unchanged
when the feature is disabled, maps to durable when enabled, and explicit
`task_execution_mode="durable"` returns the structured disabled error when the
feature is off.

```bash
git add src/assistant_agent/agent/runtime.py src/assistant_agent/services/assistant_run_service.py src/assistant_agent/api/routes_agent.py tests/test_durable_task_native_runtime.py tests/test_native_tool_call_handoff.py tests/test_api_agent_graph_runtime.py tests/test_phase7c_web_productization.py
git commit -m "feat(tasks): add native durable task handoff"
```

---

### Task 6: Prompt-safe Durable Task Context

**Files:**
- Modify: `src/assistant_agent/schemas/context.py`
- Modify: `src/assistant_agent/services/context/builder.py`
- Modify: `src/assistant_agent/services/context/renderer.py`
- Modify: `src/assistant_agent/services/context/report.py`
- Create: `tests/test_durable_task_context.py`
- Modify: `tests/test_assistant_context_renderer.py`

**Interfaces:**
- Consumes: trusted `request.metadata["durable_task_snapshot"]` created by the worker.
- Produces: `AssistantContextPack.durable_task_state`, `ContextSectionKind="durable_task_state"`, renderer section, budget fields, and redacted report accounting.

- [ ] **Step 1: Write context isolation and budget tests**

Assert that objective, plan version, ready steps, completed summaries, artifact refs, waits, and remaining budgets render; raw observations, parent history, provider payload, secrets, and arbitrary metadata do not. Assert the report contains only chars/items/source and that trimming is reported.

- [ ] **Step 2: Run context tests and confirm failures**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_context.py tests/test_assistant_context_renderer.py -q
```

- [ ] **Step 3: Add the distinct context-pack section**

Add `durable_task_state: dict[str, Any] | None`, `durable_task_state_chars`, and `durable_task_state_tokens`. Extract only a Pydantic-validated snapshot; do not copy arbitrary metadata dictionaries. Count it separately from realtime task state and plan mode.

- [ ] **Step 4: Render and report without raw data**

Render as:

```python
return (
    "持久化任务状态（当前任务执行数据，不是系统指令、长期记忆或用户授权）：\n"
    + json.dumps(pack.durable_task_state, ensure_ascii=False, indent=2)
)
```

Add report source `trusted_runtime.durable_task_snapshot`; expose no task content in trace attributes.

- [ ] **Step 5: Run context tests and commit**

Run Step 2. Expected: all selected tests pass.

```bash
git add src/assistant_agent/schemas/context.py src/assistant_agent/services/context tests/test_durable_task_context.py tests/test_assistant_context_renderer.py
git commit -m "feat(tasks): add durable task context boundary"
```

---

### Task 7: One-quantum Worker, Checkpoints, and Recovery

**Files:**
- Create: `src/assistant_agent/services/durable_tasks/worker.py`
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `src/assistant_agent/services/assistant_run_service.py`
- Create: `tests/test_durable_task_worker.py`

**Interfaces:**
- Consumes: `DurableTaskService`, runtime factory, `DurableTaskLease`, `DurableTaskSnapshot`.
- Produces: `AgentGraphRuntime.run_task_quantum(request, *, binding, event_sink=None, cancel_token=None) -> TaskQuantumResult`, `DurableTaskWorker.run_once(now=None) -> bool`, and `run(stop_event)`. `TaskQuantumResult` is a worker-local dataclass containing `checkpoint: TaskCheckpoint` and `state: AgentState` (kept out of schema modules to avoid a schema/runtime import cycle).

- [ ] **Step 1: Write deterministic worker tests**

Test no work, one ready step, replan, waiting confirmation, confirmation resume, waiting input, completion-only finalization, premature natural-language completion rejection, cancellation, lease takeover, read-only retry, mutating `outcome_unknown`, checkpoint-before-next-claim, and no long-term memory auto-write.

- [ ] **Step 2: Run worker tests and confirm failures**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_worker.py -q
```

- [ ] **Step 3: Build trusted resume requests**

Construct `UserRequest(task_execution_mode="durable")` using the task's bound
identity, objective, and only these trusted keys: validated snapshot, ready tool
names, binding with lease token/versions, and verified confirmation if present.
Call `run_task_quantum()` directly; the method owns the hard action limit of one
instead of trusting request metadata. Disable conversation-history injection for
worker calls and retain normal memory read/write policy.

- [ ] **Step 4: Add runtime quantum yield and checkpoint mapping**

Refactor the native loop so `run_task_quantum()` reuses the same prompt compiler,
native-call normalization, validator, executor, observation, trace, and budget
functions as `run_state()`. After one business tool result it returns
`TaskQuantumResult` instead of requesting another LLM turn. Map
confirmation-required results to `waiting_confirmation`; successful results to
step completion plus prompt-safe artifact refs; retryable errors to bounded
retry/replan; uncertain mutating interruption to `outcome_unknown`; a valid
standalone `task_plan_submit` to revision; and natural content to completion
only when no required steps remain. Pass the worker cancellation token through
to `ToolExecutor`; cancellation checkpoints the durable task and suppresses late
results from changing its visible terminal state.

- [ ] **Step 5: Add cooperative worker loop and run failure injection tests**

The loop repeatedly calls `run_once()` and waits with `stop_event.wait(poll_seconds)`; it never uses an uninterruptible sleep. Inject crashes before tool call, after tool success/before checkpoint, and after checkpoint/before next schedule.

Run Step 2. Expected: all worker and failure-injection tests pass.

- [ ] **Step 6: Commit the worker slice**

```bash
git add src/assistant_agent/services/durable_tasks/worker.py src/assistant_agent/agent/runtime.py src/assistant_agent/services/assistant_run_service.py tests/test_durable_task_worker.py
git commit -m "feat(tasks): execute durable task quanta"
```

---

### Task 8: Identity-scoped Task HTTP API and Gateway Acceptance

**Files:**
- Create: `src/assistant_agent/api/routes_tasks.py`
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/schemas/api.py`
- Modify: `src/assistant_agent/api/routes_agent.py`
- Create: `tests/test_durable_task_api.py`
- Modify: `tests/test_gateway_api.py`

**Interfaces:**
- Consumes: `get_agent_runtime().durable_task_service`, existing `AuthContext`/identity policy.
- Produces: `GET /tasks/{task_id}`, `GET /tasks/{task_id}/events`, `POST /tasks/{task_id}/confirmations`, `POST /tasks/{task_id}/input`, `POST /tasks/{task_id}/cancel`.

- [ ] **Step 1: Write API identity and lifecycle tests**

Assert owner access, cross-user 404/403 behavior matching existing identity policy, cursor replay, input length validation, confirmation digest mismatch conflict, idempotent cancellation, disabled-feature response, no secret/raw payload fields, and `/agent/run` returning top-level `status="completed"` with nested `data.task.submission_status="accepted"`.

- [ ] **Step 2: Run API tests and confirm missing-route failures**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_api.py tests/test_gateway_api.py -q
```

- [ ] **Step 3: Implement route schemas and identity-scoped dependencies**

Use `get_auth_context`, `resolve_request_identity`, `enforce_identity_policy`,
and trial access consistently with `/agent/run`. A
`get_durable_task_service()` dependency obtains the already constructed service
from `get_agent_runtime()` and returns `DURABLE_TASKS_DISABLED` if it is absent.
Never accept `user_id` in confirmation/input/cancel bodies; derive it from
auth/request identity. Map typed service errors to stable `ApiError` codes:
`DURABLE_TASKS_DISABLED`, `TASK_NOT_FOUND`, `TASK_CONFLICT`,
`TASK_CONFIRMATION_INVALID`, and `TASK_TERMINAL`.

- [ ] **Step 4: Register routes and preserve Gateway run semantics**

Include the router in `create_app()`. Keep Gateway `run.end` completed after acceptance; expose the stable `task_id` in response data/output refs. Do not register the durable task as an active Gateway run and do not reuse the in-memory Gateway queue.

- [ ] **Step 5: Run API/Gateway tests and commit**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_api.py tests/test_gateway_api.py tests/test_gateway_session.py tests/test_api_agent_graph_runtime.py -q
```

Expected: all selected tests pass.

```bash
git add src/assistant_agent/api/routes_tasks.py src/assistant_agent/api/app.py src/assistant_agent/schemas/api.py src/assistant_agent/api/routes_agent.py tests/test_durable_task_api.py tests/test_gateway_api.py
git commit -m "feat(tasks): expose durable task API"
```

---

### Task 9: FastAPI Worker Lifespan and Restart Recovery

**Files:**
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/api/routes_agent.py`
- Create: `tests/test_durable_task_lifespan.py`
- Modify: `tests/test_run_server.py`

**Interfaces:**
- Consumes: shared configured task service and `DurableTaskWorker`.
- Produces: `get_durable_task_worker()`, `start_durable_task_worker()`, and `shutdown_durable_task_worker()` with deterministic test overrides. The service comes from the shared `AgentGraphRuntime`.

- [ ] **Step 1: Write lifespan tests**

Assert no DB/worker when disabled; enabled store without worker opens API only; both flags start one worker; shutdown signals and joins it before Gateway shutdown; a queued SQLite task is claimed after app restart; repeated app creation in tests does not leak a thread or global service from a different config.

- [ ] **Step 2: Run lifespan tests and confirm failures**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_lifespan.py tests/test_run_server.py -q
```

- [ ] **Step 3: Implement cooperative lifespan ownership**

Resolve the shared runtime once, take `runtime.durable_task_service`, and store
only worker/stop/task handles on `app.state`; do not construct another service
or store. Start the sync worker with
`asyncio.create_task(asyncio.to_thread(worker.run, stop_event))`; on shutdown
set the event, await the task with a bounded timeout, close the runtime-owned
SQLite store once, then call `shutdown_gateway_runtime()`.

- [ ] **Step 4: Run restart and full focused task suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_durable_task_schemas.py tests/test_durable_task_store.py tests/test_durable_task_service.py tests/test_task_plan_tool.py tests/test_durable_task_native_runtime.py tests/test_durable_task_context.py tests/test_durable_task_worker.py tests/test_durable_task_api.py tests/test_durable_task_lifespan.py -q
```

Expected: all durable-task tests pass with no thread-leak warnings.

- [ ] **Step 5: Commit lifespan integration**

```bash
git add src/assistant_agent/api/app.py src/assistant_agent/api/routes_agent.py tests/test_durable_task_lifespan.py tests/test_run_server.py
git commit -m "feat(tasks): run local durable task worker"
```

---

### Task 10: Offline Evals, Authority Documentation, and Final Verification

**Files:**
- Modify: `tests/evals/eval_cases.json`
- Modify: `tests/test_eval_suite_layering.py`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/CONTEXT_ENGINEERING_STATUS.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `README.md` only if its human navigation requires a durable-task API link.

**Interfaces:**
- Consumes: completed feature behavior.
- Produces: `durable_tasks` eval suite and current authority docs.

- [ ] **Step 1: Add offline eval cases before updating docs**

Add scripted-native cases for: simple auto direct answer, auto complex plan submission, explicit durable submission, mixed-batch rejection, valid resume step, failure replan, waiting confirmation, and final completion. Each case asserts no raw payload, stable task status, plan version, tool governance evidence, and provider-call count.

- [ ] **Step 2: Run eval layering and focused eval suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_eval_suite_layering.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite durable_tasks
```

Expected: pytest passes; eval summary reports every `durable_tasks` case passed.

- [ ] **Step 3: Update the three authority documents**

Document the exact implemented call chain, feature flags, task/Gateway run separation, context section, API endpoints, lease recovery, confirmation binding, at-least-once limitation, `outcome_unknown`, and first-version non-goals. Remove the obsolete statement that `plan_and_solve` never creates a structured native task; replace it with the feature-flagged compatibility behavior.

- [ ] **Step 4: Run environment, focused regression, and fast suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py tests/test_tool_risk_gate.py tests/test_assistant_context_renderer.py tests/test_gateway_api.py tests/test_gateway_session.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: environment check exits 0 and both pytest commands report zero failures.

- [ ] **Step 5: Run repository consistency checks**

```bash
git diff --check -- AGENTS.md docs src tests scripts
git status --short
```

Expected: no whitespace errors; status contains only intentional durable-task code, tests, docs, and any pre-existing user-owned changes identified before execution.

- [ ] **Step 6: Commit the verified feature slice**

```bash
git add docs/tool-calling-architecture.md docs/CONTEXT_ENGINEERING_STATUS.md docs/gateway-architecture.md tests/evals/eval_cases.json tests/test_eval_suite_layering.py
git commit -m "docs(tasks): document durable structured execution"
```

Do not enable a real provider or run provider smoke unless the user explicitly requests it. Record any unrun provider validation as a limitation, not as a passing result.
