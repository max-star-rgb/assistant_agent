# Gateway QueuePolicy and Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, cancellable per-session queued turns and a fair process-wide Gateway run admission cap without changing the assistant loop or bypassing existing tool governance.

**Architecture:** `GatewaySessionService` owns user/session ordering and queued-turn lifecycle. A manager-owned `GatewayRunAdmissionController` owns process-wide reservations, FIFO waiters, and active permits; only a session head may request a permit. `GatewayAgentAdapter` and `AgentGraphRuntime` remain unchanged.

**Tech Stack:** Python 3.11, `asyncio`, dataclasses, existing Gateway frame/transport contracts, `unittest.IsolatedAsyncioTestCase`, pytest, mock/local/offline backends.

## Global Constraints

- Default queue mode is `followup`; only existing explicit `interrupt` control may preempt it.
- Initial defaults are `max_pending_per_session=8`, `max_queued_turns_global=64`, `max_active_runs=4`, `queue_wait_timeout_ms=120000`, `dedupe_ttl_s=300`, and `dedupe_max_entries_per_user=1024`.
- Overflow policy is exactly `reject_newest`; do not summarize, merge, or evict an accepted turn.
- Do not implement `collect`, `steer`, durable queue recovery, distributed admission, cron lanes, Proactive Wake scheduling, or Hermes background tasks.
- Do not add a second agent loop or modify the public realtime backend contracts.
- Tool calls remain behind `ActionValidator -> ToolExecutor -> ToolRegistry`; cancellation does not roll back committed side effects.
- Keep all tests mock/local/offline. Do not install dependencies or call a real Provider.
- Preserve unrelated dirty-worktree changes. Stage only files named by the current task.
- Follow TDD: add a failing test, observe the expected failure, implement minimum behavior, then run the focused regression set.
- Update `docs/gateway-architecture.md` only after implementation and tests establish current behavior.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/assistant_agent/gateway/queueing.py` | Queue policy, queued-turn data, dedupe index, reservations, admission tickets, permits, FIFO controller and snapshots. |
| `src/assistant_agent/gateway/session.py` | Per-session head/pending lifecycle, history-at-start, queue projection, cancel/interrupt/timeout and permit release. |
| `src/assistant_agent/gateway/protocol.py` | Additive `RUN_QUEUED` constant. |
| `src/assistant_agent/gateway/bridge.py` | Hangup/disconnect session-wide cleanup source. |
| `src/assistant_agent/gateway/__init__.py` | Public policy/controller/frame exports. |
| `src/assistant_agent/api/gateway_runtime.py` | Queue/admission environment configuration. |
| `src/assistant_agent/services/gateway_turn_facade.py` | One reader per user endpoint, run-id frame demux, intermediate queue frames and timeout cancellation. |
| `tests/test_gateway_queueing.py` | Pure policy/controller/dedupe tests. |
| `tests/test_gateway_session.py` | Queue lifecycle, history, cancel, timeout, interrupt, dedupe and overflow. |
| `tests/test_gateway.py` | Cross-session manager admission and Bridge cleanup. |
| `tests/test_gateway_turn_facade.py` | Queue-frame collection and timeout cleanup. |
| `tests/test_gateway_api.py` | Environment and additive WebSocket regression. |
| `docs/gateway-architecture.md` | Canonical implemented queue/admission contract. |

---

### Task 1: Queue Policy and Fair Admission Controller

**Files:**
- Create: `src/assistant_agent/gateway/queueing.py`
- Create: `tests/test_gateway_queueing.py`

**Interfaces:**
- Consumes: Python stdlib and `assistant_agent.gateway.transport.Endpoint`.
- Produces: `GatewayQueuePolicy`, `QueuedTurn`, `QueueOverflowError`, `QueueReservation`, `AdmissionTicket`, `RunPermit`, `AdmissionSnapshot`, `GatewayRunAdmissionController`.
- `reserve(...) -> QueueReservation` reserves one global queued slot.
- `request_permit(reservation) -> AdmissionTicket`; callers await `ticket.ready`.
- Callers release the granted permit exactly once in backend finalization.

- [ ] **Step 1: Write failing policy and controller tests**

Create `tests/test_gateway_queueing.py`:

```python
from __future__ import annotations

import asyncio
import unittest

from assistant_agent.gateway.queueing import (
    GatewayQueuePolicy,
    GatewayRunAdmissionController,
    QueueOverflowError,
)


class GatewayQueuePolicyTests(unittest.TestCase):
    def test_defaults_are_bounded(self) -> None:
        policy = GatewayQueuePolicy()
        assert policy.mode == "followup"
        assert policy.max_pending_per_session == 8
        assert policy.max_queued_turns_global == 64
        assert policy.max_active_runs == 4
        assert policy.queue_wait_timeout_ms == 120_000
        assert policy.dedupe_ttl_s == 300.0
        assert policy.dedupe_max_entries_per_user == 1024
        assert policy.overflow_policy == "reject_newest"

    def test_non_positive_limits_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_active_runs must be positive"):
            GatewayQueuePolicy(max_active_runs=0)


class GatewayRunAdmissionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_waiters_respect_active_cap(self) -> None:
        controller = GatewayRunAdmissionController(
            GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=4)
        )
        first = await controller.reserve(user_id="u1", session_id="s1", turn_id="t1", run_id="r1")
        second = await controller.reserve(user_id="u2", session_id="s2", turn_id="t2", run_id="r2")
        first_ticket = await controller.request_permit(first)
        second_ticket = await controller.request_permit(second)

        first_permit = await asyncio.wait_for(first_ticket.ready, timeout=0.2)
        assert second_ticket.ready.done() is False
        assert (await controller.snapshot()).active_runs == 1
        await controller.release_permit(first_permit)

        second_permit = await asyncio.wait_for(second_ticket.ready, timeout=0.2)
        assert second_permit.run_id == "r2"
        await controller.release_permit(second_permit)
        assert (await controller.snapshot()).active_runs == 0

    async def test_global_queue_overflow_rejects_newest(self) -> None:
        controller = GatewayRunAdmissionController(
            GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=1)
        )
        await controller.reserve(user_id="u1", session_id="s1", turn_id="t1", run_id="r1")
        with self.assertRaises(QueueOverflowError) as raised:
            await controller.reserve(user_id="u2", session_id="s2", turn_id="t2", run_id="r2")
        assert raised.exception.scope == "global"
        assert (await controller.snapshot()).queued_turns == 1

    async def test_cancel_waiting_ticket_releases_reservation(self) -> None:
        controller = GatewayRunAdmissionController(
            GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=3)
        )
        first = await controller.reserve(user_id="u1", session_id="s1", turn_id="t1", run_id="r1")
        second = await controller.reserve(user_id="u2", session_id="s2", turn_id="t2", run_id="r2")
        first_ticket = await controller.request_permit(first)
        second_ticket = await controller.request_permit(second)
        first_permit = await first_ticket.ready
        assert await controller.cancel_ticket(second_ticket) is True
        assert (await controller.snapshot()).queued_turns == 0
        await controller.release_permit(first_permit)

    async def test_release_permit_is_idempotent(self) -> None:
        controller = GatewayRunAdmissionController(GatewayQueuePolicy(max_active_runs=1))
        reservation = await controller.reserve(user_id="u1", session_id="s1", turn_id="t1", run_id="r1")
        ticket = await controller.request_permit(reservation)
        permit = await ticket.ready
        assert await controller.release_permit(permit) is True
        assert await controller.release_permit(permit) is False
        assert await controller.release_reservation(reservation) is False
```

- [ ] **Step 2: Run the focused test and verify import failure**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_queueing.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'assistant_agent.gateway.queueing'`.

- [ ] **Step 3: Implement policy, queued-turn types and controller**

Create `src/assistant_agent/gateway/queueing.py` with these public shapes:

