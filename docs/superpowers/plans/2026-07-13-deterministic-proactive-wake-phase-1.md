# Deterministic Proactive Wake Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first deterministic, offline Proactive Wake vertical slice: explicit structured rules trigger one governed read-only probe, establish a silent baseline, detect later evidence changes, apply attention policy, enqueue at most one durable notification, and deliver it through a mock transport without interrupting an active realtime run.

**Architecture:** Add a focused `assistant_agent.services.proactive_wake` package around existing identity, ToolSpec, ActionValidator, ToolExecutor and Gateway session boundaries. Persist rule/state/run/dedup/outbox records in a separate SQLite database and keep the coordinator deterministic; Phase 1 performs no LLM calls, starts no background scheduler, exposes no real Provider/channel, and does not project proactive messages into conversation history.

**Tech Stack:** Python 3.11+, Pydantic v2, standard-library `sqlite3`, `asyncio`, `zoneinfo`, FastAPI project conventions, pytest, existing `assistant_agent` Tool/Gateway services.

## Global Constraints

- Keep the default runtime mock/local/offline; do not call a real LLM, calendar, email, notification, web, database, or messaging Provider.
- Do not install dependencies or change the `hello_agent` conda environment.
- Do not create a second Agent loop, generic cron service, background autonomous process, or Personal OS scheduler.
- Phase 1 guarantees per-rule serialization inside one process; it does not claim cross-process WakeRun ownership or distributed scheduling.
- Do not add `PROACTIVE_CHECK`, prompt rendering, semantic conditions, provider event ingestion, real App/IM transport, or API routes in Phase 1.
- Only user-authored structured rules are eligible; Agent-created rules and free-form `HEARTBEAT.md` execution remain unsupported.
- Every probe must pass explicit proactive allowlist checks, `ToolPolicyInterpreter`, `ActionValidator`, `ToolExecutor`, and `ToolRegistry`.
- Accepted probe tools must be auto-executable, require no confirmation, declare side effect `none`, `local_read`, or `external_read`, and declare no `resource_writes`.
- Persist only stable `WakeOwner` identity fields; reconstruct runtime `RequestIdentity`/`UserRequest` data from trusted services for each run.
- First successful observation establishes a silent baseline unless `notify_on_initial=true`; Phase 1 supports only `condition.mode="changed"`.
- A duplicate signal, unchanged evidence, quiet-hour suppression, cooldown, daily limit, active realtime run, retry, or restart must never cause a second probe-derived notification for the same evidence fingerprint.
- Proactive state belongs in `.local/proactive_wake.sqlite3`, not Memory Store or conversation history.
- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest commands.
- Use `apply_patch` for manual file changes and preserve unrelated dirty-worktree changes.
- Commit the already-written design and this plan only together with Phase 1 code/tests; do not create a documentation-only commit.

---

## File Structure

### New production files

- `src/assistant_agent/schemas/proactive_wake.py`: versioned Pydantic contracts and stable literal types.
- `src/assistant_agent/services/proactive_wake/__init__.py`: deliberately small public exports for Phase 1.
- `src/assistant_agent/services/proactive_wake/store.py`: SQLite schema, rule/state/run/dedup/outbox repositories and atomic outcome writes.
- `src/assistant_agent/services/proactive_wake/probe.py`: proactive rule validation plus governed ToolExecutor probe and prompt-safe observation conversion.
- `src/assistant_agent/services/proactive_wake/change_detector.py`: canonical JSON evidence fingerprint and baseline/change construction.
- `src/assistant_agent/services/proactive_wake/policy.py`: deterministic `changed` evaluator, quiet-hours/cooldown/limit policy and notification envelope builder.
- `src/assistant_agent/services/proactive_wake/activity.py`: `UserActivityReader`, null implementation and Gateway adapter.
- `src/assistant_agent/services/proactive_wake/coordinator.py`: one WakeRun orchestration path.
- `src/assistant_agent/services/proactive_wake/delivery.py`: transport protocol, mock transport and durable outbox worker.
- `scripts/run_proactive_wake_demo.py`: deterministic offline baseline/change/delivery demonstration.

### Existing production files modified

- `src/assistant_agent/gateway/session.py`: add public async active-run snapshots without exposing private dictionaries.
- `src/assistant_agent/gateway/__init__.py`: export no new proactive type; keep Gateway aggregate stable unless tests require the existing manager class import.
- `docs/gateway-architecture.md`: document the read-only active-run query and non-interruption boundary.
- `docs/tool-calling-architecture.md`: document proactive read-only probes through the existing governance chain.
- `docs/personal-realtime-ai-assistant-roadmap.md`: mark only the deterministic Phase 1 slice implemented; keep complex scheduler/real delivery deferred.

### New tests

- `tests/test_proactive_wake_schemas.py`
- `tests/test_proactive_wake_store.py`
- `tests/test_proactive_wake_probe.py`
- `tests/test_proactive_wake_policy.py`
- `tests/test_proactive_wake_gateway_activity.py`
- `tests/test_proactive_wake_coordinator.py`
- `tests/test_proactive_wake_delivery.py`
- `tests/test_proactive_wake_demo.py`

---

### Task 1: Add the stable Proactive Wake contracts

**Files:**
- Create: `src/assistant_agent/schemas/proactive_wake.py`
- Create: `src/assistant_agent/services/proactive_wake/__init__.py`
- Create: `tests/test_proactive_wake_schemas.py`
- Include in commit: `docs/superpowers/specs/2026-07-13-proactive-wake-design.md`
- Include in commit: `docs/superpowers/plans/2026-07-13-deterministic-proactive-wake-phase-1.md`

**Interfaces:**
- Consumes: `assistant_agent.schemas.identity.RequestIdentity` only through `WakeOwner.from_identity()`; no persisted scopes/session.
- Produces: `WakeOwner`, `WakeRule`, `WakeSignal`, `WakeEvidence`, `WakeDecision`, `AttentionDecision`, `WakeRuleState`, `WakeRun`, `NotificationEnvelope`, `DeliveryResult`, `ProactiveWakeRunResult`, and literal status aliases used by every later task.

- [ ] **Step 1: Write failing schema tests**

