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