```python
"""Bounded in-process queue policy and Gateway run admission."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.gateway.transport import Endpoint

QueueMode = Literal["followup", "interrupt"]
QueueReason = Literal["session_busy", "global_capacity"]


@dataclass(frozen=True)
class GatewayQueuePolicy:
    mode: QueueMode = "followup"
    max_pending_per_session: int = 8
    max_queued_turns_global: int = 64
    max_active_runs: int = 4
    queue_wait_timeout_ms: int = 120_000
    dedupe_ttl_s: float = 300.0
    dedupe_max_entries_per_user: int = 1024
    overflow_policy: Literal["reject_newest"] = "reject_newest"

    def __post_init__(self) -> None:
        if self.mode not in {"followup", "interrupt"}:
            raise ValueError("mode must be followup or interrupt")
        for name in (
            "max_pending_per_session",
            "max_queued_turns_global",
            "max_active_runs",
            "queue_wait_timeout_ms",
            "dedupe_max_entries_per_user",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.dedupe_ttl_s <= 0:
            raise ValueError("dedupe_ttl_s must be positive")
        if self.overflow_policy != "reject_newest":
            raise ValueError("overflow_policy must be reject_newest")


@dataclass
class QueueReservation:
    reservation_id: str
    user_id: str
    session_id: str
    turn_id: str
    run_id: str
    released: bool = False


@dataclass
class RunPermit:
    permit_id: str
    run_id: str
    acquired_at_monotonic: float
    released: bool = False


@dataclass
class AdmissionTicket:
    ticket_id: str
    reservation: QueueReservation
    ready: asyncio.Future[RunPermit]
    enqueued_at_monotonic: float
    cancelled: bool = False
    granted: bool = False


@dataclass(frozen=True)
class AdmissionSnapshot:
    queued_turns: int
    waiting_turns: int
    active_runs: int
    max_queued_turns: int
    max_active_runs: int


@dataclass
class QueuedTurn:
    user_id: str
    session_id: str
    turn_id: str
    run_id: str
    endpoint: Endpoint
    payload: dict[str, Any]
    user_text: str
    accepted_at_monotonic: float
    accepted_at_unix_ms: int
    queue_deadline_monotonic: float
    client_message_id: str | None
    payload_fingerprint: str
    runtime_interrupt: bool = False
    state: str = "received"
    queue_reason: QueueReason | None = None
    reservation: QueueReservation | None = None
    admission_ticket: AdmissionTicket | None = None
    timeout_task: asyncio.Task[None] | None = None
    dispatch_task: asyncio.Task[None] | None = None


class QueueOverflowError(RuntimeError):
    def __init__(self, *, scope: Literal["session", "global"], limit: int) -> None:
        super().__init__(f"gateway {scope} queue limit reached: {limit}")
        self.scope = scope
        self.limit = limit
```

Implement `GatewayRunAdmissionController` with these exact methods:

```python
class GatewayRunAdmissionController:
    def __init__(self, policy: GatewayQueuePolicy) -> None:
        self.policy = policy
        self._lock = asyncio.Lock()
        self._reservations: dict[str, QueueReservation] = {}
        self._waiters: deque[AdmissionTicket] = deque()
        self._permits: dict[str, RunPermit] = {}
        self._closed = False

    async def reserve(self, *, user_id: str, session_id: str, turn_id: str, run_id: str) -> QueueReservation:
        async with self._lock:
            if self._closed:
                raise RuntimeError("gateway admission controller is closed")
            if len(self._reservations) >= self.policy.max_queued_turns_global:
                raise QueueOverflowError(scope="global", limit=self.policy.max_queued_turns_global)
            reservation = QueueReservation(
                reservation_id=str(uuid.uuid4()),
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    async def request_permit(self, reservation: QueueReservation) -> AdmissionTicket:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._closed:
                raise RuntimeError("gateway admission controller is closed")
            if reservation.released or reservation.reservation_id not in self._reservations:
                raise RuntimeError("queue reservation is not active")
            ticket = AdmissionTicket(
                ticket_id=str(uuid.uuid4()),
                reservation=reservation,
                ready=loop.create_future(),
                enqueued_at_monotonic=time.monotonic(),
            )
            self._waiters.append(ticket)
            grants = self._collect_grants_locked()
        self._resolve_grants(grants)
        return ticket

    async def cancel_ticket(self, ticket: AdmissionTicket) -> bool:
        async with self._lock:
            if ticket.cancelled or ticket.granted:
                return False
            ticket.cancelled = True
            self._waiters = deque(item for item in self._waiters if item is not ticket)
            self._release_reservation_locked(ticket.reservation)
            grants = self._collect_grants_locked()
        if not ticket.ready.done():
            ticket.ready.cancel()
        self._resolve_grants(grants)
        return True

    async def release_reservation(self, reservation: QueueReservation) -> bool:
        async with self._lock:
            return self._release_reservation_locked(reservation)

    async def release_permit(self, permit: RunPermit) -> bool:
        async with self._lock:
            if permit.released or permit.permit_id not in self._permits:
                return False
            permit.released = True
            self._permits.pop(permit.permit_id, None)
            grants = self._collect_grants_locked()
        self._resolve_grants(grants)
        return True

    async def snapshot(self) -> AdmissionSnapshot:
        async with self._lock:
            return AdmissionSnapshot(
                queued_turns=len(self._reservations),
                waiting_turns=sum(1 for item in self._waiters if not item.cancelled),
                active_runs=len(self._permits),
                max_queued_turns=self.policy.max_queued_turns_global,
                max_active_runs=self.policy.max_active_runs,
            )

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            waiters = list(self._waiters)
            self._waiters.clear()
            for ticket in waiters:
                ticket.cancelled = True
                self._release_reservation_locked(ticket.reservation)
        for ticket in waiters:
            if not ticket.ready.done():
                ticket.ready.cancel()

    def _collect_grants_locked(self) -> list[tuple[asyncio.Future[RunPermit], RunPermit]]:
        grants: list[tuple[asyncio.Future[RunPermit], RunPermit]] = []
        while self._waiters and len(self._permits) < self.policy.max_active_runs:
            ticket = self._waiters.popleft()
            if ticket.cancelled:
                continue
            self._release_reservation_locked(ticket.reservation)
            permit = RunPermit(
                permit_id=str(uuid.uuid4()),
                run_id=ticket.reservation.run_id,
                acquired_at_monotonic=time.monotonic(),
            )
            ticket.granted = True
            self._permits[permit.permit_id] = permit
            grants.append((ticket.ready, permit))
        return grants

    def _release_reservation_locked(self, reservation: QueueReservation) -> bool:
        if reservation.released or reservation.reservation_id not in self._reservations:
            return False
        reservation.released = True
        self._reservations.pop(reservation.reservation_id, None)
        return True

    @staticmethod
    def _resolve_grants(grants: list[tuple[asyncio.Future[RunPermit], RunPermit]]) -> None:
        for future, permit in grants:
            if not future.done():
                future.set_result(permit)
```

- [ ] **Step 4: Run tests and whitespace validation**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_queueing.py`

Run: `git diff --check -- src/assistant_agent/gateway/queueing.py tests/test_gateway_queueing.py`

Expected: tests pass; whitespace command emits no output.

- [ ] **Step 5: Commit queue primitives**

```bash
git add src/assistant_agent/gateway/queueing.py tests/test_gateway_queueing.py
git commit -m "feat(gateway): add bounded run admission controller"
```

---

### Task 2: Session Queue and Process-Wide Admission Integration

**Files:**
- Modify: `src/assistant_agent/gateway/protocol.py:8-34`
- Modify: `src/assistant_agent/gateway/__init__.py:3-65`
- Modify: `src/assistant_agent/gateway/session.py:34-402,710-1038`
- Modify: `tests/test_gateway_session.py:659-797`
- Modify: `tests/test_gateway.py:36-130`

**Interfaces:**
- Consumes: all Task 1 types.
- Produces: `RUN_QUEUED`, `GatewaySessionService(queue_policy=..., admission_controller=...)`, `GatewaySessionManager(queue_policy=..., admission_controller=...)`.
- Manager shares one controller across every default-created user service.
- Service tracks one `_current_by_session` head and a `deque` of followups.

- [ ] **Step 1: Write failing queue-frame and shared-cap tests**

Update `test_message_user_queues_behind_active_run_without_interrupt`:

```python
assert [received["type"] for received in frames] == [
    "run.started",
    "run.queued",
    "run.end",
    "run.started",
    "stream.chunk",
    "run.end",
]
queued = frames[1]
assert queued["payload"]["reason"] == "session_busy"
assert queued["payload"]["queue_depth"] == 1
assert queued["run_id"] is not None
assert queued["turn_id"] is not None
assert backend.requests[1].metadata["runtime"]["history"] == ["first", "second"]
```

Add to `tests/test_gateway.py`:

```python
async def test_manager_shares_one_admission_controller_across_users(self) -> None:
    class BlockingBackend:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.active = 0
            self.max_seen = 0

        async def run_turn(self, request, *, event_sink=None, cancel_token=None):
            self.active += 1
            self.max_seen = max(self.max_seen, self.active)
            try:
                await self.release.wait()
                return RealtimeAgentResult(status="completed", run_id=request.run_id)
            finally:
                self.active -= 1

    backend = BlockingBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        queue_policy=GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=4),
        start_reaper=False,
    )
    first = await manager.acquire(user_id="u1")
    second = await manager.acquire(user_id="u2")
    await first.endpoint.send(frame(type="message.user", session_id="s1", payload={"text": "one"}))
    await second.endpoint.send(frame(type="message.user", session_id="s2", payload={"text": "two"}))
    first_started = await _read_until(first.endpoint, "run.started")
    second_queued = await _read_until(second.endpoint, "run.queued")
    assert first_started["session_id"] == "s1"
    assert second_queued["payload"]["reason"] == "global_capacity"
    assert backend.max_seen == 1
    backend.release.set()
    await manager.close()
