# 通用长阶段任务状态底座实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不注册新 Tool、不启动 worker、不改变 `AgentGraphRuntime` 行为的前提下，建立通用 long-horizon workflow 的强类型契约、definition catalog、纯状态校验、identity-scoped service 和可跨进程重开的 SQLite Store。

**Architecture:** 新增独立的 `assistant_agent.workflows` 包。`WorkflowDefinition` 负责把通用 submission 转换为初始 DAG，`WorkflowService` 负责身份、预算、幂等和状态转换，`WorkflowStore` 负责 revision/lease/event cursor 的原子持久化。第一阶段只用测试内 `ProbeWorkflowDefinition` 验证底座，不把任何 definition 或 `workflow_submit` 暴露给现有 runtime。

**Tech Stack:** Python 3.11、Pydantic v2、标准库 `sqlite3` / `threading` / `hashlib` / `secrets`、现有 `RequestIdentity`、pytest 临时 TDD。

## Global Constraints

- 只实现路线图 P1；不得顺手实现 `workflow_submit`、LangGraph、worker、artifact workspace、Research、API/Gateway 或 legacy 删除。
- 通用模型不得出现 `research_question`、`source`、`citation`、`chapter` 等 Research 专有字段；业务输入放在受 schema 限制的 `inputs`，由具体 definition 验证。
- `WorkflowDefinition` 不调用 LLM、Provider、Tool、Store 或 Gateway，只验证 submission 并生成初始通用 work-item DAG。
- `WorkflowService` 不直接读取 SQLite 私有字段；所有持久操作经 `WorkflowStore` Protocol。
- `WorkflowStore` 的 `revision` 是业务 optimistic concurrency token；event cursor 在同一次 create/save 事务中分配。
- 重复 submission 以 `(user_id, agent_id, ingress_run_id, idempotency_key)` 唯一；相同 key + 相同 payload 返回既有 Workflow，相同 key + 不同 payload 返回结构化冲突。
- `session_id` 保留入口关联，但访问控制至少绑定 `user_id + agent_id`，不能只凭 `workflow_id` 读取。
- claim lease 只用于未来 worker 的持久语义验证；本阶段不启动后台线程。
- queued Workflow 的 cancel 立即进入 `cancelled`；持有活动 lease 的 Workflow 只设置 `cancel_requested=True` 并增加 revision，留给未来 worker 在安全边界终结。
- 默认 mock/local/offline，不读取真实 `.env`，不访问网络、不调用 Provider、不安装新依赖。
- Core invariant: unchanged。本阶段是未暴露的 feature foundation，不修改 `tests/core` 或 `tests/core/INVARIANTS.md`。
- 临时 RED/GREEN 只放在 `tests/tdd/durable-workflow-foundation/`；用户可在功能完成后手动整目录删除，执行者不得擅自删除或晋升 core。
- 保留用户工作树中的无关修改；每个 commit 只包含本计划列出的文件。

---

## File Structure

### 新增源码

- `src/assistant_agent/workflows/__init__.py`：轻量 package marker，不聚合具体 definition。
- `src/assistant_agent/workflows/models.py`：submission、budget、record、plan、work item、event、lease、bundle 契约。
- `src/assistant_agent/workflows/definitions.py`：`WorkflowDefinition` Protocol、descriptor、catalog 和错误。
- `src/assistant_agent/workflows/transitions.py`：DAG 校验、预算收紧、初始 bundle 和取消等纯函数。
- `src/assistant_agent/workflows/store.py`：Store Protocol、持久错误和 InMemory 实现。
- `src/assistant_agent/workflows/sqlite_store.py`：SQLite/WAL 实现。
- `src/assistant_agent/workflows/service.py`：身份隔离、幂等提交、查询、事件和取消 facade。

### 新增临时测试

- `tests/tdd/durable-workflow-foundation/test_models.py`
- `tests/tdd/durable-workflow-foundation/test_definitions.py`
- `tests/tdd/durable-workflow-foundation/test_transitions.py`
- `tests/tdd/durable-workflow-foundation/test_in_memory_store.py`
- `tests/tdd/durable-workflow-foundation/test_sqlite_store.py`
- `tests/tdd/durable-workflow-foundation/test_service.py`