```python
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.proactive_wake import (
    WakeConditionSpec,
    WakeDecision,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeTriggerSpec,
)


def test_wake_owner_persists_only_stable_identity() -> None:
    identity = RequestIdentity.for_user(
        user_id="u1",
        session_id="temporary-session",
        tenant_id="tenant-1",
        project_id="project-1",
        allowed_scopes=["session", "user_profile"],
    )

    owner = WakeOwner.from_identity(identity)

    assert owner.model_dump() == {
        "tenant_id": "tenant-1",
        "user_id": "u1",
        "project_id": "project-1",
    }


def test_changed_rule_defaults_to_silent_initial_baseline() -> None:
    rule = WakeRule(
        rule_id="rule-1",
        owner=WakeOwner(user_id="u1"),
        name="Calendar changes",
        trigger=WakeTriggerSpec(event_sources=["calendar"], event_types=["calendar.changed"]),
        probe=WakeProbeSpec(tool_name="calendar.search_events", arguments={"query": "next two hours"}),
        condition=WakeConditionSpec(mode="changed", notify_when="Calendar evidence changes"),
    )

    assert rule.condition.notify_on_initial is False
    assert rule.version == 1
    assert rule.enabled is True


def test_silent_decision_rejects_user_message() -> None:
    with pytest.raises(ValidationError):
        WakeDecision(
            outcome="silent",
            severity="normal",
            reason_code="unchanged",
            summary="No change.",
            user_message="This must not be sent.",
            evidence_ids=["e1"],
        )


def test_notify_decision_requires_message_and_evidence() -> None:
    with pytest.raises(ValidationError):
        WakeDecision(
            outcome="notify",
            severity="normal",
            reason_code="evidence_changed",
            summary="Changed.",
            evidence_ids=[],
        )
```

- [ ] **Step 2: Run the schema tests and verify the missing-module failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_schemas.py -q
```

Expected: FAIL during collection with `ModuleNotFoundError: assistant_agent.schemas.proactive_wake`.

- [ ] **Step 3: Implement the Pydantic contracts**

Create `src/assistant_agent/schemas/proactive_wake.py` with these exact public types and invariants:

```python
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from assistant_agent.schemas.identity import RequestIdentity

WakeSignalKind = Literal["provider_event", "reconcile_tick", "manual"]
WakeConditionMode = Literal["changed", "semantic"]
WakeDecisionOutcome = Literal["silent", "notify"]
AttentionOutcome = Literal["allow", "defer", "suppress"]
WakeRunStatus = Literal[
    "received",
    "deduplicated",
    "config_error",
    "probing",
    "probe_failed",
    "baseline_established",
    "unchanged",
    "notify_candidate",
    "suppressed",
    "enqueued",
    "delivered",
    "delivery_failed",
]
DeliveryStatus = Literal[
    "queued",
    "leased",
    "sent",
    "acknowledged",
    "retry_wait",
    "expired",
    "dead_letter",
]
Severity = Literal["low", "normal", "high"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class WakeOwner(BaseModel):
    tenant_id: str | None = None
    user_id: str = Field(min_length=1)
    project_id: str | None = None

    @classmethod
    def from_identity(cls, identity: RequestIdentity) -> "WakeOwner":
        return cls(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            project_id=identity.project_id,
        )


class WakeTriggerSpec(BaseModel):
    event_sources: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    reconcile_interval_s: int = Field(default=3600, ge=60)


class WakeProbeSpec(BaseModel):
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class WakeConditionSpec(BaseModel):
    mode: WakeConditionMode = "changed"
    notify_when: str = Field(min_length=1, max_length=500)
    notify_on_initial: bool = False


class QuietHours(BaseModel):
    start_local: time
    end_local: time
    timezone: str = Field(default="Asia/Shanghai", min_length=1)


class WakeAttentionSpec(BaseModel):
    channel: str = Field(default="mock_app", min_length=1)
    quiet_hours: QuietHours | None = None
    cooldown_s: int = Field(default=1800, ge=0)
    daily_notification_limit: int = Field(default=6, ge=1, le=100)
    minimum_severity: Severity = "normal"


class WakeRule(BaseModel):
    rule_id: str = Field(default_factory=lambda: _id("wake_rule"), min_length=1)
    owner: WakeOwner
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    version: int = Field(default=1, ge=1)
    trigger: WakeTriggerSpec
    probe: WakeProbeSpec
    condition: WakeConditionSpec
    attention: WakeAttentionSpec = Field(default_factory=WakeAttentionSpec)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WakeSignal(BaseModel):
    signal_id: str = Field(default_factory=lambda: _id("wake_signal"), min_length=1)
    kind: WakeSignalKind
    source: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    occurred_at: datetime = Field(default_factory=utc_now)
    owner: WakeOwner
    event_key: str | None = None
    cursor: str | None = None
    prompt_safe_facts: dict[str, Any] = Field(default_factory=dict)


class WakeEvidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: _id("wake_evidence"), min_length=1)
    rule_id: str = Field(min_length=1)
    observed_at: datetime = Field(default_factory=utc_now)
    probe_tool_name: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "timed_out"]
    fingerprint: str = Field(min_length=1)
    previous_fingerprint: str | None = None
    is_initial: bool
    changed: bool
    summary: str = Field(min_length=1, max_length=500)
    prompt_safe_payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)