```

- [ ] **Step 2: Run focused tests and observe missing behavior**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_session.py::GatewaySessionTests::test_message_user_queues_behind_active_run_without_interrupt tests/test_gateway.py::GatewayTests::test_manager_shares_one_admission_controller_across_users`

Expected: missing wire frame and unsupported `queue_policy` failures.

- [ ] **Step 3: Add protocol and package exports**

Add `RUN_QUEUED = "run.queued"` to `gateway/protocol.py`. Import/export `RUN_QUEUED`, `AdmissionSnapshot`, `GatewayQueuePolicy`, `GatewayRunAdmissionController`, and `QueueOverflowError` from `gateway/__init__.py` without removing existing names.

- [ ] **Step 4: Replace weak pending frames with queued-turn state**

In `gateway/session.py`, remove `PendingUserMessage`, import `deque` and Task 1 types, and extend `ActiveRun`:

```python
@dataclass
class ActiveRun:
    run_id: str
    turn_id: str
    cancel: "CancelToken"
    task: "asyncio.Task[None]"
    permit: RunPermit
    deadline_task: "asyncio.Task[None] | None" = None
```

Extend `GatewaySessionService.__init__` with `queue_policy` and `admission_controller`, then initialize:

```python
self._queue_policy = queue_policy or GatewayQueuePolicy()
self._admission = admission_controller or GatewayRunAdmissionController(self._queue_policy)
self._owns_admission = admission_controller is None
self._active_by_session: dict[str, ActiveRun] = {}
self._current_by_session: dict[str, QueuedTurn] = {}
self._pending_by_session: dict[str, deque[QueuedTurn]] = {}
self._history_by_session: dict[str, list[str]] = {}
self._turns_by_run_id: dict[str, QueuedTurn] = {}
self._lock = asyncio.Lock()
```

- [ ] **Step 5: Accept identity and emit run.queued at ingress**

Build `QueuedTurn` before testing session busy, reserve global capacity, and store it under its run ID. The acceptance core must be:

```python
turn_id = str(payload.get("turn_id") or uuid.uuid4())
run_id = str(payload.get("run_id") or uuid.uuid4())
now = time.monotonic()
turn = QueuedTurn(
    user_id=user_id,
    session_id=str(session_id),
    turn_id=turn_id,
    run_id=run_id,
    endpoint=ep,
    payload=payload,
    user_text=user_text,
    accepted_at_monotonic=now,
    accepted_at_unix_ms=int(time.time() * 1000),
    queue_deadline_monotonic=now + self._queue_policy.queue_wait_timeout_ms / 1000,
    client_message_id=_optional_string(payload.get("client_message_id")),
    payload_fingerprint="",
)
try:
    turn.reservation = await self._admission.reserve(
        user_id=user_id,
        session_id=str(session_id),
        turn_id=turn_id,
        run_id=run_id,
    )
except QueueOverflowError as exc:
    await self._send_queue_error(turn, scope=exc.scope, limit=exc.limit)
    return

async with self._lock:
    self._turns_by_run_id[run_id] = turn
    if str(session_id) in self._current_by_session:
        turn.state = "session_queued"
        turn.queue_reason = "session_busy"
        self._pending_by_session.setdefault(str(session_id), deque()).append(turn)
        queued = True
    else:
        self._current_by_session[str(session_id)] = turn
        queued = False
if queued:
    await self._send_queued(turn)
else:
    self._schedule_dispatch(turn)
```

Implement `_send_queued` with bounded fields only:

```python
async def _send_queued(self, turn: QueuedTurn) -> None:
    snapshot = await self._admission.snapshot()
    async with self._lock:
        session_depth = len(self._pending_by_session.get(turn.session_id, ()))
    await turn.endpoint.send(
        frame(
            type=RUN_QUEUED,
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            run_id=turn.run_id,
            payload={
                "reason": turn.queue_reason,
                "queue_depth": session_depth,
                "global_queue_depth": snapshot.queued_turns,
                "queued_at_ms": turn.accepted_at_unix_ms,
            },
        )
    )
    self._emit_lifecycle(
        "gateway.run.queued",
        session_id=turn.session_id,
        run_id=turn.run_id,
        turn_id=turn.turn_id,
        payload={
            "queue_reason": turn.queue_reason,
            "session_queue_depth": session_depth,
            "global_queue_depth": snapshot.queued_turns,
        },
    )
```

Add the rejection helper used before acceptance:

```python
async def _send_queue_error(
    self,
    turn: QueuedTurn,
    *,
    scope: str,
    limit: int,
    code: str = "queue_overflow",
) -> None:
    await turn.endpoint.send(
        frame(
            type="error",
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            run_id=turn.run_id,
            error={"code": code, "scope": scope, "limit": limit},
        )
    )
```

- [ ] **Step 6: Dispatch only the head and append history after permit**

Add:

```python
def _schedule_dispatch(self, turn: QueuedTurn) -> None:
    task = asyncio.create_task(self._dispatch_turn(turn), name=f"gateway-dispatch-{turn.run_id}")
    turn.dispatch_task = task
    _consume_background_task(task)

async def _dispatch_turn(self, turn: QueuedTurn) -> None:
    if turn.reservation is None:
        raise RuntimeError("accepted turn is missing queue reservation")
    ticket = await self._admission.request_permit(turn.reservation)
    turn.admission_ticket = ticket
    if not ticket.ready.done():
        turn.state = "admission_queued"
        turn.queue_reason = "global_capacity"
        await self._send_queued(turn)
    permit = await ticket.ready
    current_task = asyncio.current_task()
    assert current_task is not None
    async with self._lock:
        if self._current_by_session.get(turn.session_id) is not turn or turn.state == "terminal":
            await self._admission.release_permit(permit)
            return
        turn.state = "running"
        history = self._history_by_session.setdefault(turn.session_id, [])
        history.append(turn.user_text)
        history_snapshot = list(history)
        cancel = CancelToken()
        deadline_ms = _run_timeout_ms(turn.payload, self._config)
        deadline_task = self._start_deadline_monitor(
            session_id=turn.session_id,
            run_id=turn.run_id,
            cancel=cancel,
            deadline_ms=deadline_ms,
        )
        self._active_by_session[turn.session_id] = ActiveRun(
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            cancel=cancel,
            task=current_task,
            permit=permit,
            deadline_task=deadline_task,
        )
    await self._run_backend_turn(
        ep=turn.endpoint,
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        run_id=turn.run_id,
        user_id=turn.user_id,
        user_text=turn.user_text,
        history=history_snapshot,
        payload=turn.payload,
        runtime_interrupt=turn.runtime_interrupt,
        cancel=cancel,
    )
```

Remove the old nested-task `_start_user_message` path. Keep backend/event/request helpers unchanged.

- [ ] **Step 7: Release permit before promoting the next head**

In `_run_backend_turn` finalization, use this complete ownership block before the existing terminal lifecycle emission:

```python
deadline_task: asyncio.Task[None] | None = None
next_turn: QueuedTurn | None = None
permit: RunPermit | None = None
async with self._lock:
    active = self._active_by_session.get(session_id)
    if active is not None and active.run_id == run_id:
        deadline_task = active.deadline_task
        permit = active.permit
        self._active_by_session.pop(session_id, None)
    current = self._current_by_session.get(session_id)
    if current is not None and current.run_id == run_id:
        current.state = "terminal"
        self._current_by_session.pop(session_id, None)
        pending = self._pending_by_session.get(session_id)
        if pending:
            next_turn = pending.popleft()
            self._current_by_session[session_id] = next_turn
            if not pending:
                self._pending_by_session.pop(session_id, None)
if deadline_task is not None:
    deadline_task.cancel()
    await asyncio.gather(deadline_task, return_exceptions=True)
if permit is not None:
    await self._admission.release_permit(permit)
self._emit_lifecycle(
    _terminal_lifecycle_event_type(end_reason),
    session_id=session_id,
    run_id=run_id,
    turn_id=turn_id,
    payload={"reason": end_reason, "expects_reply": expects_reply},
)
if next_turn is not None:
    self._schedule_dispatch(next_turn)
```