### 更新文档

- `docs/development/2026-08-07-durable-workflow-runtime-experiment-design.md`：将阶段 0 标记为已实现，并记录实际 schema 与提案差异。
- `docs/tool-calling-architecture.md`：只增加“Workflow foundation 尚未暴露为 Tool/worker”的当前事实说明，避免把内部 package 误认为已启用能力。

---

## Task 1：定义通用 Workflow 强类型契约

**Files:**

- Create: `src/assistant_agent/workflows/__init__.py`
- Create: `src/assistant_agent/workflows/models.py`
- Create: `tests/tdd/durable-workflow-foundation/test_models.py`

**Interfaces:**

- Consumes: Pydantic v2、标准库 `datetime`、`JsonValue`。
- Produces: `WorkflowSubmission`、`WorkflowBudgetRequest`、`WorkflowBudget`、`WorkflowRecord`、`WorkflowWorkItem`、`WorkflowPlanVersion`、`WorkflowEvent`、`WorkflowLease`、`WorkflowBundle`。

- [ ] **Step 1：写 schema、引用完整性和终态不变量 RED 测试**

```python
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.workflows.models import (
    WorkflowBudget,
    WorkflowBundle,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
    WorkflowWorkItem,
)


def test_submission_is_generic_and_forbids_unknown_research_fields() -> None:
    submission = WorkflowSubmission(
        workflow_type="probe",
        objective="objective-sentinel",
        deliverables=["deliverable-sentinel"],
        constraints=[],
        inputs={"questions": ["question-sentinel"]},
        requested_budget={"model_calls": 4, "tool_calls": 8},
        durability_reasons=["multi_stage"],
        idempotency_key="submission-sentinel",
    )
    assert submission.inputs["questions"] == ["question-sentinel"]
    with pytest.raises(ValidationError):
        WorkflowSubmission.model_validate(
            {**submission.model_dump(), "research_questions": ["not-generic"]}
        )


def test_bundle_requires_current_plan_and_terminal_timestamp() -> None:
    now = datetime.now(timezone.utc)
    record = WorkflowRecord(
        workflow_id="workflow-sentinel",
        workflow_type="probe",
        definition_version="1",
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
        ingress_run_id="run-sentinel",
        idempotency_key="submission-sentinel",
        submission_digest="digest-sentinel",
        objective="objective-sentinel",
        deliverables=["deliverable-sentinel"],
        constraints=[],
        status="completed",
        phase="completed",
        current_plan_version=1,
        revision=1,
        budget=WorkflowBudget(
            model_calls_remaining=4,
            tool_calls_remaining=8,
            workflow_quanta_remaining=16,
            deadline_at=now,
        ),
    )
    plan = WorkflowPlanVersion(
        workflow_id=record.workflow_id,
        version=1,
        definition_version="1",
        revision_reason="initial",
        work_items=[
            WorkflowWorkItem(
                work_item_id="step-sentinel",
                kind="probe",
                objective="step-objective",
            )
        ],
    )
    with pytest.raises(ValidationError, match="terminal_at"):
        WorkflowBundle(workflow=record, plans=[plan])
```

- [ ] **Step 2：运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_models.py
```

Expected: collection FAIL，提示 `assistant_agent.workflows` 不存在。

- [ ] **Step 3：实现 package marker 和完整模型**

`models.py` 使用 `ConfigDict(extra="forbid")`，并定义以下稳定集合：

```python
WorkflowStatus = Literal[
    "queued", "running", "waiting_input", "blocked", "recovering",
    "completed", "failed", "cancelled",
]
WorkItemStatus = Literal[
    "pending", "ready", "running", "succeeded", "retryable_failed",
    "blocked", "superseded", "skipped", "cancelled",
]
TERMINAL_WORKFLOW_STATUSES = {"completed", "failed", "cancelled"}


class WorkflowBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_calls: int | None = Field(default=None, ge=1, le=10_000)
    tool_calls: int | None = Field(default=None, ge=1, le=100_000)
    workflow_quanta: int | None = Field(default=None, ge=1, le=1_000_000)
    deadline_seconds: int | None = Field(default=None, ge=60, le=2_592_000)


class WorkflowSeedWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed_id: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=4_000)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    input_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    acceptance_contract: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    objective: str = Field(min_length=1, max_length=10_000)
    deliverables: list[str] = Field(min_length=1, max_length=32)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    initial_workstreams: list[WorkflowSeedWorkItem] = Field(
        default_factory=list, max_length=128
    )
    requested_budget: WorkflowBudgetRequest = Field(
        default_factory=WorkflowBudgetRequest
    )
    durability_reasons: list[str] = Field(min_length=1, max_length=16)
    seed_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=240)
```

同一文件继续定义：

```python
class WorkflowBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_calls_remaining: int = Field(ge=0)
    tool_calls_remaining: int = Field(ge=0)
    workflow_quanta_remaining: int = Field(ge=0)
    deadline_at: datetime


class WorkflowWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_item_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=10_000)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    input_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    acceptance_contract: dict[str, JsonValue] = Field(default_factory=dict)
    status: WorkItemStatus = "pending"
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    active_attempt_id: str | None = Field(default=None, min_length=1)
    output_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    result_summary: str = Field(default="", max_length=4_000)
    error_code: str | None = Field(default=None, max_length=160)


class WorkflowPlanVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    definition_version: str = Field(min_length=1, max_length=80)
    revision_reason: str = Field(min_length=1, max_length=500)
    work_items: list[WorkflowWorkItem] = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(min_length=1)
    workflow_type: str = Field(min_length=1, max_length=80)
    definition_version: str = Field(min_length=1, max_length=80)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    ingress_run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=240)
    submission_digest: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=10_000)
    deliverables: list[str] = Field(min_length=1, max_length=32)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    status: WorkflowStatus = "queued"
    phase: str = Field(default="admitted", min_length=1, max_length=120)
    current_plan_version: int = Field(default=1, ge=1)
    revision: int = Field(default=1, ge=1)
    cancel_requested: bool = False
    budget: WorkflowBudget
    seed_artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    terminal_reason_code: str | None = Field(default=None, max_length=160)
    lease_owner: str | None = Field(default=None, min_length=1)
    lease_token: str | None = Field(default=None, min_length=1)
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    terminal_at: datetime | None = None


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(min_length=1)
    cursor: int = Field(default=0, ge=0)
    event_type: str = Field(min_length=1, max_length=160)
    status: str = Field(min_length=1, max_length=80)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowLease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(min_length=1)
    workflow_revision: int = Field(ge=1)
    worker_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    expires_at: datetime
```

`WorkflowBundle` 包含 `workflow: WorkflowRecord` 和非空 `plans`。`@model_validator(mode="after")` 必须验证：plan version 唯一；current version 存在；所有 plan 的 `workflow_id` 一致；每个 plan 内 `work_item_id` 唯一；终态必须有 `terminal_at`，非终态不得有 `terminal_at`；lease owner/token/expires 三者同时为空或同时存在；所有持久时间必须 timezone-aware。

- [ ] **Step 4：补齐模型边界测试并运行 GREEN**

补充断言：naive datetime、重复 plan version、重复 work item、跨 Workflow plan、半套 lease 字段、非终态带 `terminal_at`、空 deliverables、未知字段均被拒绝。

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_models.py
```

Expected: PASS。

- [ ] **Step 5：提交模型契约**

```bash
git add src/assistant_agent/workflows/__init__.py \
  src/assistant_agent/workflows/models.py \
  tests/tdd/durable-workflow-foundation/test_models.py
git commit -m "feat: define durable workflow contracts"
```

---

## Task 2：实现 Definition Catalog、DAG 校验和预算收紧

**Files:**

- Create: `src/assistant_agent/workflows/definitions.py`
- Create: `src/assistant_agent/workflows/transitions.py`
- Create: `tests/tdd/durable-workflow-foundation/test_definitions.py`
- Create: `tests/tdd/durable-workflow-foundation/test_transitions.py`

**Interfaces:**

- Consumes: Task 1 contracts。
- Produces: `WorkflowDefinitionDescriptor`、`WorkflowDefinition`、`WorkflowDefinitionCatalog`、`WorkflowLimits`、`validate_plan_dag()`、`create_initial_bundle()`、`request_cancel()`。