class WakeDecision(BaseModel):
    outcome: WakeDecisionOutcome
    severity: Severity
    reason_code: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)
    user_message: str | None = Field(default=None, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "WakeDecision":
        if self.outcome == "silent" and self.user_message is not None:
            raise ValueError("silent decision must not include user_message")
        if self.outcome == "notify" and (not self.user_message or not self.evidence_ids):
            raise ValueError("notify decision requires user_message and evidence_ids")
        return self


class AttentionDecision(BaseModel):
    outcome: AttentionOutcome
    reason_code: str = Field(min_length=1)
    deliver_after: datetime | None = None
    expires_at: datetime | None = None


class WakeRuleState(BaseModel):
    rule_id: str = Field(min_length=1)
    last_fingerprint: str | None = None
    last_checked_at: datetime | None = None
    last_notified_at: datetime | None = None
    last_notified_fingerprint: str | None = None
    next_reconcile_at: datetime | None = None
    notification_count_date: date | None = None
    notification_count: int = Field(default=0, ge=0)


class WakeRun(BaseModel):
    run_id: str = Field(default_factory=lambda: _id("wake_run"), min_length=1)
    rule_id: str = Field(min_length=1)
    owner: WakeOwner
    signal_id: str = Field(min_length=1)
    status: WakeRunStatus = "received"
    reason_code: str | None = None
    evidence: WakeEvidence | None = None
    decision: WakeDecision | None = None
    attention: AttentionDecision | None = None
    delivery_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NotificationEnvelope(BaseModel):
    delivery_id: str = Field(default_factory=lambda: _id("wake_delivery"), min_length=1)
    owner: WakeOwner
    channel: str = Field(min_length=1)
    destination_ref: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    evidence_fingerprint: str = Field(min_length=1)
    deliver_after: datetime
    expires_at: datetime
    status: DeliveryStatus = "queued"
    attempt_count: int = Field(default=0, ge=0)
    lease_until: datetime | None = None
    provider_message_id: str | None = None
    last_reason_code: str | None = None


class DeliveryResult(BaseModel):
    accepted: bool
    provider_message_id: str | None = None
    error_code: str | None = None


class ProactiveWakeRunResult(BaseModel):
    run: WakeRun
    notification: NotificationEnvelope | None = None
```

Create `src/assistant_agent/services/proactive_wake/__init__.py` with only schema re-exports initially; later tasks add service exports deliberately.

- [ ] **Step 4: Run schema tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_schemas.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit contracts with the approved design and plan**

```bash
git add docs/superpowers/specs/2026-07-13-proactive-wake-design.md docs/superpowers/plans/2026-07-13-deterministic-proactive-wake-phase-1.md src/assistant_agent/schemas/proactive_wake.py src/assistant_agent/services/proactive_wake/__init__.py tests/test_proactive_wake_schemas.py
git commit -m "Add proactive wake contracts"
```

---

### Task 2: Add SQLite rule, state, dedup and run persistence

**Files:**
- Create: `src/assistant_agent/services/proactive_wake/store.py`
- Create: `tests/test_proactive_wake_store.py`
- Modify: `src/assistant_agent/services/proactive_wake/__init__.py`

**Interfaces:**
- Consumes: Task 1 schema models.
- Produces: `SQLiteProactiveWakeStore(path)`, `save_rule()`, `get_rule()`, `list_rules()`, `list_due_rules()`, `begin_run()`, `get_rule_state()`, `complete_run()`, and `list_runs()`.

- [ ] **Step 1: Write failing store tests**

Cover all of these concrete cases in `tests/test_proactive_wake_store.py`:

```python
from datetime import datetime, timedelta, timezone

from assistant_agent.schemas.proactive_wake import (
    WakeConditionSpec,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.services.proactive_wake.store import SQLiteProactiveWakeStore


def make_rule() -> WakeRule:
    return WakeRule(
        rule_id="rule-1",
        owner=WakeOwner(user_id="u1"),
        name="Calendar changes",
        trigger=WakeTriggerSpec(event_sources=["calendar"], event_types=["calendar.changed"]),
        probe=WakeProbeSpec(tool_name="calendar.search_events", arguments={"query": "next two hours"}),
        condition=WakeConditionSpec(mode="changed", notify_when="Calendar evidence changes"),
    )


def test_rule_round_trip_and_owner_scope(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)

    assert store.get_rule(WakeOwner(user_id="u1"), "rule-1") == rule
    assert store.get_rule(WakeOwner(user_id="u2"), "rule-1") is None


def test_begin_run_claims_event_key_once(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    signal = WakeSignal(
        signal_id="signal-1",
        kind="provider_event",
        source="calendar",
        event_type="calendar.changed",
        event_key="calendar-event-1",
        owner=rule.owner,
    )

    first, first_claimed = store.begin_run(rule, signal)
    second, second_claimed = store.begin_run(rule, signal.model_copy(update={"signal_id": "signal-2"}))

    assert first_claimed is True
    assert first.status == "received"
    assert second_claimed is False
    assert second.status == "deduplicated"


def test_due_rules_use_persisted_next_reconcile_at(tmp_path) -> None:
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    state = store.get_rule_state(rule.rule_id)
    store.save_rule_state(state.model_copy(update={"next_reconcile_at": now - timedelta(seconds=1)}))

    assert [item.rule_id for item in store.list_due_rules(now=now, limit=10)] == ["rule-1"]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_store.py -q
```

Expected: FAIL with missing `store` module.

- [ ] **Step 3: Implement the SQLite schema and repository methods**

Use a schema version table and JSON model payloads. The migration must execute these tables exactly once under a transaction:

```sql
CREATE TABLE IF NOT EXISTS proactive_wake_schema_version (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wake_rules (
    rule_id TEXT PRIMARY KEY,
    tenant_id TEXT,
    user_id TEXT NOT NULL,
    project_id TEXT,
    enabled INTEGER NOT NULL,
    version INTEGER NOT NULL,
    rule_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wake_rules_owner
ON wake_rules (tenant_id, user_id, project_id, enabled);

CREATE TABLE IF NOT EXISTS wake_rule_state (
    rule_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    next_reconcile_at TEXT,
    FOREIGN KEY (rule_id) REFERENCES wake_rules(rule_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS wake_signal_dedup (
    dedup_key TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wake_runs (
    run_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    tenant_id TEXT,
    user_id TEXT NOT NULL,
    project_id TEXT,
    status TEXT NOT NULL,
    run_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Implement these exact public signatures:

- `SQLiteProactiveWakeStore.__init__(path: Path | str = ".local/proactive_wake.sqlite3") -> None`
- `save_rule(rule: WakeRule) -> WakeRule`
- `get_rule(owner: WakeOwner, rule_id: str) -> WakeRule | None`
- `list_rules(owner: WakeOwner) -> list[WakeRule]`
- `delete_rule(owner: WakeOwner, rule_id: str) -> bool`
- `get_rule_state(rule_id: str) -> WakeRuleState`
- `save_rule_state(state: WakeRuleState) -> WakeRuleState`
- `list_due_rules(*, now: datetime, limit: int = 100) -> list[WakeRule]`
- `begin_run(rule: WakeRule, signal: WakeSignal) -> tuple[WakeRun, bool]`
- `complete_run(run: WakeRun, state: WakeRuleState) -> WakeRun`
- `list_runs(owner: WakeOwner, *, limit: int = 100) -> list[WakeRun]`

Required implementation details:

- Enable `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `synchronous=NORMAL`, and `busy_timeout=5000` on each connection.
- Serialize with `model_dump_json()` and restore with `model_validate_json()`.
- Build dedup key as SHA-256 of `owner tenant/user/project + rule_id + (event_key or signal_id)`.
- `begin_run()` inserts the dedup row and WakeRun in one transaction; on dedup conflict it still stores a `deduplicated` WakeRun without executing later work.
- `save_rule()` creates an empty `WakeRuleState` only when no state exists; updating a rule never discards fingerprint/cooldown state.
- `list_due_rules()` returns only enabled rules with non-null `next_reconcile_at <= now`.
- Owner-scoped SQL must compare nullable tenant/project fields with normalized `IS` semantics so `None` never broadens a user query.
- Do not persist `RequestIdentity.allowed_scopes`, session IDs, provider payloads, ToolResult raw data, notification content outside the later outbox table, or secrets.

- [ ] **Step 4: Run store tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_store.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/services/proactive_wake/store.py src/assistant_agent/services/proactive_wake/__init__.py tests/test_proactive_wake_store.py
git commit -m "Add proactive wake SQLite state"
```

---

### Task 3: Add governed read-only probes and evidence fingerprints

**Files:**
- Create: `src/assistant_agent/services/proactive_wake/probe.py`
- Create: `src/assistant_agent/services/proactive_wake/change_detector.py`
- Create: `tests/test_proactive_wake_probe.py`
- Modify: `src/assistant_agent/services/proactive_wake/__init__.py`

**Interfaces:**
- Consumes: `ToolRegistry`, `ToolPolicyInterpreter`, `ActionValidator`, `ToolExecutor`, `observation_from_tool_result()`, and Task 1/2 types.
- Produces: `ProactiveRuleValidator.validate(rule) -> ProactiveRuleValidation`, `GovernedProbeRunner.run(rule, signal) -> ProbeObservation`, and `build_wake_evidence(rule, observation, state, observed_at) -> WakeEvidence`.

- [ ] **Step 1: Write failing probe governance tests**

Create local fake tools in the test using the existing `@tool` decorator pattern. Cover:

```python
def test_read_only_allowlisted_probe_runs_through_validator_and_executor() -> None:
    # Register calendar.search_events with policy risk="external_read",
    # approval="never", dependency_mode="independent", no resource_writes,
    # and model_observation containing one sanitized event.
    # Assert validation accepted, tool called once, and ProbeObservation contains
    # the model_observation but not raw_data_ref/provider raw data.


def test_probe_rejects_tool_not_in_explicit_allowlist() -> None:
    # Validator returns accepted=False and code="proactive_tool_not_allowed".


def test_probe_rejects_write_or_confirmation_tool_before_execution() -> None:
    # Register calendar.create_event with external_write/resource_writes.
    # Assert code="proactive_tool_not_read_only" and tool call count remains zero.


def test_probe_rejects_semantic_condition_in_phase_one() -> None:
    # Rule with condition.mode="semantic" is rejected with
    # code="proactive_condition_mode_unsupported".


def test_fingerprint_is_stable_for_key_order_and_changes_for_evidence() -> None:
    # Canonical JSON dict key ordering produces the same hash;
    # changing event time/title produces a different hash.
```

- [ ] **Step 2: Run tests and verify missing-module failures**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_probe.py -q
```

Expected: FAIL importing `assistant_agent.services.proactive_wake.probe`.

- [ ] **Step 3: Implement rule validation**

Add this exact result contract and validation order in `probe.py`:

```python
class ProactiveRuleValidation(BaseModel):
    accepted: bool
    code: str
    message: str


class ProactiveRuleValidator:
    def __init__(self, *, registry: ToolRegistry, allowed_tool_names: set[str]) -> None:
        self.registry = registry
        self.allowed_tool_names = frozenset(allowed_tool_names)

    def validate(self, rule: WakeRule) -> ProactiveRuleValidation:
        if not rule.enabled:
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_rule_disabled",
                message="Disabled rules cannot run proactive probes.",
            )
        if rule.condition.mode != "changed":
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_condition_mode_unsupported",
                message="Phase 1 only supports condition.mode=changed.",
            )
        if rule.probe.tool_name not in self.allowed_tool_names:
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_tool_not_allowed",
                message="Probe tool is not in the proactive allowlist.",
            )
        spec = next((item for item in self.registry.list_specs() if item.name == rule.probe.tool_name), None)
        if spec is None:
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_tool_unknown",
                message="Probe tool is not registered.",
            )
        view = ToolPolicyInterpreter().view_for_spec(spec)
        if (
            view.side_effect_level not in {"none", "local_read", "external_read"}
            or view.requires_confirmation
            or not view.auto_executable
            or bool(view.resource_writes)
        ):
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_tool_not_read_only",
                message="Probe tool must be auto-executable, confirmation-free, read-only, and declare no resource writes.",
            )
        return ProactiveRuleValidation(
            accepted=True,
            code="accepted",
            message="Rule accepted for deterministic proactive execution.",
        )
```

Do not accept unknown side effects. Do not infer permission only from a tool name or skill Markdown.

- [ ] **Step 4: Implement governed execution and prompt-safe observation conversion**

`GovernedProbeRunner.run()` must:

1. Call `ProactiveRuleValidator.validate()`.
2. Construct a synthetic `UserRequest` with stable `user_id`, session ID `proactive:<rule_id>`, and metadata containing only `source=proactive_wake`, stable tenant/project, rule ID and signal ID.
3. Build `AgentState.from_request(request)` and `AssistantDecision(type="tool_call", tool_name=rule.probe.tool_name, tool_input=dict(rule.probe.arguments), reason="Explicit proactive wake rule probe.")`.
4. Call `ActionValidator.validate()` and return a structured rejected result if it fails.
5. Call `ToolExecutor.run_tool(state, "proactive_probe", tool_name, arguments, trace_id=state.trace_id, node_name="proactive_probe")`.
6. Convert success with `observation_from_tool_result()`, then `compact_observation_for_context()`.
7. Persist/return only `summary`, `structured_output`, `output_ref`, status and error code. Never copy `raw_data_ref`, `audit_payload`, provider raw response or full `ToolResult.data`.

Use this public result:

```python
class ProbeObservation(BaseModel):
    accepted: bool
    code: str
    tool_name: str
    success: bool
    summary: str
    prompt_safe_payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
```

- [ ] **Step 5: Implement canonical fingerprint and evidence construction**

In `change_detector.py`:

```python
def evidence_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_wake_evidence(
    *,
    rule: WakeRule,
    observation: ProbeObservation,
    state: WakeRuleState,
    observed_at: datetime,
) -> WakeEvidence:
    fingerprint = evidence_fingerprint(observation.prompt_safe_payload)
    previous = state.last_fingerprint
    return WakeEvidence(
        rule_id=rule.rule_id,
        observed_at=observed_at,
        probe_tool_name=observation.tool_name,
        status="succeeded" if observation.success else "failed",
        fingerprint=fingerprint,
        previous_fingerprint=previous,
        is_initial=previous is None,
        changed=previous is not None and previous != fingerprint,
        summary=observation.summary[:500],
        prompt_safe_payload=observation.prompt_safe_payload,
        source_refs=observation.source_refs,
    )
```

When probe execution fails, coordinator records `probe_failed`; it must not use a failure fingerprint as the new baseline.

- [ ] **Step 6: Run probe tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_probe.py tests/test_tool_policy_interpreter.py tests/test_tool_executor.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/assistant_agent/services/proactive_wake/probe.py src/assistant_agent/services/proactive_wake/change_detector.py src/assistant_agent/services/proactive_wake/__init__.py tests/test_proactive_wake_probe.py
git commit -m "Add governed proactive wake probes"
```

---

### Task 4: Add deterministic decision and attention policy

**Files:**
- Create: `src/assistant_agent/services/proactive_wake/policy.py`
- Create: `tests/test_proactive_wake_policy.py`
- Modify: `src/assistant_agent/services/proactive_wake/__init__.py`

**Interfaces:**
- Consumes: `WakeRule`, `WakeEvidence`, `WakeRuleState`, UTC `now`, and explicit `user_active` boolean.
- Produces: `DeterministicWakeEvaluator.evaluate()`, `AttentionPolicy.evaluate()`, and `build_notification_envelope()`.

- [ ] **Step 1: Write failing policy tests**

Implement these exact tests and assertions:

- `test_initial_evidence_establishes_silent_baseline_by_default`: `is_initial=True`, `notify_on_initial=False` returns `outcome="silent"`, `reason_code="baseline_established"`, and no `user_message`.
- `test_changed_evidence_creates_one_notify_candidate`: `is_initial=False`, `changed=True` returns `notify/evidence_changed`, one evidence ID, six-hour expiry, and a message no longer than 500 characters.
- `test_unchanged_evidence_is_silent`: matching previous/current fingerprint returns `silent/unchanged`.
- `test_duplicate_notified_fingerprint_is_suppressed`: state fingerprint equal to evidence fingerprint returns `suppress/duplicate_evidence`.
- `test_cooldown_and_daily_limit_are_suppressed`: test cooldown and daily limit as two parameterized cases with reason codes `cooldown_active` and `daily_limit_reached`.
- `test_quiet_hours_defer_until_local_end`: at `2026-07-13T15:30:00Z` with Shanghai quiet hours `23:00-08:00`, return `defer/quiet_hours` and `deliver_after=2026-07-14T00:00:00Z`.
- `test_overnight_quiet_hours_are_supported`: assert both 23:30 and 07:30 local are quiet while 12:00 local is allowed.
- `test_active_realtime_run_defers_for_sixty_seconds`: `user_active=True` returns `defer/active_conversation` and `deliver_after=now+60s`.
- `test_notification_message_uses_rule_name_and_sanitized_summary`: include a token-shaped value in the summary, verify `sanitize_error_message` removes it, and verify the final message starts with the rule name.

Use fixed UTC datetimes and `Asia/Shanghai`; do not depend on wall-clock time.

- [ ] **Step 2: Run tests and verify missing-module failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_policy.py -q
```

Expected: FAIL importing `policy`.

- [ ] **Step 3: Implement deterministic evaluator**

```python
class DeterministicWakeEvaluator:
    def evaluate(self, *, rule: WakeRule, evidence: WakeEvidence, now: datetime) -> WakeDecision:
        if evidence.is_initial and not rule.condition.notify_on_initial:
            return WakeDecision(
                outcome="silent",
                severity="normal",
                reason_code="baseline_established",
                summary="Initial evidence baseline established.",
                evidence_ids=[evidence.evidence_id],
            )
        if not evidence.changed and not (evidence.is_initial and rule.condition.notify_on_initial):
            return WakeDecision(
                outcome="silent",
                severity="normal",
                reason_code="unchanged",
                summary="Evidence fingerprint is unchanged.",
                evidence_ids=[evidence.evidence_id],
            )
        message = sanitize_error_message(f"{rule.name}：{evidence.summary}")[:500]
        return WakeDecision(
            outcome="notify",
            severity="normal",
            reason_code="evidence_changed" if not evidence.is_initial else "initial_notification_enabled",
            summary=evidence.summary,
            user_message=message,
            evidence_ids=[evidence.evidence_id],
            expires_at=now + timedelta(hours=6),
        )
```

- [ ] **Step 4: Implement attention policy and notification builder**

`AttentionPolicy.evaluate()` order must be:

1. rule enabled;
2. decision is notify;
3. evidence fingerprint not already notified;
4. cooldown elapsed;
5. local-day notification count below limit;
6. decision not expired;
7. active realtime run -> defer 60 seconds;
8. quiet hours -> defer until local quiet end;
9. allow now.

Use `zoneinfo.ZoneInfo`; invalid timezone must fail closed with `suppress/policy_invalid_timezone`. Minimum severity comparison uses `low < normal < high` and suppresses below-rule severity.

`build_notification_envelope()` must use:

```python
idempotency_key = hashlib.sha256(
    f"{owner.tenant_id}|{owner.user_id}|{owner.project_id}|{rule.rule_id}|{evidence.fingerprint}|{rule.attention.channel}".encode()
).hexdigest()
```

Use destination reference `user:<user_id>` for the mock transport, `deliver_after` from the attention decision or `now`, and `expires_at` from the decision or `now + 6 hours`.

- [ ] **Step 5: Run policy tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_policy.py -q
```

Expected: all policy tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/assistant_agent/services/proactive_wake/policy.py src/assistant_agent/services/proactive_wake/__init__.py tests/test_proactive_wake_policy.py
git commit -m "Add proactive wake attention policy"
```

---

### Task 5: Add the read-only Gateway active-run boundary

**Files:**
- Create: `src/assistant_agent/services/proactive_wake/activity.py`
- Create: `tests/test_proactive_wake_gateway_activity.py`
- Modify: `src/assistant_agent/gateway/session.py`
- Modify: `src/assistant_agent/services/proactive_wake/__init__.py`

**Interfaces:**
- Consumes: existing `GatewaySessionService` and `GatewaySessionManager` active-run lifecycle.
- Produces: async `GatewaySessionService.has_active_run()`, async `GatewaySessionManager.has_active_run(user_id)`, `UserActivityReader`, `NullUserActivityReader`, and `GatewayUserActivityReader`.

- [ ] **Step 1: Write failing active-run tests**

In `tests/test_proactive_wake_gateway_activity.py`, use a blocking fake `RealtimeAgentBackend` and assert:

```python
async def test_gateway_activity_reader_tracks_active_run_not_idle_session() -> None:
    # Acquire a manager session for u1: before sending a user message,
    # reader.is_active(WakeOwner(user_id="u1")) is False.
    # Send message.user and block backend: reader returns True.
    # Release backend and collect run.end: reader returns False.


async def test_gateway_activity_reader_returns_false_for_unknown_user() -> None:
    # No manager entry is created as a side effect of the query.
```

- [ ] **Step 2: Run tests and verify missing methods**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_gateway_activity.py -q
```

Expected: FAIL because active-run query/adapter does not exist.

- [ ] **Step 3: Add public async snapshots without exposing private state**

Add to `GatewaySessionService`:

```python
async def has_active_run(self) -> bool:
    """Return whether this user service currently owns any active run."""
    async with self._lock:
        return bool(self._active_by_session)
```

Add to `GatewaySessionManager`:

```python
async def has_active_run(self, user_id: str) -> bool:
    """Return active-run state without creating or touching a session."""
    async with self._lock:
        entry = self._entries.get(user_id)
    if entry is None:
        return False
    return await entry.service.has_active_run()
```

Do not reuse `has_active_session()`; an idle/reconnected session is not an active run.

- [ ] **Step 4: Implement the activity protocol and adapters**

```python
class UserActivityReader(Protocol):
    async def is_active(self, owner: WakeOwner) -> bool:
        raise NotImplementedError


class NullUserActivityReader:
    async def is_active(self, owner: WakeOwner) -> bool:
        return False


class GatewayUserActivityReader:
    def __init__(self, manager: GatewaySessionManager) -> None:
        self.manager = manager

    async def is_active(self, owner: WakeOwner) -> bool:
        return await self.manager.has_active_run(owner.user_id)
```

- [ ] **Step 5: Run Gateway activity and regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_gateway_activity.py tests/test_gateway_session.py tests/test_gateway_api.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/assistant_agent/gateway/session.py src/assistant_agent/services/proactive_wake/activity.py src/assistant_agent/services/proactive_wake/__init__.py tests/test_proactive_wake_gateway_activity.py
git commit -m "Expose Gateway active-run state"
```

---

### Task 6: Add atomic outcome/outbox persistence and the coordinator

**Files:**
- Create: `src/assistant_agent/services/proactive_wake/coordinator.py`
- Create: `tests/test_proactive_wake_coordinator.py`
- Modify: `src/assistant_agent/services/proactive_wake/store.py`
- Modify: `src/assistant_agent/services/proactive_wake/__init__.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: `SQLiteProactiveWakeStore.complete_outcome()`, outbox query primitives, and `ProactiveWakeCoordinator.run_rule(rule_id, owner, signal) -> ProactiveWakeRunResult`.

- [ ] **Step 1: Write failing coordinator tests**

Use a sequence fake read-only tool whose `model_observation` returns baseline, the same baseline, then changed data. Implement these exact tests:

- `test_first_probe_establishes_baseline_without_notification`: tool count 1, run `baseline_established`, state fingerprint populated, outbox empty.
- `test_same_evidence_stays_silent_without_notification`: tool count 2, second run `unchanged`, fingerprint unchanged, outbox empty.
- `test_changed_evidence_enqueues_exactly_one_notification`: tool count 3, run `enqueued`, outbox length 1, envelope fingerprint equals new state fingerprint.
- `test_duplicate_event_key_skips_probe`: two signals share `event_key`; second run is `deduplicated`, tool count remains 1.
- `test_active_realtime_run_enqueues_deferred_notification`: activity reader returns true; envelope `deliver_after=now+60s`, run `enqueued`.
- `test_invalid_or_write_rule_becomes_config_error_without_mutating_enabled`: replace registry policy after storage; run is `config_error`, tool count 0, persisted rule remains enabled.
- `test_probe_failure_keeps_previous_fingerprint_and_creates_no_notification`: failing result creates `probe_failed`; fingerprint/outbox remain unchanged.
- `test_missing_or_cross_user_rule_does_not_probe`: both absent and wrong-owner calls raise `rule_not_found`; tool count 0.
- `test_signal_owner_mismatch_does_not_probe`: raise `signal_owner_mismatch`; tool count 0.
- `test_provider_event_source_or_type_mismatch_does_not_probe`: parameterize wrong source and wrong type; raise `signal_not_matched`; tool count 0.
- `test_persisted_run_and_sqlite_file_exclude_raw_tool_payload`: fake result contains `raw-private-calendar-token` only in `data/raw_data_ref`; assert it is absent from loaded WakeRun, outbox and SQLite file bytes.
- `test_concurrent_distinct_signals_for_same_rule_are_serialized`: run two distinct signals with `asyncio.gather`; a blocking fake tool records maximum concurrency 1, final state matches the second completed fingerprint, and identical resulting evidence leaves at most one outbox row.

Assertions must include tool call count, WakeRun status/reason, persisted fingerprint, outbox count and unchanged `rule.enabled`.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_coordinator.py -q
```

Expected: FAIL importing `coordinator` or missing outbox methods.

- [ ] **Step 3: Extend SQLite schema with durable outbox and attempts**

Add these tables through schema version 2 migration:

```sql
CREATE TABLE IF NOT EXISTS notification_outbox (
    delivery_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT,
    user_id TEXT NOT NULL,
    project_id TEXT,
    rule_id TEXT NOT NULL,
    status TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_reason_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
ON notification_outbox (status, available_at, lease_until);

CREATE TABLE IF NOT EXISTS notification_attempts (
    attempt_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (delivery_id) REFERENCES notification_outbox(delivery_id) ON DELETE CASCADE
);
```

Implement:

- `complete_outcome(*, run: WakeRun, state: WakeRuleState, notification: NotificationEnvelope | None) -> tuple[WakeRun, NotificationEnvelope | None]`
- `list_outbox(owner: WakeOwner | None = None) -> list[NotificationEnvelope]`

`complete_outcome()` must update run JSON/status, update rule state, and insert the notification with `ON CONFLICT(idempotency_key) DO NOTHING` in one SQLite transaction. When the insert conflicts, return the existing envelope and do not increment the notification count twice.

- [ ] **Step 4: Implement coordinator orchestration**

Use these exact public signatures:

- `ProactiveWakeCoordinator.__init__(*, store: SQLiteProactiveWakeStore, rule_validator: ProactiveRuleValidator, probe_runner: GovernedProbeRunner, evaluator: DeterministicWakeEvaluator | None = None, attention_policy: AttentionPolicy | None = None, activity_reader: UserActivityReader | None = None, now_fn: Callable[[], datetime] = utc_now) -> None`
- `save_rule(rule: WakeRule) -> WakeRule`
- `run_rule(*, rule_id: str, owner: WakeOwner, signal: WakeSignal) -> ProactiveWakeRunResult` as an async method.

Required order:

1. `save_rule()` must call `rule_validator.validate()` before `store.save_rule()` and raise `ProactiveWakeError` with the validator code on rejection.
2. Load owner-scoped rule; raise `ProactiveWakeError(code="rule_not_found")` if absent, without creating a WakeRun or calling a tool.
3. Require `signal.owner == owner == rule.owner`; mismatch raises `signal_owner_mismatch` before `begin_run()`.
4. For `provider_event`, require signal source and event type to be declared by the rule; mismatch raises `signal_not_matched` before probe. Manual and reconciliation signals are trusted service triggers and do not use provider event matching.
5. Acquire a process-local `asyncio.Lock` keyed by stable owner fields plus rule ID. Keep the lock through `begin_run`, probe, state comparison and atomic completion; never hold a Gateway lock while waiting.
6. `begin_run()` before any probe; return immediately for deduplicated signal.
7. Validate the persisted rule again at runtime because registry/allowlist policy may have changed; on rejection store `config_error`, preserve rule enabled state and skip probe.
8. Store `probing` status.
9. Run sync ToolExecutor work using `await asyncio.to_thread(probe_runner.run, rule, signal)` so the event loop stays responsive.
10. On probe failure store `probe_failed`, preserve previous fingerprint and skip notification.
11. Build evidence from previous state.
12. Evaluate deterministic decision.
13. Query `activity_reader.is_active(owner)` and apply AttentionPolicy.
14. Set next reconciliation time to `now + rule.trigger.reconcile_interval_s` for a successful probe.
15. For baseline/unchanged/suppressed outcomes, atomically persist run and state with no notification.
16. For allow/defer, build one envelope and atomically persist run/state/outbox.

Update `last_notified_at`, `last_notified_fingerprint` and daily count only when a unique notification is enqueued, not when the model/policy merely produces a candidate.

- [ ] **Step 5: Run coordinator and dependency tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_coordinator.py tests/test_proactive_wake_store.py tests/test_proactive_wake_probe.py tests/test_proactive_wake_policy.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/assistant_agent/services/proactive_wake/coordinator.py src/assistant_agent/services/proactive_wake/store.py src/assistant_agent/services/proactive_wake/__init__.py tests/test_proactive_wake_coordinator.py
git commit -m "Add deterministic proactive wake coordinator"
```

---

### Task 7: Add durable mock delivery and retry recovery

**Files:**
- Create: `src/assistant_agent/services/proactive_wake/delivery.py`
- Create: `tests/test_proactive_wake_delivery.py`
- Modify: `src/assistant_agent/services/proactive_wake/store.py`
- Modify: `src/assistant_agent/services/proactive_wake/__init__.py`

**Interfaces:**
- Consumes: outbox rows from Task 6 and `UserActivityReader`.
- Produces: `ProactiveNotificationTransport`, `MockProactiveNotificationTransport`, `NotificationDeliveryWorker.drain_once()`, plus store lease/sent/retry/defer/expired methods.

- [ ] **Step 1: Write failing delivery tests**

Implement these exact tests:

- `test_due_notification_is_leased_sent_and_not_reclaimed`: one due envelope becomes `sent`, transport count 1, second drain returns empty.
- `test_transport_failure_retries_without_rerunning_probe`: first transport result rejects, row becomes `retry_wait`, attempt count 1, coordinator/probe count unchanged; advancing to retry time sends the existing envelope.
- `test_expired_lease_is_reclaimed_after_restart`: claim without finishing, recreate store/worker after lease expiry, then send the same delivery ID once.
- `test_active_user_defers_delivery_without_counting_failure`: row returns to `retry_wait`, `attempt_count=0`, available time advances 60 seconds, transport count 0.
- `test_expired_notification_is_marked_expired_and_not_sent`: status `expired`, transport count 0.
- `test_max_attempts_moves_notification_to_dead_letter`: three rejected sends with `max_attempts=3` end at `dead_letter`, attempt count 3.
- `test_same_idempotency_key_cannot_create_second_outbox_row`: two atomic outcomes with the same key leave one row and return the original delivery ID.

Use a counting probe fixture only to prove delivery retries never call it; the worker itself must have no coordinator/probe dependency.

- [ ] **Step 2: Run tests and verify missing-module failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_delivery.py -q
```

Expected: FAIL importing `delivery`.

- [ ] **Step 3: Add exact outbox store methods**

Implement these exact store signatures:

- `claim_due_notifications(*, now: datetime, lease_s: int = 30, limit: int = 20) -> list[NotificationEnvelope]`
- `mark_notification_sent(delivery_id: str, *, provider_message_id: str | None, now: datetime) -> NotificationEnvelope`
- `defer_notification(delivery_id: str, *, available_at: datetime, reason_code: str, now: datetime) -> NotificationEnvelope`
- `mark_notification_failed(delivery_id: str, *, error_code: str, retry_at: datetime | None, now: datetime, max_attempts: int) -> NotificationEnvelope`
- `mark_notification_expired(delivery_id: str, *, now: datetime) -> NotificationEnvelope`

Claim in one transaction by selecting `queued/retry_wait` rows with `available_at <= now`, plus `leased` rows with expired `lease_until`, then update selected rows to `leased`. Record every actual transport attempt in `notification_attempts`; active-user deferral is not a transport attempt.

Every outbox transition must update both indexed columns and `envelope_json` in the same transaction so `list_outbox()` and reclaimed leases return the same status, attempt count, lease, provider message ID and reason code.

- [ ] **Step 4: Implement transport and worker**

```python
class ProactiveNotificationTransport(Protocol):
    async def send(self, notification: NotificationEnvelope) -> DeliveryResult:
        raise NotImplementedError


class MockProactiveNotificationTransport:
    def __init__(self, results: list[DeliveryResult] | None = None) -> None:
        self.results = list(results or [])
        self.sent: list[NotificationEnvelope] = []

    async def send(self, notification: NotificationEnvelope) -> DeliveryResult:
        self.sent.append(notification)
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(accepted=True, provider_message_id=f"mock:{notification.delivery_id}")


```

Implement these exact worker signatures:

- `NotificationDeliveryWorker.__init__(*, store: SQLiteProactiveWakeStore, transport: ProactiveNotificationTransport, activity_reader: UserActivityReader | None = None, now_fn: Callable[[], datetime] = utc_now, max_attempts: int = 3) -> None`
- `drain_once(*, limit: int = 20) -> list[NotificationEnvelope]` as an async method.

Worker behavior:

- Claim due envelopes.
- If `expires_at <= now`, mark expired without transport call.
- If user active, defer for 60 seconds without incrementing attempt count.
- Otherwise call transport exactly once.
- On accepted result, mark sent.
- On failure, retry at `now + min(300, 5 * 2**attempt_count)` until `max_attempts`, then dead-letter.
- Never import or call coordinator, ToolExecutor, AgentGraphRuntime or an LLM adapter.

- [ ] **Step 5: Run delivery tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_delivery.py tests/test_proactive_wake_coordinator.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/assistant_agent/services/proactive_wake/delivery.py src/assistant_agent/services/proactive_wake/store.py src/assistant_agent/services/proactive_wake/__init__.py tests/test_proactive_wake_delivery.py
git commit -m "Add durable proactive notification delivery"
```

---

### Task 8: Add the offline demo, authority-document updates and final gate

**Files:**
- Create: `scripts/run_proactive_wake_demo.py`
- Create: `tests/test_proactive_wake_demo.py`
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/personal-realtime-ai-assistant-roadmap.md`

**Interfaces:**
- Consumes: the complete Phase 1 service package.
- Produces: one deterministic local demo and current-architecture documentation.

- [ ] **Step 1: Write failing demo test**

```python
import json

from scripts.run_proactive_wake_demo import main


def test_proactive_wake_demo_establishes_baseline_then_delivers_one_change(tmp_path, capsys) -> None:
    exit_code = main(["--db", str(tmp_path / "wake.sqlite3")])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["offline"] is True
    assert payload["llm_calls"] == 0
    assert payload["probe_calls"] == 2
    assert payload["baseline_status"] == "baseline_established"
    assert payload["changed_status"] == "enqueued"
    assert payload["delivered_count"] == 1
    assert payload["delivery_status"] == "sent"
```

- [ ] **Step 2: Run test and verify missing script failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_proactive_wake_demo.py -q
```

Expected: FAIL importing `scripts.run_proactive_wake_demo`.

- [ ] **Step 3: Implement the deterministic demo**

The script must:

- define an in-process `calendar.search_events` mock tool with explicit `external_read`, approval `never`, independent execution, no resource writes, and sanitized `model_observation`;
- return baseline event data on first probe and changed event time on second probe;
- construct `ToolRegistry`, explicit allowlist `{"calendar.search_events"}`, SQLite store, null activity reader, coordinator and mock transport;
- save one structured `mode=changed` rule;
- save it through `ProactiveWakeCoordinator.save_rule()` so creation-time governance is exercised;
- run one manual baseline signal and one distinct manual change signal;
- drain the outbox once;
- print only the JSON summary asserted above;
- never load `.env`, call a provider, start a server, create a background ticker, or write outside the provided `--db` path.

Provide `main(argv: list[str] | None = None) -> int` and `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 4: Update authority docs with implemented facts only**

Add concise sections stating:

- `docs/gateway-architecture.md`: `GatewaySessionService.has_active_run()` and `GatewaySessionManager.has_active_run(user_id)` are read-only snapshots used to defer proactive delivery; they do not create sessions or let proactive work interrupt a run.
- `docs/tool-calling-architecture.md`: Proactive Phase 1 probes are explicit-rule, explicit-allowlist read-only calls that still pass ToolPolicyInterpreter, ActionValidator, ToolExecutor and ToolRegistry; no LLM chooses the tool.
- `docs/personal-realtime-ai-assistant-roadmap.md`: deterministic Phase 1 is a narrow local slice; semantic wake, real event ingest, real notification transport and generic scheduler remain deferred.

Do not describe Phase 2/3 as implemented.

- [ ] **Step 5: Run the focused Phase 1 suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_proactive_wake_schemas.py \
  tests/test_proactive_wake_store.py \
  tests/test_proactive_wake_probe.py \
  tests/test_proactive_wake_policy.py \
  tests/test_proactive_wake_gateway_activity.py \
  tests/test_proactive_wake_coordinator.py \
  tests/test_proactive_wake_delivery.py \
  tests/test_proactive_wake_demo.py
```

Expected: all Proactive Wake tests PASS.

- [ ] **Step 6: Run affected subsystem regressions**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_gateway_session.py \
  tests/test_gateway_api.py \
  tests/test_tool_policy_interpreter.py \
  tests/test_tool_executor.py \
  tests/test_action_validator.py
```

Expected: all selected Gateway/tool-governance tests PASS.

- [ ] **Step 7: Run environment, fast-suite and diff checks**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- docs src tests scripts
```

Expected: environment check succeeds, fast tests report zero failures, and `git diff --check` prints no errors.

- [ ] **Step 8: Run the demo manually**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_proactive_wake_demo.py --db /tmp/assistant-agent-proactive-wake-demo.sqlite3
```

Expected: JSON reports `offline=true`, `llm_calls=0`, baseline established, one changed notification enqueued, and one mock delivery sent.

- [ ] **Step 9: Commit final Phase 1 integration and docs**

```bash
git add scripts/run_proactive_wake_demo.py tests/test_proactive_wake_demo.py docs/gateway-architecture.md docs/tool-calling-architecture.md docs/personal-realtime-ai-assistant-roadmap.md
git commit -m "Document deterministic proactive wake phase one"
```

---

## Phase 1 Completion Gate

Do not claim Phase 1 complete unless fresh verification proves all of the following:

- no rule means no probe and no notification;
- first observation establishes a baseline and remains silent by default;
- unchanged observation produces no notification;
- changed observation creates at most one outbox row per fingerprint/channel;
- duplicate signals do not run the probe again;
- write/unknown/unallowlisted tools never execute;
- all successful probes pass ActionValidator and ToolExecutor;
- active realtime run defers both enqueue delivery time and actual worker send;
- worker retries never rerun probe/decision logic;
- process restart can reclaim an expired delivery lease without duplicating the outbox row;
- no LLM, real Provider, real channel, background ticker, memory write or conversation-history projection occurs;
- focused tests, affected Gateway/tool tests, environment check, fast suite and diff check all pass.

## Plan Self-Review

### Spec coverage

| Phase 1 requirement | Implementing task |
| --- | --- |
| Stable owner and structured rule/signal/evidence/run/delivery contracts | Task 1 |
| Separate SQLite rule/state/dedup/run persistence | Task 2 |
| Explicit allowlist plus ToolPolicyInterpreter/ActionValidator/ToolExecutor/ToolRegistry chain | Task 3 |
| Prompt-safe observation, canonical fingerprint and silent baseline | Tasks 3–4 |
| Deterministic changed decision and bounded notification text | Task 4 |
| Quiet hours, cooldown, daily limit, duplicate evidence and active-run deferral | Tasks 4–5 |
| Gateway active run queried without creating/touching sessions | Task 5 |
| Per-rule process-local serialization and atomic run/state/outbox outcome | Task 6 |
| Durable leases, retry, expiry, dead-letter and mock transport | Task 7 |
| Offline end-to-end proof and authority-document synchronization | Task 8 |

### Intentional gaps

The following approved-spec sections are intentionally outside this first independently testable plan: semantic `PROACTIVE_CHECK`, prompt/context rendering, real event ingress, real read-only Provider, real App/IM transport, application ACK, automatic reconciliation ticker, cross-process WakeRun ownership, conversation-history projection and long-term memory access. Each is named in the follow-on boundaries below and must not be partially introduced by a Phase 1 task.

### Type and naming consistency

- All persistent ownership uses `WakeOwner`; runtime request identity is reconstructed in `GovernedProbeRunner`.
- `WakeRule.condition.mode` accepts the future schema value `semantic`, while `ProactiveRuleValidator` rejects it in Phase 1.
- `WakeEvidence.fingerprint`, `WakeRuleState.last_fingerprint`, `WakeRuleState.last_notified_fingerprint` and `NotificationEnvelope.evidence_fingerprint` use the same SHA-256 canonical evidence value.
- `AttentionDecision.deliver_after` maps to `NotificationEnvelope.deliver_after` and SQLite `available_at`.
- Every outbox transition updates both indexed columns and `envelope_json`.
- `sent` means mock transport accepted the notification; Phase 1 does not claim user read/acknowledgment.

## Explicit Follow-on Plan Boundaries

Create separate specs/plans after Phase 1 evidence is accepted:

1. **Semantic Proactive Wake:** `PROACTIVE_CHECK` system profile, minimal context renderer, structured one-shot `WakeDecision`, prompt-injection tests and LLM budgets.
2. **Provider Event and Channel Pilot:** authenticated event ingest, one real read-only Provider, one real App/IM transport, provider_smoke/pilot opt-in and delivery ACK semantics.
3. **Automatic Reconciler:** only if product evidence justifies a process-owned ticker; it must consume the existing due-rule interface and must not become a generic scheduler.