This order puts the same session's next turn behind existing cross-session waiters.

- [ ] **Step 8: Share one controller from GatewaySessionManager**

Add `queue_policy` and `admission_controller` constructor arguments. Initialize:

```python
self.queue_policy = queue_policy or GatewayQueuePolicy()
self.admission_controller = admission_controller or GatewayRunAdmissionController(self.queue_policy)
self._owns_admission_controller = admission_controller is None
```

Pass both into every default-created `GatewaySessionService`. At the end of manager `close()`, after destroying sessions, call `await self.admission_controller.close()` only when the manager owns it.

Preserve the existing two-argument `service_factory(user_id, config)` contract. Add a pre-start binding method to `GatewaySessionService` and call it for custom services before `_GatewaySessionEntry.start()`:

```python
def bind_queueing(
    self,
    *,
    queue_policy: GatewayQueuePolicy,
    admission_controller: GatewayRunAdmissionController,
) -> None:
    if self._current_by_session or self._active_by_session:
        raise RuntimeError("cannot rebind queueing after session work has started")
    self._queue_policy = queue_policy
    self._admission = admission_controller
    self._owns_admission = False
```

This keeps custom test/service factories source-compatible while still enforcing the manager-owned process-wide controller.

- [ ] **Step 9: Run focused and existing queue regressions**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_queueing.py tests/test_gateway_session.py::GatewaySessionTests::test_message_user_queues_behind_active_run_without_interrupt tests/test_gateway_session.py::GatewaySessionTests::test_lifecycle_sink_records_queued_and_terminal_run_events tests/test_gateway.py::GatewayTests::test_manager_shares_one_admission_controller_across_users`

Expected: all selected tests pass.

- [ ] **Step 10: Commit session admission integration**

```bash
git add src/assistant_agent/gateway/protocol.py src/assistant_agent/gateway/__init__.py src/assistant_agent/gateway/session.py tests/test_gateway_session.py tests/test_gateway.py
git commit -m "feat(gateway): admit queued turns through shared capacity"
```

---

### Task 3: Queued Cancellation, Timeout, Interrupt and Connection Cleanup

**Files:**
- Modify: `src/assistant_agent/gateway/session.py:140-590,940-1038`
- Modify: `src/assistant_agent/gateway/bridge.py:145-161,216-256`
- Modify: `tests/test_gateway_session.py:400-940`
- Modify: `tests/test_gateway.py:130-240`

**Interfaces:**
- Consumes: Task 2 session/current/pending structures and Task 1 admission APIs.
- Produces: `GatewaySessionService.close()`, queued `run.cancel`, `queue_timeout` before-LLM terminal projection, non-overlapping interrupt, session-wide hangup/disconnect cleanup.
- Queued terminal sequence is `run.queued -> run.end(reason="cancelled")`; it never calls backend or appends history.

- [ ] **Step 1: Write failing queued-cancel and timeout tests**

Add a module-level backend fixture to `tests/test_gateway_session.py`:

```python
class _BlockingFirstBackend:
    def __init__(self) -> None:
        self.release_first = asyncio.Event()
        self.requests = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        if request.text == "first":
            await self.release_first.wait()
        return RealtimeAgentResult(status="completed", run_id=request.run_id)
```

Add these two cases using the module's existing endpoint read/close pattern:

```python
async def test_run_cancel_removes_queued_turn_without_backend_or_history(self) -> None:
    backend = _BlockingFirstBackend()
    session = GatewaySessionService(backend=backend)
    client_ep, session_ep = InMemoryDuplex.create_pair()
    session_task = asyncio.create_task(session.serve(session_ep))
    await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": "first"}))
    await _read_frame_type(client_ep, "run.started")
    await client_ep.send(
        frame(
            type="message.user",
            session_id="s1",
            payload={"text": "second", "turn_id": "t2", "run_id": "r2"},
        )
    )
    await _read_frame_type(client_ep, "run.queued")
    await client_ep.send(frame(type="run.cancel", session_id="s1", run_id="r2"))
    cancelled = await _read_run_end(client_ep, "r2")
    assert cancelled["reason"] == "cancelled"
    assert cancelled["payload"]["cancel"]["phase"] == "before_llm"
    assert [request.text for request in backend.requests] == ["first"]
    backend.release_first.set()
    await _close_session(client_ep, session_ep, session_task)

async def test_queued_turn_timeout_never_calls_backend(self) -> None:
    backend = _BlockingFirstBackend()
    session = GatewaySessionService(
        backend=backend,
        queue_policy=GatewayQueuePolicy(queue_wait_timeout_ms=30),
    )
    client_ep, session_ep = InMemoryDuplex.create_pair()
    session_task = asyncio.create_task(session.serve(session_ep))
    await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": "first"}))
    await _read_frame_type(client_ep, "run.started")
    await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": "expires"}))
    queued = await _read_frame_type(client_ep, "run.queued")
    expired = await _read_run_end(client_ep, str(queued["run_id"]))
    assert expired["reason"] == "cancelled"
    assert expired["payload"]["cancel"]["source"] == "queue_timeout"
    assert [request.text for request in backend.requests] == ["first"]
    backend.release_first.set()
    await _close_session(client_ep, session_ep, session_task)
```

Add these concrete helpers once if the module lacks equivalents:

```python
async def _read_frame_type(endpoint, frame_type: str):
    async def _read():
        async for received in endpoint:
            if received.get("type") == frame_type:
                return received
        raise AssertionError(f"endpoint closed before {frame_type}")
    return await asyncio.wait_for(_read(), timeout=3.0)


async def _read_run_end(endpoint, run_id: str):
    async def _read():
        async for received in endpoint:
            if received.get("type") == "run.end" and received.get("run_id") == run_id:
                return received
        raise AssertionError(f"endpoint closed before run.end for {run_id}")
    return await asyncio.wait_for(_read(), timeout=3.0)
```

- [ ] **Step 2: Write the failing interrupt non-overlap test**

```python
async def test_interrupt_waits_for_old_backend_release(self) -> None:
    class InterruptBackend:
        def __init__(self) -> None:
            self.requests = []
            self.active = 0
            self.max_seen = 0
            self.second_started = asyncio.Event()

        async def run_turn(self, request, *, event_sink=None, cancel_token=None):
            self.requests.append(request)
            self.active += 1
            self.max_seen = max(self.max_seen, self.active)
            try:
                if request.text == "first":
                    await cancel_token.cancelled()
                    return RealtimeAgentResult(status="cancelled", run_id=request.run_id)
                self.second_started.set()
                return RealtimeAgentResult(status="completed", run_id=request.run_id)
            finally:
                self.active -= 1

    backend = InterruptBackend()
    session = GatewaySessionService(backend=backend)
    client_ep, session_ep = InMemoryDuplex.create_pair()
    session_task = asyncio.create_task(session.serve(session_ep))
    await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": "first"}))
    await _read_frame_type(client_ep, "run.started")
    await client_ep.send(
        frame(type="message.user", session_id="s1", payload={"text": "second", "interrupt": True})
    )
    await asyncio.wait_for(backend.second_started.wait(), timeout=2.0)
    assert [request.text for request in backend.requests] == ["first", "second"]
    assert backend.max_seen == 1
    assert backend.requests[1].metadata["control"] == "interrupt"
    await _close_session(client_ep, session_ep, session_task)
```

- [ ] **Step 3: Run tests and observe missing queued lifecycle behavior**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_session.py -k "queued_turn_timeout or removes_queued or interrupt_waits"`

Expected: queued cancel returns `run_not_found`, timeout never terminates, or interrupt concurrency reaches 2.

- [ ] **Step 4: Add one bounded timeout task per accepted queued turn**

After successful acceptance, create:

```python
turn.timeout_task = asyncio.create_task(
    self._expire_queued_turn(turn),
    name=f"gateway-queue-timeout-{turn.run_id}",
)
_consume_background_task(turn.timeout_task)
```

Add:

```python
async def _expire_queued_turn(self, turn: QueuedTurn) -> None:
    await asyncio.sleep(max(0.0, turn.queue_deadline_monotonic - time.monotonic()))
    await self._cancel_queued_turn(
        turn,
        source="queue_timeout",
        reason="queue_wait_timeout",
        emit_cancel_requested=False,
    )

def _cancel_queue_timeout(self, turn: QueuedTurn) -> None:
    task = turn.timeout_task
    turn.timeout_task = None
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()
```

Call `_cancel_queue_timeout(turn)` after permit acquisition and before history append.

- [ ] **Step 5: Implement a single queued terminal helper**

Add this complete ownership/terminal helper:

```python
async def _cancel_queued_turn(
    self,
    turn: QueuedTurn,
    *,
    source: str,
    reason: str | None,
    emit_cancel_requested: bool = True,
) -> bool:
    promote: QueuedTurn | None = None
    async with self._lock:
        if turn.state in {"running", "terminal"}:
            return False
        current = self._current_by_session.get(turn.session_id)
        if current is turn:
            self._current_by_session.pop(turn.session_id, None)
            pending = self._pending_by_session.get(turn.session_id)
            if pending:
                promote = pending.popleft()
                self._current_by_session[turn.session_id] = promote
                if not pending:
                    self._pending_by_session.pop(turn.session_id, None)
        else:
            pending = self._pending_by_session.get(turn.session_id)
            if pending is not None:
                kept = deque(item for item in pending if item is not turn)
                if kept:
                    self._pending_by_session[turn.session_id] = kept
                else:
                    self._pending_by_session.pop(turn.session_id, None)
        turn.state = "terminal"
        ticket = turn.admission_ticket
        reservation = turn.reservation
        dispatch_task = turn.dispatch_task

    self._cancel_queue_timeout(turn)
    ticket_granted = bool(ticket is not None and ticket.granted)
    if ticket is not None and not ticket_granted:
        await self._admission.cancel_ticket(ticket)
    elif ticket is None and reservation is not None:
        await self._admission.release_reservation(reservation)
    if not ticket_granted and dispatch_task is not None and dispatch_task is not asyncio.current_task():
        dispatch_task.cancel()
    if emit_cancel_requested:
        self._emit_lifecycle(
            "gateway.run.cancel_requested",
            session_id=turn.session_id,
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            payload={"source": source, "reason": reason},
        )
    result = RealtimeAgentResult(
        status="cancelled",
        run_id=turn.run_id,
        expects_reply=True,
        metadata=build_realtime_turn_cancellation_metadata(
            {"cancel_source": source, "cancel_reason": reason},
            phase="before_llm",
        ),
    )
    try:
        await turn.endpoint.send(
            frame(
                type="run.end",
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
                reason="cancelled",
                payload=_run_end_payload(result=result, expects_reply=True, run_id=turn.run_id),
            )
        )
    finally:
        self._emit_lifecycle(
            "gateway.run.cancelled",
            session_id=turn.session_id,
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            payload={"reason": "cancelled", "source": source, "phase": "before_llm"},
        )
        if promote is not None:
            self._schedule_dispatch(promote)
    return True
```

When `ticket.granted` races with cancellation, do not cancel the dispatch task. `_dispatch_turn` observes `turn.state == "terminal"`, releases the granted permit, and returns without appending history or calling backend. This is the required no-leak race behavior.

- [ ] **Step 6: Extend run.cancel lookup and session-wide sources**

At the start of `_handle_cancel`, resolve `run_id` against `_turns_by_run_id`. When the matching turn is queued/admission-waiting, call:

```python
cancelled = await self._cancel_queued_turn(
    queued_turn,
    source=cancel_source,
    reason=cancel_reason,
)
if cancelled:
    return
```

For `gateway_disconnect` and `gateway_hangup`, snapshot every non-running turn in the session and cancel each before applying existing active-run cancellation. A normal session-only `run.cancel` without run ID continues to target only the active run.

- [ ] **Step 7: Serialize interrupt after successful capacity reservation**

Resolve interrupt only when the session is busy:

```python
explicit_interrupt = _message_requests_interrupt(turn.payload, self._config)
async with self._lock:
    current = self._current_by_session.get(turn.session_id)
    turn.runtime_interrupt = current is not None and (
        explicit_interrupt or self._queue_policy.mode == "interrupt"
    )
```

Reserve the new turn before touching the old run. If reservation fails, return overflow and leave the old run alive. For a running current head, signal its cancel token and `appendleft` the new turn; do not schedule it. Existing run finalization promotes it after releasing the old permit:

```python
active.cancel.cancel(source="gateway_interrupt")
turn.state = "session_queued"
turn.queue_reason = "session_busy"
self._pending_by_session.setdefault(turn.session_id, deque()).appendleft(turn)
await self._send_queued(turn)
```

For an admission-waiting current head, append the interrupt turn to the pending deque's left edge first, then cancel the old head through `_cancel_queued_turn`. The helper promotes and schedules the interrupt turn exactly once. Do not separately assign `_current_by_session` or call `_schedule_dispatch`; preserve ordinary followups behind the promoted interrupt.

- [ ] **Step 8: Add deterministic service and Bridge cleanup**

Initialize `self._closed = False` and wrap the body of `serve()` in `try/finally` so endpoint close or serve-task cancellation calls `await self.close()`. Add an idempotent close method:

```python
async def close(self, *, source: str = "gateway_disconnect") -> None:
    async with self._lock:
        if self._closed:
            return
        self._closed = True
        queued = [turn for turn in self._turns_by_run_id.values() if turn.state not in {"running", "terminal"}]
        active = list(self._active_by_session.values())
    if queued:
        await asyncio.gather(
            *(
                self._cancel_queued_turn(turn, source=source, reason="session_closed")
                for turn in queued
            ),
            return_exceptions=True,
        )
    for run in active:
        run.cancel.cancel(source=source, reason="session_closed")
        if not run.task.done():
            run.task.cancel()
    tasks = [run.task for run in active if not run.task.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if self._owns_admission:
        await self._admission.close()
```

Call `await entry.service.close(source="gateway_disconnect")` in manager `destroy()` before endpoints close.

In `GatewayBridge.bridge()` send disconnect cancellation whenever session ID is known:

```python
await endpoint.send(
    frame(
        type="run.cancel",
        run_id=run_id,
        session_id=sid,
        user_id=user_id,
        payload={"source": "gateway_disconnect", "reason": "client_disconnected"},
    )
)
```

In `CALL_HANGUP`, send session cancellation even if no active run ID is known, with `source=gateway_hangup` and `reason=call_hangup`. Keep `call.hangup_ack.payload.cancelled_active_run` compatibility unchanged.

- [ ] **Step 9: Run cancellation and Bridge regressions**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_session.py tests/test_gateway.py`

Expected: all tests pass, including active cancellation, interrupt stale-output and hangup cases.

- [ ] **Step 10: Commit queued lifecycle controls**

```bash
git add src/assistant_agent/gateway/session.py src/assistant_agent/gateway/bridge.py tests/test_gateway_session.py tests/test_gateway.py
git commit -m "feat(gateway): cancel and expire queued turns safely"
```

---

### Task 4: Dedupe, Identity Conflicts and Per-Session Overflow

**Files:**
- Modify: `src/assistant_agent/gateway/queueing.py`
- Modify: `src/assistant_agent/gateway/session.py:140-240`
- Modify: `tests/test_gateway_queueing.py`
- Modify: `tests/test_gateway_session.py`

**Interfaces:**
- Consumes: `QueuedTurn` identity fields and Task 3 lifecycle.
- Produces: `GatewayTurnIdentityIndex`, `IdentityConflictError`, `gateway_payload_fingerprint`, `duplicate_message`, `identity_conflict`, and session/global `queue_overflow`.
- Index stores fingerprints and identities, never assistant output or raw provider/tool data.

- [ ] **Step 1: Write failing identity and overflow tests**

Append to `tests/test_gateway_queueing.py`:

```python
from assistant_agent.gateway.queueing import GatewayTurnIdentityIndex, IdentityConflictError