- [ ] **Step 1：写 catalog、DAG 和预算 RED 测试**

```python
import pytest

from assistant_agent.workflows.definitions import (
    DuplicateWorkflowDefinition,
    UnknownWorkflowDefinition,
    WorkflowDefinitionCatalog,
)
from assistant_agent.workflows.transitions import validate_plan_dag


def test_catalog_rejects_duplicate_type_and_unknown_lookup(probe_definition) -> None:
    catalog = WorkflowDefinitionCatalog([probe_definition])
    with pytest.raises(DuplicateWorkflowDefinition):
        catalog.register(probe_definition)
    with pytest.raises(UnknownWorkflowDefinition):
        catalog.require("missing")


def test_plan_rejects_cycle_and_unknown_dependency(plan_factory) -> None:
    with pytest.raises(ValueError, match="cycle"):
        validate_plan_dag(plan_factory({"a": ["b"], "b": ["a"]}))
    with pytest.raises(ValueError, match="unknown dependency"):
        validate_plan_dag(plan_factory({"a": ["missing"]}))
```

预算测试必须证明用户请求只能被系统 policy 收紧，不能扩大默认/最大值；deadline 使用注入的 aware clock，保证测试不 sleep。

- [ ] **Step 2：运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_definitions.py \
  tests/tdd/durable-workflow-foundation/test_transitions.py
```

Expected: collection FAIL，提示目标模块不存在。

- [ ] **Step 3：实现 Definition Protocol 和显式 Catalog**

```python
class WorkflowDefinitionDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workflow_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    definition_version: str = Field(min_length=1, max_length=80)


class WorkflowDefinition(Protocol):
    descriptor: WorkflowDefinitionDescriptor

    def validate_submission(self, submission: WorkflowSubmission) -> None: ...

    def build_initial_plan(
        self,
        *,
        workflow_id: str,
        submission: WorkflowSubmission,
    ) -> WorkflowPlanVersion: ...


class WorkflowDefinitionCatalog:
    def __init__(self, definitions: Iterable[WorkflowDefinition] = ()) -> None: ...
    def register(self, definition: WorkflowDefinition) -> None: ...
    def require(self, workflow_type: str) -> WorkflowDefinition: ...
    def list_types(self) -> tuple[str, ...]: ...
```

Catalog 按 `workflow_type` 唯一，`list_types()` 排序且只返回字符串，不把 definition 对象暴露给入口层。`definitions.py` 不提供默认/Probe/Research definition；Probe 只定义在测试 fixture。

- [ ] **Step 4：实现纯状态函数**

`WorkflowLimits` 是 frozen Pydantic model，字段固定为：默认/最大 model calls、tool calls、workflow quanta、deadline seconds、max work items，且 `default <= maximum` 由 validator 保证。

`normalize_budget(request, limits, now)` 对每个非空请求取 `min(requested, maximum)`，空请求取 default，返回绝对 aware `deadline_at`。

`validate_plan_dag(plan, max_work_items)` 必须：

1. 拒绝 work item 数量超限；
2. 拒绝 self dependency、未知 dependency 和重复 dependency；
3. 用 Kahn 或 DFS 验证无环；
4. 不修改输入 plan；
5. 至少一个 root work item；
6. 将 root 的初始 `pending` 标准化为 `ready` 只由 `create_initial_bundle()` 完成。

`create_initial_bundle(...)` 调 definition 之前由 service 完成 identity/definition lookup；函数接收已经验证的 definition version、submission digest、plan 和 budget，验证 DAG 后深拷贝 plan，把 root 标为 `ready`，创建 revision 1 的 `WorkflowRecord`、`WorkflowBundle` 和两个初始事件：`workflow.accepted`、`workflow.plan.created`。

`request_cancel(bundle, now, reason_code)` 返回深拷贝结果和事件：

- 已 `cancelled` 时幂等返回，无新事件；
- `completed/failed` 时抛 `WorkflowTransitionRejected`；
- 无活动 lease 时设 `cancelled/terminal_at/terminal_reason_code`，未成功 item 标 `cancelled`；
- 有活动 lease 时只设 `cancel_requested=True`；
- 不增加 revision，revision 只由 Store save 原子增加。

- [ ] **Step 5：运行 GREEN 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_definitions.py \
  tests/tdd/durable-workflow-foundation/test_transitions.py
```

