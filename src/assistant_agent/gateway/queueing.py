"""Bounded in-process queue policy and Gateway run admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Mapping
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
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.dedupe_ttl_s, bool)
            or not isinstance(self.dedupe_ttl_s, (int, float))
            or not math.isfinite(self.dedupe_ttl_s)
            or self.dedupe_ttl_s <= 0
        ):
            raise ValueError("dedupe_ttl_s must be finite and positive")
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
    arbitration_pending: bool = False
    arbitration_decision_id: str | None = None
    arbitration_expected_run_id: str | None = None
    arbitration_task: asyncio.Task[None] | None = None


class QueueOverflowError(RuntimeError):
    def __init__(self, *, scope: Literal["session", "global"], limit: int) -> None:
        super().__init__(f"gateway {scope} queue limit reached: {limit}")
        self.scope = scope
        self.limit = limit


def gateway_payload_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    """Raised when Gateway identity aliases no longer name one payload."""


class GatewayTurnIdentityIndex:
    """Bounded per-user identity index for process-local turn dedupe."""

    def __init__(self, *, ttl_s: float, max_entries: int) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._records: OrderedDict[str, DedupeRecord] = OrderedDict()

    def check(
        self,
        *,
        session_id: str,
        client_message_id: str | None,
        turn_id: str,
        run_id: str,
        payload_fingerprint: str,
    ) -> DedupeRecord | None:
        self._prune()
        matches = [
            self._records[key]
            for key in self._keys(session_id, client_message_id, turn_id, run_id)
            if key in self._records
        ]
        if not matches:
            return None
        canonical = matches[0]
        if any(record is not canonical for record in matches):
            raise IdentityConflictError(
                "gateway identifiers resolve to different records"
            )
        if canonical.payload_fingerprint != payload_fingerprint:
            raise IdentityConflictError(
                "gateway identifier reused with different payload"
            )
        self._touch(canonical)
        return canonical

    def remember(
        self,
        *,
        session_id: str,
        client_message_id: str | None,
        turn_id: str,
        run_id: str,
        payload_fingerprint: str,
        state: str,
    ) -> DedupeRecord:
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
    def _keys(
        session_id: str,
        client_message_id: str | None,
        turn_id: str,
        run_id: str,
    ) -> tuple[str, ...]:
        keys = [
            f"turn:{session_id}:{turn_id}",
            f"run:{session_id}:{run_id}",
        ]
        if client_message_id:
            keys.append(f"client:{session_id}:{client_message_id}")
        return tuple(keys)

    def _prune(self) -> None:
        now = time.monotonic()
        expired = {
            id(value)
            for value in self._records.values()
            if value.expires_at_monotonic <= now
        }
        self._records = OrderedDict(
            (key, value)
            for key, value in self._records.items()
            if id(value) not in expired
        )

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
        self._records = OrderedDict(
            (key, value)
            for key, value in self._records.items()
            if id(value) in keep
        )


class GatewayRunAdmissionController:
    """Own process-local queued reservations and fair active-run permits."""

    def __init__(self, policy: GatewayQueuePolicy) -> None:
        self.policy = policy
        self._lock = asyncio.Lock()
        self._reservations: dict[str, QueueReservation] = {}
        self._waiters: deque[AdmissionTicket] = deque()
        self._permits: dict[str, RunPermit] = {}
        self._closed = False

    async def reserve(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        run_id: str,
    ) -> QueueReservation:
        async with self._lock:
            if self._closed:
                raise RuntimeError("gateway admission controller is closed")
            if len(self._reservations) >= self.policy.max_queued_turns_global:
                raise QueueOverflowError(
                    scope="global",
                    limit=self.policy.max_queued_turns_global,
                )
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
            self._grant_waiters_locked()
            return ticket

    async def cancel_ticket(self, ticket: AdmissionTicket) -> bool:
        async with self._lock:
            if ticket.cancelled or ticket.granted:
                return False
            ticket.cancelled = True
            self._waiters = deque(item for item in self._waiters if item is not ticket)
            self._release_reservation_locked(ticket.reservation)
            if not ticket.ready.done():
                ticket.ready.cancel()
            self._grant_waiters_locked()
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
            self._grant_waiters_locked()
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
            if self._closed:
                return
            self._closed = True
            waiters = list(self._waiters)
            self._waiters.clear()
            for ticket in waiters:
                ticket.cancelled = True
            reservations = list(self._reservations.values())
            for reservation in reservations:
                self._release_reservation_locked(reservation)
            for ticket in waiters:
                if not ticket.ready.done():
                    ticket.ready.cancel()

    def _grant_waiters_locked(self) -> None:
        while self._waiters and len(self._permits) < self.policy.max_active_runs:
            ticket = self._waiters.popleft()
            if ticket.cancelled or ticket.ready.done():
                ticket.cancelled = True
                self._release_reservation_locked(ticket.reservation)
                continue
            self._release_reservation_locked(ticket.reservation)
            permit = RunPermit(
                permit_id=str(uuid.uuid4()),
                run_id=ticket.reservation.run_id,
                acquired_at_monotonic=time.monotonic(),
            )
            ticket.granted = True
            self._permits[permit.permit_id] = permit
            ticket.ready.set_result(permit)

    def _release_reservation_locked(self, reservation: QueueReservation) -> bool:
        if reservation.released or reservation.reservation_id not in self._reservations:
            return False
        reservation.released = True
        self._reservations.pop(reservation.reservation_id, None)
        return True