class GatewayTurnIdentityIndexTests(unittest.TestCase):
    def test_duplicate_returns_canonical_record(self) -> None:
        index = GatewayTurnIdentityIndex(ttl_s=300, max_entries=4)
        index.remember(
            session_id="s1",
            client_message_id="m1",
            turn_id="t1",
            run_id="r1",
            payload_fingerprint="hash-one",
            state="session_queued",
        )
        duplicate = index.check(
            session_id="s1",
            client_message_id="m1",
            turn_id="t1",
            run_id="r1",
            payload_fingerprint="hash-one",
        )
        assert duplicate is not None
        assert duplicate.run_id == "r1"
        assert duplicate.state == "session_queued"

    def test_reused_identity_with_different_payload_conflicts(self) -> None:
        index = GatewayTurnIdentityIndex(ttl_s=300, max_entries=4)
        index.remember(
            session_id="s1",
            client_message_id="m1",
            turn_id="t1",
            run_id="r1",
            payload_fingerprint="hash-one",
            state="session_queued",
        )
        with self.assertRaises(IdentityConflictError):
            index.check(
                session_id="s1",
                client_message_id="m1",
                turn_id="t1",
                run_id="r1",
                payload_fingerprint="hash-two",
            )
```

Add session tests that assert a duplicate executes once and session overflow returns:

```python
assert duplicate_error["error"] == {
    "code": "duplicate_message",
    "turn_id": "t1",
    "run_id": "r1",
    "state": "session_queued",
}
assert overflow_error["error"]["code"] == "queue_overflow"
assert overflow_error["error"]["scope"] == "session"
```

- [ ] **Step 2: Run tests and observe missing index/cap behavior**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_queueing.py -k Identity tests/test_gateway_session.py -k "duplicate or overflow"`

Expected: missing index import or acceptance beyond configured pending cap.

- [ ] **Step 3: Implement fingerprint and bounded identity index**

Add SHA-256 canonical payload hashing:

```python
def gateway_payload_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
```

Add:

```python
@dataclass
class DedupeRecord:
    session_id: str
    client_message_id: str | None
    turn_id: str
    run_id: str
    payload_fingerprint: str
    state: str
    expires_at_monotonic: float


class IdentityConflictError(RuntimeError):
    pass


class GatewayTurnIdentityIndex:
    def __init__(self, *, ttl_s: float, max_entries: int) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._records: OrderedDict[str, DedupeRecord] = OrderedDict()

    def check(self, *, session_id: str, client_message_id: str | None, turn_id: str, run_id: str, payload_fingerprint: str) -> DedupeRecord | None:
        self._prune()
        matches = [self._records[key] for key in self._keys(session_id, client_message_id, turn_id, run_id) if key in self._records]
        if not matches:
            return None
        canonical = matches[0]
        if any(record is not canonical for record in matches):
            raise IdentityConflictError("gateway identifiers resolve to different records")
        if canonical.payload_fingerprint != payload_fingerprint:
            raise IdentityConflictError("gateway identifier reused with different payload")
        self._touch(canonical)
        return canonical

    def remember(self, *, session_id: str, client_message_id: str | None, turn_id: str, run_id: str, payload_fingerprint: str, state: str) -> DedupeRecord:
        record = DedupeRecord(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=turn_id,
            run_id=run_id,
            payload_fingerprint=payload_fingerprint,
            state=state,
            expires_at_monotonic=time.monotonic() + self._ttl_s,
        )
        for key in self._keys(session_id, client_message_id, turn_id, run_id):
            self._records[key] = record
            self._records.move_to_end(key)
        self._trim()
        return record

    def update_state(self, record: DedupeRecord, state: str) -> None:
        record.state = state
        record.expires_at_monotonic = time.monotonic() + self._ttl_s
        self._touch(record)

    @staticmethod
    def _keys(session_id: str, client_message_id: str | None, turn_id: str, run_id: str) -> tuple[str, ...]:
        keys = [f"turn:{session_id}:{turn_id}", f"run:{session_id}:{run_id}"]
        if client_message_id:
            keys.append(f"client:{session_id}:{client_message_id}")
        return tuple(keys)

    def _prune(self) -> None:
        now = time.monotonic()
        expired = {id(value) for value in self._records.values() if value.expires_at_monotonic <= now}
        self._records = OrderedDict((key, value) for key, value in self._records.items() if id(value) not in expired)

    def _touch(self, record: DedupeRecord) -> None:
        for key, value in list(self._records.items()):
            if value is record:
                self._records.move_to_end(key)

    def _trim(self) -> None:
        newest: list[DedupeRecord] = []
        seen: set[int] = set()
        for value in reversed(self._records.values()):
            if id(value) not in seen:
                newest.append(value)
                seen.add(id(value))
        keep = {id(value) for value in newest[: self._max_entries]}
        self._records = OrderedDict((key, value) for key, value in self._records.items() if id(value) in keep)
```

Import `hashlib`, `json`, `OrderedDict`, and `Mapping` in `queueing.py`.

- [ ] **Step 4: Gate session acceptance before global reservation**

Initialize one index and one exact record map per user service:

```python
self._identity_index = GatewayTurnIdentityIndex(
    ttl_s=self._queue_policy.dedupe_ttl_s,
    max_entries=self._queue_policy.dedupe_max_entries_per_user,
)
self._identity_records_by_run_id: dict[str, DedupeRecord] = {}
```

Before `reserve`, check canonical `turn_id`, `run_id`, optional `client_message_id`, and fingerprint. Return `identity_conflict` for different content and `duplicate_message` with canonical IDs/state for exact duplicates.

Under the service lock, compute the rejection without performing endpoint I/O:

```python
pending_depth = len(self._pending_by_session.get(str(session_id), ()))
session_limit_reached = (
    str(session_id) in self._current_by_session
    and pending_depth >= self._queue_policy.max_pending_per_session
)
```

After releasing the lock, reject before global reservation:

```python
if session_limit_reached:
    await self._send_queue_error(
        turn,
        code="queue_overflow",
        scope="session",
        limit=self._queue_policy.max_pending_per_session,
    )
    return
```

Remember identity only after session and global capacity both accept the turn, store it under `run_id`, and use one transition helper:

```python
record = self._identity_index.remember(
    session_id=turn.session_id,
    client_message_id=turn.client_message_id,
    turn_id=turn.turn_id,
    run_id=turn.run_id,
    payload_fingerprint=turn.payload_fingerprint,
    state=turn.state,
)
self._identity_records_by_run_id[turn.run_id] = record

def _set_turn_state(self, turn: QueuedTurn, state: str) -> None:
    turn.state = state
    record = self._identity_records_by_run_id.get(turn.run_id)
    if record is not None:
        self._identity_index.update_state(record, state)
        if state == "terminal":
            self._identity_records_by_run_id.pop(turn.run_id, None)
```

Use `_set_turn_state` for `session_queued`, `admission_queued`, `running`, and `terminal`. Rejected turns do not enter the index, so the same client ID can retry after capacity recovers.

- [ ] **Step 5: Run queueing and session suites**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_queueing.py tests/test_gateway_session.py`

Expected: all tests pass; duplicate and rejected messages never reach backend.

- [ ] **Step 6: Commit dedupe and overflow**

```bash
git add src/assistant_agent/gateway/queueing.py src/assistant_agent/gateway/session.py tests/test_gateway_queueing.py tests/test_gateway_session.py
git commit -m "feat(gateway): bound and deduplicate queued turns"
```

---

### Task 5: Runtime Configuration and Facade Timeout Cleanup

**Files:**
- Modify: `src/assistant_agent/api/gateway_runtime.py:25-96,218-256`
- Modify: `src/assistant_agent/services/gateway_turn_facade.py:79-126`
- Modify: `tests/test_gateway_api.py`
- Modify: `tests/test_gateway_turn_facade.py`

**Interfaces:**
- Consumes: `GatewayQueuePolicy` and manager constructor.
- Produces: six exact environment variables, `_GatewayTurnDispatcher` run-id demultiplexing, and facade timeout cleanup via `run.cancel(run_id=...)`.

- [ ] **Step 1: Write failing environment policy test**

Add to `tests/test_gateway_api.py`:

Import `GatewayQueuePolicy` from `assistant_agent.gateway` before adding the test.

```python
def test_create_gateway_session_manager_reads_queue_policy_env() -> None:
    manager = gateway_runtime.create_gateway_session_manager(
        env={
            "MULTIMODAL_AGENT_GATEWAY_MAX_ACTIVE_RUNS": "2",
            "MULTIMODAL_AGENT_GATEWAY_MAX_PENDING_PER_SESSION": "3",
            "MULTIMODAL_AGENT_GATEWAY_MAX_QUEUED_TURNS": "7",
            "MULTIMODAL_AGENT_GATEWAY_QUEUE_WAIT_TIMEOUT_MS": "9000",
            "MULTIMODAL_AGENT_GATEWAY_DEDUPE_TTL_S": "45",
            "MULTIMODAL_AGENT_GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER": "20",
        },
        start_reaper=False,
    )
    assert manager.queue_policy == GatewayQueuePolicy(
        max_active_runs=2,
        max_pending_per_session=3,
        max_queued_turns_global=7,
        queue_wait_timeout_ms=9000,
        dedupe_ttl_s=45.0,
        dedupe_max_entries_per_user=20,
    )