Expected: PASS。

- [ ] **Step 6：提交 definition 与状态纯函数**

```bash
git add src/assistant_agent/workflows/definitions.py \
  src/assistant_agent/workflows/transitions.py \
  tests/tdd/durable-workflow-foundation/test_definitions.py \
  tests/tdd/durable-workflow-foundation/test_transitions.py
git commit -m "feat: add workflow definition and transition rules"
```

---

## Task 3：定义 Store Protocol 并实现 InMemory 原子语义

**Files:**

- Create: `src/assistant_agent/workflows/store.py`
- Create: `tests/tdd/durable-workflow-foundation/test_in_memory_store.py`

**Interfaces:**

- Consumes: `WorkflowBundle`、`WorkflowEvent`、`WorkflowLease`。
- Produces: `WorkflowStore`、`InMemoryWorkflowStore`、`WorkflowAlreadyExists`、`WorkflowRevisionConflict`、`WorkflowLeaseConflict`。

- [ ] **Step 1：写 create/save/event/OCC/lease RED 测试**

```python
from datetime import timedelta

import pytest

from assistant_agent.workflows.store import (
    InMemoryWorkflowStore,
    WorkflowRevisionConflict,
)


def test_save_is_atomic_and_rejects_stale_revision(initial_bundle, initial_events) -> None:
    store = InMemoryWorkflowStore()
    created = store.create(initial_bundle, initial_events)
    first = created.model_copy(deep=True)
    first.workflow.phase = "first-write"
    saved = store.save(first, expected_revision=1, events=[])
    assert saved.workflow.revision == 2

    stale = created.model_copy(deep=True)
    stale.workflow.phase = "stale-write"
    with pytest.raises(WorkflowRevisionConflict):
        store.save(stale, expected_revision=1, events=[])
    assert store.load(created.workflow.workflow_id).workflow.phase == "first-write"


def test_expired_lease_can_be_reclaimed_without_old_worker_release(
    initial_bundle, initial_events, aware_now
) -> None:
    store = InMemoryWorkflowStore()
    store.create(initial_bundle, initial_events)
    old = store.claim_next(worker_id="worker-old", now=aware_now, lease_seconds=30)
    new = store.claim_next(
        worker_id="worker-new",
        now=aware_now + timedelta(seconds=31),
        lease_seconds=30,
    )
    assert old is not None and new is not None
    assert new.lease_token != old.lease_token
    with pytest.raises(Exception):
        store.release(old, expected_revision=old.workflow_revision)
```

同时覆盖：create duplicate；按 submission unique key 查找；event cursor 从 1 单调递增；list after/limit；copy-on-read；非 claimable 状态不被领取；未过期 lease 不被抢占；release token/owner/revision 三重校验。

- [ ] **Step 2：运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_in_memory_store.py
```

Expected: collection FAIL，提示 `assistant_agent.workflows.store` 不存在。

- [ ] **Step 3：实现 Protocol、错误和 InMemory Store**

```python
class WorkflowStore(Protocol):
    def create(
        self, bundle: WorkflowBundle, events: list[WorkflowEvent]
    ) -> WorkflowBundle: ...

    def load(self, workflow_id: str) -> WorkflowBundle | None: ...

    def load_by_submission(
        self,
        *,
        user_id: str,
        agent_id: str,
        ingress_run_id: str,
        idempotency_key: str,
    ) -> WorkflowBundle | None: ...

    def save(
        self,
        bundle: WorkflowBundle,
        *,
        expected_revision: int,
        events: list[WorkflowEvent],
    ) -> WorkflowBundle: ...

    def list_events(
        self, workflow_id: str, *, after: int = 0, limit: int = 100
    ) -> list[WorkflowEvent]: ...

    def claim_next(
        self, *, worker_id: str, now: datetime, lease_seconds: int
    ) -> WorkflowLease | None: ...

    def release(
        self, lease: WorkflowLease, *, expected_revision: int
    ) -> None: ...

    def close(self) -> None: ...
```

InMemory 实现使用 `RLock`，内部保存 bundle deep copy、submission unique index 和 events。`create()` 原子写三者；`save()` 先验证 expected revision，再让 Store 设置 `revision=expected+1` 和 `updated_at`，最后追加分配 cursor 的 event；调用者不能预增 revision。

`claim_next()` 只领取 `queued/running/recovering` 且 lease 为空或过期的最早 Workflow；
`cancel_requested=True` 不能被过滤掉，因为未来 worker 必须能够 reclaim 并在 graph guard 中终结它。
claim 原子写入 owner/token/expires、仅在未请求取消时把 queued 转 running、增加 revision，然后返回包含
新 revision 的 lease。`release()` 要求 lease owner/token 和 expected revision 全相等，清 lease并再次增加
revision。

- [ ] **Step 4：运行 GREEN 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_in_memory_store.py
```

Expected: PASS。

- [ ] **Step 5：提交 InMemory Store**

```bash
git add src/assistant_agent/workflows/store.py \
  tests/tdd/durable-workflow-foundation/test_in_memory_store.py
git commit -m "feat: add in-memory workflow store"
```

---

## Task 4：实现 SQLite Store 与跨实例重开

**Files:**

- Create: `src/assistant_agent/workflows/sqlite_store.py`
- Create: `tests/tdd/durable-workflow-foundation/test_sqlite_store.py`

**Interfaces:**

- Consumes: Task 3 `WorkflowStore` 语义。
- Produces: `SQLiteWorkflowStore`，行为与 InMemory Store 一致。

- [ ] **Step 1：写 SQLite parity 和 restart RED 测试**

用 pytest `tmp_path` 创建数据库，测试：

```python
def test_sqlite_reopens_bundle_events_and_submission_index(
    tmp_path, initial_bundle, initial_events
) -> None:
    path = tmp_path / "workflows.sqlite3"
    first = SQLiteWorkflowStore(path)
    created = first.create(initial_bundle, initial_events)
    first.close()

    reopened = SQLiteWorkflowStore(path)
    loaded = reopened.load(created.workflow.workflow_id)
    duplicate = reopened.load_by_submission(
        user_id=created.workflow.user_id,
        agent_id=created.workflow.agent_id,
        ingress_run_id=created.workflow.ingress_run_id,
        idempotency_key=created.workflow.idempotency_key,
    )
    assert loaded == created
    assert duplicate == created
    assert [item.cursor for item in reopened.list_events(created.workflow.workflow_id)] == [1, 2]
```

复用一组 parametrized parity helpers 对 InMemory/SQLite 验证 stale save、event cursor、claim、lease expiry 和 old token release。