```

Add a case passing `MULTIMODAL_AGENT_GATEWAY_MAX_ACTIVE_RUNS=0` and asserting `ValueError` names that variable.

- [ ] **Step 2: Write failing facade timeout cleanup test**

Add to `tests/test_gateway_turn_facade.py`:

Import `pytest`, `GatewayTurnTimeout`, and `RealtimeAgentEvent` alongside the module's existing Gateway/realtime imports.

```python
async def test_timeout_sends_run_cancel_and_releases_backend(self) -> None:
    class CancellableBackend:
        def __init__(self) -> None:
            self.cancelled = asyncio.Event()

        async def run_turn(self, request, *, event_sink=None, cancel_token=None):
            await cancel_token.cancelled()
            self.cancelled.set()
            return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

    backend = CancellableBackend()
    manager = GatewaySessionManager(backend_factory=lambda: backend, start_reaper=False)
    facade = GatewayTurnFacade(manager=manager)
    with pytest.raises(GatewayTurnTimeout):
        await facade.run_turn(
            GatewayTurnRequest(user_id="u1", session_id="s1", text="wait", timeout_s=0.02)
        )
    await asyncio.wait_for(backend.cancelled.wait(), timeout=1.0)
    assert (await manager.admission_controller.snapshot()).active_runs == 0
    await manager.close()
```

Add a concurrent same-user test:

```python
async def test_concurrent_same_user_turns_receive_their_own_frames(self) -> None:
    class OrderedBackend:
        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def run_turn(self, request, *, event_sink=None, cancel_token=None):
            assert event_sink is not None
            if request.text == "first":
                self.first_started.set()
                await self.release_first.wait()
            await event_sink(RealtimeAgentEvent(type="response.chunk", text=f"done:{request.text}"))
            return RealtimeAgentResult(status="completed", run_id=request.run_id)

    backend = OrderedBackend()
    manager = GatewaySessionManager(backend_factory=lambda: backend, start_reaper=False)
    facade = GatewayTurnFacade(manager=manager)
    first = asyncio.create_task(
        facade.run_turn(GatewayTurnRequest(user_id="u1", session_id="s1", text="first"))
    )
    await asyncio.wait_for(backend.first_started.wait(), timeout=1.0)
    second = asyncio.create_task(
        facade.run_turn(GatewayTurnRequest(user_id="u1", session_id="s1", text="second"))
    )
    async def _wait_for_queued_reservation() -> None:
        while (await manager.admission_controller.snapshot()).queued_turns == 0:
            await asyncio.sleep(0)
    await asyncio.wait_for(_wait_for_queued_reservation(), timeout=1.0)
    backend.release_first.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.response_text == "done:first"
    assert second_result.response_text == "done:second"
    assert "run.queued" in [item["type"] for item in second_result.frames]
    assert first_result.run_id != second_result.run_id
    await manager.close()
```

- [ ] **Step 3: Run new tests and observe failures**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_api.py tests/test_gateway_turn_facade.py -k "queue_policy_env or timeout_sends_run_cancel or concurrent_same_user"`

Expected: manager uses default policy, facade timeout leaves backend active, or concurrent facade readers steal/misroute frames.

- [ ] **Step 4: Parse strict positive queue environment values**

Add constants:

```python
GATEWAY_MAX_ACTIVE_RUNS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_ACTIVE_RUNS"
GATEWAY_MAX_PENDING_PER_SESSION_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_PENDING_PER_SESSION"
GATEWAY_MAX_QUEUED_TURNS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_QUEUED_TURNS"
GATEWAY_QUEUE_WAIT_TIMEOUT_MS_ENV = "MULTIMODAL_AGENT_GATEWAY_QUEUE_WAIT_TIMEOUT_MS"
GATEWAY_DEDUPE_TTL_S_ENV = "MULTIMODAL_AGENT_GATEWAY_DEDUPE_TTL_S"
GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER_ENV = "MULTIMODAL_AGENT_GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER"
```

Add strict helpers without changing existing lenient session/reaper parsers:

```python
def _positive_int_env(env: Mapping[str, str], name: str, *, default: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float_env(env: Mapping[str, str], name: str, *, default: float) -> float:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be positive") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
```

Construct `GatewayQueuePolicy` with exact defaults from Global Constraints and pass it as `queue_policy` to `GatewaySessionManager`.

- [ ] **Step 5: Add one endpoint reader and run-id inbox demultiplexing**

Add a private dispatcher. `register` is called before `message.user` send, `_read_loop` is the only endpoint consumer, and `unregister` runs in `finally`:

```python
class _GatewayTurnDispatcher:
    def __init__(self, endpoint) -> None:
        self.endpoint = endpoint
        self._inboxes: dict[str, asyncio.Queue[Frame | None]] = {}
        self._lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._read_loop(), name="gateway-turn-dispatcher")

    async def register(self, run_id: str) -> asyncio.Queue[Frame | None]:
        async with self._lock:
            if run_id in self._inboxes:
                raise GatewayTurnError(f"duplicate facade run id: {run_id}")
            inbox: asyncio.Queue[Frame | None] = asyncio.Queue()
            self._inboxes[run_id] = inbox
            return inbox

    async def unregister(self, run_id: str) -> None:
        async with self._lock:
            self._inboxes.pop(run_id, None)

    async def _read_loop(self) -> None:
        try:
            async for received in self.endpoint:
                run_id = received.get("run_id")
                if not isinstance(run_id, str):
                    continue
                async with self._lock:
                    inbox = self._inboxes.get(run_id)
                if inbox is not None:
                    await inbox.put(dict(received))
        finally:
            async with self._lock:
                inboxes = list(self._inboxes.values())
                self._inboxes.clear()
            for inbox in inboxes:
                await inbox.put(None)
```

Store dispatchers by `user_id` in `GatewayTurnFacade`. Reuse only when `dispatcher.endpoint is handle.endpoint`; replace a stale dispatcher after manager recreation. Change `_collect_turn` to consume `inbox.get()` until `run.end`, treating `None` as endpoint closure. Do not create a second reader in `_collect_turn`.

Initialize and resolve dispatchers under one lock:

```python
self._dispatchers: dict[str, _GatewayTurnDispatcher] = {}
self._dispatcher_lock = asyncio.Lock()

async def _dispatcher_for(self, user_id: str, endpoint) -> _GatewayTurnDispatcher:
    async with self._dispatcher_lock:
        current = self._dispatchers.get(user_id)
        if current is not None and current.endpoint is endpoint:
            return current
        dispatcher = _GatewayTurnDispatcher(endpoint)
        self._dispatchers[user_id] = dispatcher
        return dispatcher
```

In `run_turn`, register before send and always unregister:

```python
dispatcher = await self._dispatcher_for(request.user_id, handle.endpoint)
inbox = await dispatcher.register(run_id)
try:
    await handle.endpoint.send(
        frame(
            type="message.user",
            session_id=request.session_id,
            user_id=request.user_id,
            payload=_message_payload(request, turn_id=turn_id, run_id=run_id),
        )
    )
    return await self._collect_turn(
        inbox,
        handle.endpoint,
        session_id=request.session_id,
        turn_id=turn_id,
        run_id=run_id,
        timeout_s=request.timeout_s,
    )
finally:
    await dispatcher.unregister(run_id)
```

Replace endpoint iteration inside `_read_until_terminal` with:

```python
while True:
    received = await inbox.get()
    if received is None:
        raise GatewayTurnError("Gateway endpoint closed before run.end")
    frames.append(received)
    if received.get("type") == "stream.chunk":
        chunks.append(_chunk_text(received))
        continue
    if received.get("type") == "run.end":
        return _turn_result(frames=frames, terminal=received, chunks=chunks)
```

- [ ] **Step 6: Cancel facade-owned run on total timeout**

Replace the `_collect_turn` timeout handler:

```python
except TimeoutError as exc:
    try:
        await endpoint.send(
            frame(
                type="run.cancel",
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"source": "gateway_cancel", "reason": "facade_timeout"},
            )
        )
    except Exception:
        pass
    raise GatewayTurnTimeout(
        f"Gateway turn timed out after {timeout_s:.3g}s before run.end"
    ) from exc
```