- [ ] **Step 2：运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_sqlite_store.py
```

Expected: collection FAIL，提示 `SQLiteWorkflowStore` 不存在。

- [ ] **Step 3：实现 WAL SQLite schema 和事务**

初始化连接：`check_same_thread=False`、row factory、`PRAGMA journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=1000`、进程内 `RLock`。

schema 固定为：

```sql
CREATE TABLE IF NOT EXISTS durable_workflows (
  workflow_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  ingress_run_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL,
  revision INTEGER NOT NULL,
  cancel_requested INTEGER NOT NULL,
  lease_owner TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  bundle_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, agent_id, ingress_run_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS durable_workflow_events (
  workflow_id TEXT NOT NULL,
  cursor INTEGER NOT NULL,
  event_json TEXT NOT NULL,
  PRIMARY KEY (workflow_id, cursor),
  FOREIGN KEY (workflow_id) REFERENCES durable_workflows(workflow_id)
);

CREATE INDEX IF NOT EXISTS idx_durable_workflows_claim
ON durable_workflows(status, cancel_requested, lease_expires_at, updated_at);
```

`create/save/claim/release` 全部使用 `BEGIN IMMEDIATE`，异常 rollback。`save()` 的 UPDATE 必须带 `WHERE workflow_id=? AND revision=?`；cursor 在同一事务中读取 `MAX(cursor)` 并插入。claim 先按 `updated_at, workflow_id` 选一条，再带旧 revision 条件 UPDATE。JSON 使用 Pydantic `model_dump_json()` / `model_validate_json()`，不手工拼接业务字段。

遇到 workflow PK 或 submission unique 冲突统一抛 `WorkflowAlreadyExists`；service 在下一 Task 通过 `load_by_submission()` 区分幂等和 payload conflict。

- [ ] **Step 4：运行 SQLite GREEN 和 Store parity**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_in_memory_store.py \
  tests/tdd/durable-workflow-foundation/test_sqlite_store.py
```

Expected: PASS。

- [ ] **Step 5：提交 SQLite Store**

```bash
git add src/assistant_agent/workflows/sqlite_store.py \
  tests/tdd/durable-workflow-foundation/test_sqlite_store.py
git commit -m "feat: persist durable workflow state in sqlite"
```

---

## Task 5：实现 Identity-scoped WorkflowService 与幂等提交

**Files:**

- Create: `src/assistant_agent/workflows/service.py`
- Create: `tests/tdd/durable-workflow-foundation/test_service.py`

**Interfaces:**

- Consumes: `RequestIdentity`、Definition catalog、transitions、Store。
- Produces: `WorkflowService.submit()`、`get_workflow()`、`list_events()`、`cancel()` 和结构化 service errors。

- [ ] **Step 1：写提交、身份、幂等和取消 RED 测试**

```python
import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.service import (
    WorkflowAccessDenied,
    WorkflowSubmissionConflict,
)


def test_submit_is_idempotent_only_for_same_payload(service, submission) -> None:
    identity = RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )
    first = service.submit(
        identity=identity,
        ingress_run_id="run-sentinel",
        submission=submission,
    )
    same = service.submit(
        identity=identity,
        ingress_run_id="run-sentinel",
        submission=submission,
    )
    assert same.workflow.workflow_id == first.workflow.workflow_id

    changed = submission.model_copy(update={"objective": "changed-objective"})
    with pytest.raises(WorkflowSubmissionConflict):
        service.submit(
            identity=identity,
            ingress_run_id="run-sentinel",
            submission=changed,
        )


def test_read_is_owner_and_agent_scoped(service, submitted_bundle) -> None:
    with pytest.raises(WorkflowAccessDenied):
        service.get_workflow(
            identity=RequestIdentity.for_user(
                user_id="user-sentinel", agent_id="other-agent"
            ),
            workflow_id=submitted_bundle.workflow.workflow_id,
        )
```

继续测试：unknown definition；definition validation error；缺 session；预算被收紧；DAG invalid 不落库；duplicate race fallback；events identity check；limit clamp 1—500；queued cancel terminal 幂等；completed cancel 拒绝；leased cancel 只置 request 且旧 revision 后续保存失败。

- [ ] **Step 2：运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_service.py
```

Expected: collection FAIL，提示 service 不存在。

- [ ] **Step 3：实现结构化错误和 submission digest**

```python
class WorkflowServiceError(RuntimeError):
    code = "workflow_service_error"


class WorkflowNotFound(WorkflowServiceError):
    code = "workflow_not_found"


class WorkflowAccessDenied(WorkflowServiceError):
    code = "workflow_access_denied"


class WorkflowSubmissionRejected(WorkflowServiceError):
    code = "workflow_submission_rejected"


class WorkflowSubmissionConflict(WorkflowServiceError):
    code = "workflow_submission_conflict"


def submission_digest(submission: WorkflowSubmission) -> str:
    payload = json.dumps(
        submission.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

异常消息不得包含完整 objective、inputs 或 artifact 内容；Tool 层后续只消费 `code` 和安全摘要。

- [ ] **Step 4：实现 WorkflowService**

构造函数：

```python
class WorkflowService:
    def __init__(
        self,
        *,
        store: WorkflowStore,
        definitions: WorkflowDefinitionCatalog,
        limits: WorkflowLimits | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None: ...
```

`submit()` 顺序必须固定：

1. 要求可信 `identity.user_id/agent_id/session_id` 和非空 `ingress_run_id`；
2. 计算 digest，并用 submission unique key 查询；存在时相同 digest 返回，其他 digest 抛 conflict；
3. `definitions.require(workflow_type)`；
4. `definition.validate_submission(submission)`；
5. 生成随机 `workflow_<32 hex>`；
6. `definition.build_initial_plan()`，校验返回 workflow ID 和 definition version；
7. 收紧预算，调用 `create_initial_bundle()`；
8. `store.create()`；若发生 duplicate race，重新按 submission key 加载并执行第 2 步比较；
9. 返回持久化后的 deep-copy bundle。

`get_workflow()` 先 load；不存在抛 not found；存在但 `user_id` 或 `agent_id` 不同抛 access denied。不要用 session 作为长期 owner 边界，session 只用于关联。

`list_events()` 必须先调用 `get_workflow()` 做身份检查，再 clamp limit。

`cancel()` 先做身份检查，调用 `request_cancel()`，无新事件时直接返回；有事件时以当前 revision `store.save()`。如果并发冲突，不在 service 内无限重试，转换为 `WorkflowSubmissionConflict` 的同级 `WorkflowStateConflict`。

- [ ] **Step 5：运行 Service GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_service.py
```

Expected: PASS。

- [ ] **Step 6：用 SQLite 重建 Service 验证跨进程语义**

在 `test_service.py` 增加：第一实例 submit/close；第二实例使用同一路径、同一 Probe catalog 重建 service；按 identity 读取同一 Workflow 和 events；重复 submit 返回同一 ID；不同 payload conflict。

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation/test_service.py \
  tests/tdd/durable-workflow-foundation/test_sqlite_store.py
```

Expected: PASS。

- [ ] **Step 7：提交 Service**

```bash
git add src/assistant_agent/workflows/service.py \
  tests/tdd/durable-workflow-foundation/test_service.py
git commit -m "feat: add identity-scoped workflow service"
```

---

## Task 6：文档对账与本实施包验证

**Files:**

- Modify: `docs/development/2026-08-07-durable-workflow-runtime-experiment-design.md`
- Modify: `docs/tool-calling-architecture.md`

- [ ] **Step 1：对账设计文档**

在实验设计的实施阶段增加实际状态：P1/阶段 0 已实现的 commit、schema 和测试命令；明确记录以下设计修正：

- 通用 `WorkflowSubmission` 不直接包含 `research_questions`，Research 输入后续放在 definition-owned `inputs` schema；
- P1 没有 `workspace_ref/current_checkpoint_id/attempt`，这些字段只在其实际实施包中添加；
- P1 没有注册 Tool、启动 worker 或改变 runtime。

不要把 P2—P9 标记为已确认或已实施。

- [ ] **Step 2：更新当前 Tool 权威文档**

在 `docs/tool-calling-architecture.md` 的 durable task / extension 邻近位置增加简短当前事实：仓库存在未暴露的 `assistant_agent.workflows` foundation；没有 builtin Workflow Tool、worker 或 runtime candidate exposure；现有 `task_plan_submit` 行为不变。避免复制实验设计全文。

- [ ] **Step 3：运行本包完整临时 TDD**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/durable-workflow-foundation
```

Expected: PASS；不得访问网络或真实 Provider。

- [ ] **Step 4：运行静态导入与编译检查**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/workflows
```

Expected: exit 0。

- [ ] **Step 5：确认普通 runtime 没有引用新包**

Run:

```bash
rg -n "assistant_agent\.workflows|workflow_submit" \
  src/assistant_agent/runtime \
  src/assistant_agent/gateway \
  src/assistant_agent/tools/plugins/defaults.py \
  src/assistant_agent/tools/plugins/registry_factory.py
```

Expected: 无匹配。若出现匹配，说明 scope 泄漏，先移除再结束本包。

- [ ] **Step 6：核对工作树并提交文档**

```bash
git status --short
git diff --check
git add docs/tool-calling-architecture.md
git commit -m "docs: record dormant workflow foundation"
```

`docs/development/**` 默认被本地 exclude 忽略，不强制加入 commit；只保留本地实验记录。不得提交用户的无关修改。

## Completion Report

完成时必须按仓库格式报告：

```text
Core invariant: unchanged.
Tests: added tests/tdd/durable-workflow-foundation for temporary RED/GREEN; user may delete the directory manually.
```

同时列出实际运行命令、commit、未完成的 P2—P9，以及明确结论：没有注册 `workflow_submit`、没有启动 Workflow worker、没有修改普通 `AgentGraphRuntime` 行为、没有调用真实 Provider。