Keep intermediate `run.queued` frames in the result frame list. Do not suppress `GatewayTurnTimeout`.

- [ ] **Step 7: Run API and facade suites**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_turn_facade.py tests/test_gateway_api.py`

Expected: all tests pass and existing HTTP response schemas remain unchanged.

- [ ] **Step 8: Commit runtime wiring and facade cleanup**

```bash
git add src/assistant_agent/api/gateway_runtime.py src/assistant_agent/services/gateway_turn_facade.py tests/test_gateway_api.py tests/test_gateway_turn_facade.py
git commit -m "feat(gateway): configure queue limits and cancel timed out turns"
```

---

### Task 6: Lifecycle Observability and Canonical Documentation

**Files:**
- Modify: `src/assistant_agent/gateway/session.py`
- Modify: `tests/test_gateway_session.py`
- Modify: `docs/gateway-architecture.md:1-367`
- Retain: `docs/superpowers/specs/2026-07-13-gateway-queue-admission-design.md`
- Retain: `docs/superpowers/plans/2026-07-13-gateway-queue-admission.md`

**Interfaces:**
- Consumes: queue transitions and `GatewayLifecycleEvent`.
- Produces: prompt-safe `gateway.run.admitted`, `gateway.run.queue_rejected`, `gateway.run.queue_expired` evidence and canonical architecture state.
- Does not modify `TraceInvariantObserver`; queued-before-LLM runs have no runtime trace.

- [ ] **Step 1: Write failing lifecycle sequence and redaction tests**

Extend `tests/test_gateway_session.py` with a queued cancellation event assertion:

```python
assert [event.type for event in events] == [
    "gateway.run.admitted",
    "gateway.run.started",
    "gateway.run.queued",
    "gateway.run.cancel_requested",
    "gateway.run.cancelled",
]
queued_event = events[2]
assert queued_event.run_id == "r2"
assert queued_event.payload["queue_reason"] == "session_busy"
assert "text" not in queued_event.payload
assert "payload" not in queued_event.payload
```

For a global capacity wait, assert:

```python
admitted = next(event for event in events if event.type == "gateway.run.admitted")
assert admitted.payload["queue_wait_ms"] >= 0
assert admitted.payload["active_runs"] <= admitted.payload["max_active_runs"]
```

Add a Gateway frame contract assertion for queued terminal behavior:

```python
assert queued_cancel_types == ["run.queued", "run.end"]
assert queued_cancel_end["reason"] == "cancelled"
assert queued_cancel_end["payload"]["cancel"]["phase"] == "before_llm"
```

Add completed/error integration assertions showing every actual completed or error `run.end` for these test flows has a preceding `run.started` with the same run ID. Do not create assistant runtime trace events or a separate test-only state machine for a queued-only turn.

- [ ] **Step 2: Run lifecycle tests and observe missing events**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_session.py -k "lifecycle or queued_cancel"`

Expected: failures name missing admitted/rejected/expired lifecycle evidence.

- [ ] **Step 3: Emit bounded lifecycle payloads**

After permit acquisition and before `run.started`, emit:

```python
snapshot = await self._admission.snapshot()
self._emit_lifecycle(
    "gateway.run.admitted",
    session_id=turn.session_id,
    run_id=turn.run_id,
    turn_id=turn.turn_id,
    payload={
        "queue_wait_ms": max(0, int((time.monotonic() - turn.accepted_at_monotonic) * 1000)),
        "active_runs": snapshot.active_runs,
        "max_active_runs": snapshot.max_active_runs,
        "global_queue_depth": snapshot.queued_turns,
    },
)
```

Before returning overflow, emit:

```python
self._emit_lifecycle(
    "gateway.run.queue_rejected",
    session_id=turn.session_id,
    run_id=turn.run_id,
    turn_id=turn.turn_id,
    payload={"reason": "queue_overflow", "scope": scope, "limit": limit},
)
```

On queue timeout, emit `gateway.run.queue_expired` before terminal cancellation with only `queue_wait_ms` and `reason=queue_wait_timeout`. Never add user text, raw payload, provider data, tool arguments, memory content, or another session's IDs.

- [ ] **Step 4: Update canonical Gateway architecture**

Edit `docs/gateway-architecture.md` so Quick Handoff, Gateway Responsibilities and Current Code Map record these implemented facts:

```text
- Gateway assigns turn_id/run_id when a user message is accepted, not when backend execution starts.
- Ordinary same-session turns use bounded followup FIFO; explicit interrupt cancels the old run but the next backend starts only after the old backend releases its permit.
- GatewaySessionManager shares one process-wide GatewayRunAdmissionController.
- Defaults: per-session pending 8, global queued 64, active runs 4, queue wait 120000 ms.
- Queued turns can be cancelled by run_id and may end cancelled before run.started.
- Queue wait does not consume active run timeout or enter conversation history.
- v1 is process-local and excludes collect, steer, durable recovery, Proactive Wake scheduling and worker-agent admission.
```

Add `src/assistant_agent/gateway/queueing.py` to the code map. Link the retained spec and plan as design/execution evidence while keeping `docs/gateway-architecture.md` authoritative for implemented runtime behavior.

- [ ] **Step 5: Run lifecycle and documentation validation**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_session.py`

Run: `git diff --check -- docs/gateway-architecture.md docs/superpowers/specs/2026-07-13-gateway-queue-admission-design.md docs/superpowers/plans/2026-07-13-gateway-queue-admission.md src/assistant_agent/gateway/session.py tests/test_gateway_session.py`

Expected: tests pass and whitespace check emits no output.

- [ ] **Step 6: Commit lifecycle evidence and authority update**

Do not commit the design independently. Include it with the completed implementation stage:

```bash
git add src/assistant_agent/gateway/session.py tests/test_gateway_session.py docs/gateway-architecture.md docs/superpowers/specs/2026-07-13-gateway-queue-admission-design.md docs/superpowers/plans/2026-07-13-gateway-queue-admission.md
git commit -m "docs(gateway): establish queue admission contract"
```

---

### Task 7: Offline Qualification Gate

**Files:**
- Verify only. Modify a file only when a failing test identifies a regression caused by Tasks 1-6.

**Interfaces:**
- Consumes: complete QueuePolicy v1.
- Produces: reproducible offline evidence for Gateway, realtime adapter, HTTP/WebSocket entries and tool-governed runtime compatibility.

- [ ] **Step 1: Run focused Gateway and realtime tests**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_gateway_queueing.py tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_gateway_turn_facade.py tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py`

Expected: all selected tests pass with no real Provider calls.

- [ ] **Step 2: Run entry convergence and realtime simulator regressions**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_phase0_entrypoint_contracts.py tests/test_realtime_call_simulator.py`

Expected: all tests pass; HTTP, CLI/demo, Gateway WebSocket and realtime entry classifications remain unchanged.

- [ ] **Step 3: Run repository fast gate**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q`

Expected: all fast tests pass. When an unrelated dirty-worktree test fails, record its exact node ID and verify that the failure is independent of the scoped staged files before modifying anything.

- [ ] **Step 4: Run environment and whitespace checks**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py`

Run: `git diff --check -- src/assistant_agent/gateway src/assistant_agent/api/gateway_runtime.py src/assistant_agent/services/gateway_turn_facade.py tests/test_gateway_queueing.py tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_gateway_turn_facade.py docs/gateway-architecture.md docs/superpowers/specs/2026-07-13-gateway-queue-admission-design.md docs/superpowers/plans/2026-07-13-gateway-queue-admission.md`

Expected: environment check succeeds and whitespace check emits no output.

- [ ] **Step 5: Inspect scoped status and diff**

Run: `git status --short`

Run: `git diff --stat -- src/assistant_agent/gateway src/assistant_agent/api/gateway_runtime.py src/assistant_agent/services/gateway_turn_facade.py tests/test_gateway_queueing.py tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_gateway_turn_facade.py docs/gateway-architecture.md docs/superpowers/specs/2026-07-13-gateway-queue-admission-design.md docs/superpowers/plans/2026-07-13-gateway-queue-admission.md`

Expected: only planned files appear in the scoped diff; unrelated pre-existing modifications remain unstaged and untouched.

- [ ] **Step 6: Record qualification result without an empty commit**

Do not create a qualification-only commit when all gates pass. If a regression is found, return the correction and its failing test to the owning Task 1-6 file set, rerun that task's exact test and commit steps, then repeat Task 7 from Step 1. Record final commands, pass counts, any unrelated pre-existing failure, and the explicit statement that no real Provider was invoked.
