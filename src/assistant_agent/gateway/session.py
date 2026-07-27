"""Gateway session service and per-user session manager."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.gateway.observability import (
    GatewayLifecycleSink,
    emit_gateway_lifecycle_event,
)
from assistant_agent.gateway.protocol import RUN_QUEUED, Frame, frame
from assistant_agent.gateway.queueing import (
    DedupeRecord,
    GatewayQueuePolicy,
    GatewayRunAdmissionController,
    GatewayTurnIdentityIndex,
    IdentityConflictError,
    QueueOverflowError,
    QueuedTurn,
    RunPermit,
    gateway_payload_fingerprint,
)
from assistant_agent.gateway.transport import Endpoint, InMemoryDuplex
from assistant_agent.gateway.turn_arbitration import GatewayTurnArbitrationController
from assistant_agent.gateway.runtime_adapter import GatewayRuntimeAdapter
from assistant_agent.gateway.runtime_backend import RealtimeAgentBackend
from assistant_agent.gateway.runtime_types import (
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
)
from assistant_agent.gateway.delivery import progress_replacement_key
from assistant_agent.gateway.cancellation_models import (
    build_realtime_turn_cancellation_metadata,
    realtime_turn_cancellation_from_metadata,
)
from assistant_agent.gateway.turn_arbitration_models import (
    REALTIME_TURN_ARBITRATION_METADATA_KEY,
    RealtimeTurnArbitrationDecision,
    RealtimeTurnArbitrationRequest,
    prompt_safe_arbitration_task_state,
)
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.identifiers import new_prefixed_uuid7, new_run_id, new_turn_id
from assistant_agent.runtime.realtime_task_state import (
    RealtimeTaskStateStore,
    apply_cancel_only_arbitration_to_task_state,
    get_default_realtime_task_state_store,
    snapshot_from_task_state,
)


@dataclass
class ActiveRun:
    run_id: str
    turn_id: str
    cancel: "CancelToken"
    task: "asyncio.Task[None]"
    permit: RunPermit
    deadline_task: "asyncio.Task[None] | None" = None
    trace_id: str | None = None


class CancelToken:
    """Cooperative cancellation token passed into realtime backends."""

    def __init__(self) -> None:
        self._evt = asyncio.Event()
        self._metadata: dict[str, Any] = {}

    def cancel(
        self,
        *,
        source: str = "gateway_cancel",
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self._evt.is_set():
            return
        cancel_metadata = dict(metadata or {})
        cancel_metadata["cancel_source"] = source
        if reason is not None:
            cancel_metadata["cancel_reason"] = reason
        self._metadata = build_realtime_turn_cancellation_metadata(
            cancel_metadata,
            phase="final_streaming",
        )
        self._evt.set()

    async def cancelled(self) -> None:
        await self._evt.wait()

    def is_cancelled(self) -> bool:
        return self._evt.is_set()

    @property
    def cancel_metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    @property
    def metadata(self) -> dict[str, Any]:
        return self.cancel_metadata


class GatewaySessionService:
    """Gateway-managed session side of the Gateway<->agent stream.

    This service owns session history, active run lifecycle, cooperative
    cancellation, and event mapping. Agent execution is delegated to a
    RealtimeAgentBackend; by default this remains GatewayRuntimeAdapter.
    """

    def __init__(
        self,
        *,
        user_id: str = "default",
        backend: RealtimeAgentBackend | None = None,
        backend_factory: Callable[[], RealtimeAgentBackend] | None = None,
        config: Mapping[str, Any] | None = None,
        lifecycle_sink: GatewayLifecycleSink | None = None,
        queue_policy: GatewayQueuePolicy | None = None,
        admission_controller: GatewayRunAdmissionController | None = None,
        turn_arbitration_controller: GatewayTurnArbitrationController | None = None,
        realtime_task_state_store: RealtimeTaskStateStore | None = None,
    ) -> None:
        self._user_id = user_id
        self._backend = backend
        self._backend_factory = backend_factory
        self._config: dict[str, Any] = dict(config or {})
        self._lifecycle_sink = lifecycle_sink
        self._queue_policy = queue_policy or GatewayQueuePolicy()
        self._admission = admission_controller or GatewayRunAdmissionController(
            self._queue_policy
        )
        self._owns_admission = admission_controller is None
        self._turn_arbitration = turn_arbitration_controller
        self._realtime_task_state_store = (
            realtime_task_state_store or get_default_realtime_task_state_store()
        )
        self._active_by_session: dict[str, ActiveRun] = {}
        self._current_by_session: dict[str, QueuedTurn] = {}
        self._pending_by_session: dict[str, deque[QueuedTurn]] = {}
        self._history_by_session: dict[str, list[str]] = {}
        self._turns_by_run_id: dict[str, QueuedTurn] = {}
        self._identity_index = GatewayTurnIdentityIndex(
            ttl_s=self._queue_policy.dedupe_ttl_s,
            max_entries=self._queue_policy.dedupe_max_entries_per_user,
        )
        self._identity_records_by_run_id: dict[str, DedupeRecord] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    def update_config(self, values: Mapping[str, Any]) -> None:
        self._config.update({str(key): value for key, value in values.items()})

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
        self._identity_index = GatewayTurnIdentityIndex(
            ttl_s=queue_policy.dedupe_ttl_s,
            max_entries=queue_policy.dedupe_max_entries_per_user,
        )
        self._identity_records_by_run_id.clear()

    def bind_turn_arbitration(
        self,
        controller: GatewayTurnArbitrationController,
    ) -> None:
        if self._current_by_session or self._active_by_session:
            raise RuntimeError("cannot bind turn arbitration after session work has started")
        self._turn_arbitration = controller

    async def has_active_run(self) -> bool:
        """Return whether this user service currently owns any active run."""

        async with self._lock:
            return bool(self._active_by_session)

    def _emit_lifecycle(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        emit_gateway_lifecycle_event(
            self._lifecycle_sink,
            type=event_type,
            user_id=self._user_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            payload=payload,
        )

    async def serve(self, ep: Endpoint) -> None:
        try:
            async for f in ep:
                frame_type = f.get("type")
                if frame_type == "message.user":
                    await self._handle_user_message(ep, f)
                elif frame_type == "run.cancel":
                    await self._handle_cancel(ep, f)
                elif frame_type == "ping":
                    await ep.send(frame(type="pong"))
                else:
                    await ep.send(
                        frame(
                            type="error",
                            error={
                                "code": "unknown_frame",
                                "message": f"unknown type: {frame_type}",
                            },
                        )
                    )
        finally:
            await self.close()

    async def close(self, *, source: str = "gateway_disconnect") -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            queued = [
                turn
                for turn in self._turns_by_run_id.values()
                if turn.state not in {"running", "terminal"}
            ]
            active = list(self._active_by_session.values())

        if queued:
            await asyncio.gather(
                *(
                    self._cancel_queued_turn(
                        turn,
                        source=source,
                        reason="session_closed",
                    )
                    for turn in queued
                ),
                return_exceptions=True,
            )

        for run in active:
            run.cancel.cancel(source=source, reason="session_closed")

        tasks = [run.task for run in active if not run.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=0.05)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._owns_admission:
            await self._admission.close()

    async def _handle_user_message(self, ep: Endpoint, f: Frame) -> None:
        raw_session_id = f.get("session_id")
        payload = _payload_dict(f)
        user_text = str(payload.get("text", ""))
        user_id = str(f.get("user_id") or self._user_id)

        try:
            turn_mode, turn_mode_explicit = _message_turn_mode(payload)
        except ValueError as exc:
            await ep.send(
                frame(
                    type="error",
                    session_id=_optional_string(raw_session_id),
                    error={
                        "code": "invalid_turn_mode",
                        "message": str(exc),
                    },
                )
            )
            return

        if not raw_session_id:
            await ep.send(frame(type="error", error={"code": "missing_session_id"}))
            return
        session_id = str(raw_session_id)
        turn_id = str(payload.get("turn_id") or new_turn_id())
        run_id = str(payload.get("run_id") or new_run_id())
        now = time.monotonic()
        turn = QueuedTurn(
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            endpoint=ep,
            payload=payload,
            user_text=user_text,
            accepted_at_monotonic=now,
            accepted_at_unix_ms=int(time.time() * 1000),
            queue_deadline_monotonic=(
                now + self._queue_policy.queue_wait_timeout_ms / 1000
            ),
            client_message_id=_optional_string(payload.get("client_message_id")),
            payload_fingerprint=gateway_payload_fingerprint(payload),
            turn_mode=turn_mode,
            turn_mode_explicit=turn_mode_explicit,
        )

        try:
            duplicate = self._identity_index.check(
                session_id=turn.session_id,
                client_message_id=turn.client_message_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
                payload_fingerprint=turn.payload_fingerprint,
            )
        except IdentityConflictError:
            await turn.endpoint.send(
                frame(
                    type="error",
                    session_id=turn.session_id,
                    turn_id=turn.turn_id,
                    run_id=turn.run_id,
                    error={"code": "identity_conflict"},
                )
            )
            return
        if duplicate is not None:
            await turn.endpoint.send(
                frame(
                    type="error",
                    session_id=turn.session_id,
                    turn_id=duplicate.turn_id,
                    run_id=duplicate.run_id,
                    error={
                        "code": "duplicate_message",
                        "turn_id": duplicate.turn_id,
                        "run_id": duplicate.run_id,
                        "state": duplicate.state,
                    },
                )
            )
            return

        async with self._lock:
            pending_depth = len(self._pending_by_session.get(session_id, ()))
            session_limit_reached = (
                session_id in self._current_by_session
                and pending_depth >= self._queue_policy.max_pending_per_session
            )
        if session_limit_reached:
            await self._send_queue_error(
                turn,
                code="queue_overflow",
                scope="session",
                limit=self._queue_policy.max_pending_per_session,
            )
            return

        try:
            turn.reservation = await self._admission.reserve(
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
            )
        except QueueOverflowError as exc:
            await self._send_queue_error(turn, scope=exc.scope, limit=exc.limit)
            return

        turn.timeout_task = asyncio.create_task(
            self._expire_queued_turn(turn),
            name=f"gateway-queue-timeout-{turn.run_id}",
        )
        _consume_background_task(turn.timeout_task)

        legacy_interrupt_requested = _message_requests_interrupt(payload, self._config)
        queued = False
        async with self._lock:
            current = self._current_by_session.get(session_id)
            active = self._active_by_session.get(session_id)
            self._turns_by_run_id[run_id] = turn
            if current is not None:
                turn.interrupts_active_run = bool(
                    turn.turn_mode == "replace"
                    or (
                        not turn.turn_mode_explicit
                        and (
                            legacy_interrupt_requested
                            or self._queue_policy.mode == "interrupt"
                        )
                    )
                )
                if (
                    not turn.interrupts_active_run
                    and not turn.turn_mode_explicit
                    and active is not None
                    and self._semantic_interrupt_enabled(payload)
                ):
                    turn.arbitration.pending = True
                    turn.arbitration.decision_id = new_prefixed_uuid7("arbitration")
                    turn.arbitration.expected_run_id = active.run_id
                turn.state = "session_queued"
                turn.queue_reason = "session_busy"
                pending = self._pending_by_session.setdefault(session_id, deque())
                if turn.interrupts_active_run:
                    pending.appendleft(turn)
                else:
                    pending.append(turn)
                queued = True
            else:
                self._current_by_session[session_id] = turn
            record = self._identity_index.remember(
                session_id=turn.session_id,
                client_message_id=turn.client_message_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
                payload_fingerprint=turn.payload_fingerprint,
                state=turn.state,
            )
            self._identity_records_by_run_id[turn.run_id] = record

        if queued:
            await self._send_queued(turn)
            if turn.interrupts_active_run and current is not None:
                cancelled_before_run = await self._cancel_queued_turn(
                    current,
                    source="gateway_interrupt",
                    reason="interrupted_by_new_turn",
                )
                if not cancelled_before_run:
                    await self._interrupt_if_needed(
                        session_id=session_id,
                        expected_run_id=current.run_id,
                    )
            elif turn.arbitration.pending:
                self._schedule_arbitration(turn)
            return
        self._schedule_dispatch(turn)

    def _semantic_interrupt_enabled(self, payload: Mapping[str, Any]) -> bool:
        controller = self._turn_arbitration
        if controller is None or not controller.policy.enabled:
            return False
        if self._config.get("semantic_interrupt_enabled") is False:
            return False
        metadata = payload.get("metadata")
        if not isinstance(metadata, Mapping):
            return False
        if _trusted_entry_source(metadata) is None:
            return False
        gateway = metadata.get("gateway")
        if not isinstance(gateway, Mapping):
            return False
        capabilities = gateway.get("entry_capabilities")
        return bool(
            isinstance(capabilities, Mapping)
            and capabilities.get("supports_semantic_interrupt") is True
        )

    def _schedule_arbitration(self, turn: QueuedTurn) -> None:
        task = asyncio.create_task(
            self._arbitrate_turn(turn),
            name=f"gateway-turn-arbitration-{turn.run_id}",
        )
        turn.arbitration.task = task
        _consume_background_task(task)

    async def _arbitrate_turn(self, turn: QueuedTurn) -> None:
        controller = self._turn_arbitration
        decision_id = turn.arbitration.decision_id
        expected_run_id = turn.arbitration.expected_run_id
        if controller is None or decision_id is None or expected_run_id is None:
            return
        request = self._build_arbitration_request(
            turn,
            decision_id=decision_id,
            expected_run_id=expected_run_id,
        )
        self._emit_lifecycle(
            "gateway.turn.arbitration.started",
            session_id=turn.session_id,
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            payload={
                "decision_id": decision_id,
                "expected_run_id": expected_run_id,
            },
        )
        try:
            outcome = await controller.decide(request)
        except asyncio.CancelledError:
            return
        await self._apply_arbitration_decision(
            turn,
            decision=outcome.decision,
            controller_status=outcome.status,
        )

    def _build_arbitration_request(
        self,
        turn: QueuedTurn,
        *,
        decision_id: str,
        expected_run_id: str,
    ) -> RealtimeTurnArbitrationRequest:
        task_state = self._realtime_task_state_store.get(turn.user_id, turn.session_id)
        task_snapshot = prompt_safe_arbitration_task_state(
            snapshot_from_task_state(task_state).model_dump(mode="json")
            if task_state is not None
            else {}
        )
        language = _optional_string(
            self._config.get("language") or self._config.get("locale")
        )
        return RealtimeTurnArbitrationRequest(
            decision_id=decision_id,
            user_id=turn.user_id,
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            run_id=turn.run_id,
            expected_run_id=expected_run_id,
            utterance=turn.user_text[:1200],
            language=language,
            task_state=task_snapshot,
        )

    async def _apply_arbitration_decision(
        self,
        turn: QueuedTurn,
        *,
        decision: RealtimeTurnArbitrationDecision,
        controller_status: str,
    ) -> None:
        schedule_turn: QueuedTurn | None = None
        cancel_run: ActiveRun | None = None
        cancel_reason: str | None = None
        complete_control_turn = False
        expected_run_matched = False
        stale = False
        normalized_disposition = decision.disposition
        async with self._lock:
            if (
                turn.state == "terminal"
                or not turn.arbitration.pending
                or turn.arbitration.decision_id != decision.decision_id
            ):
                return
            active = self._active_by_session.get(turn.session_id)
            expected_run_matched = bool(
                active is not None and active.run_id == decision.expected_run_id
            )
            turn.arbitration.pending = False
            turn.arbitration.task = None

            if (
                decision.disposition
                in {"CANCEL_ONLY", "REVISE_ACTIVE", "REPLACE_ACTIVE"}
                and not expected_run_matched
            ):
                normalized_disposition = "FOLLOWUP"
                stale = True

            if normalized_disposition == "CANCEL_ONLY":
                if active is not None:
                    active.cancel.cancel(
                        source="gateway_interrupt",
                        reason="semantic_cancel_only",
                        metadata={"decision_id": decision.decision_id},
                    )
                    cancel_run = active
                    cancel_reason = "semantic_cancel_only"
                complete_control_turn = True
            elif normalized_disposition == "ACK_NOOP":
                complete_control_turn = True
            elif normalized_disposition in {"REVISE_ACTIVE", "REPLACE_ACTIVE"}:
                turn.interrupts_active_run = True
                _attach_arbitration_metadata(turn, decision)
                pending = self._pending_by_session.get(turn.session_id)
                if pending is not None:
                    reordered = deque(item for item in pending if item is not turn)
                    reordered.appendleft(turn)
                    self._pending_by_session[turn.session_id] = reordered
                if active is not None:
                    active.cancel.cancel(
                        source="gateway_interrupt",
                        reason="semantic_interrupt",
                        metadata={"decision_id": decision.decision_id},
                    )
                    cancel_run = active
                    cancel_reason = "semantic_interrupt"

            current = self._current_by_session.get(turn.session_id)
            if (
                normalized_disposition in {"FOLLOWUP", "UNCERTAIN"}
                and current is turn
                and active is None
                and turn.dispatch_task is None
            ):
                schedule_turn = turn

        lifecycle_payload = _arbitration_lifecycle_payload(
            decision,
            controller_status=controller_status,
            normalized_disposition=normalized_disposition,
            expected_run_matched=expected_run_matched,
        )
        event_type = "gateway.turn.arbitration.finished"
        if stale:
            event_type = "gateway.turn.arbitration.stale"
        elif decision.fallback_reason:
            event_type = "gateway.turn.arbitration.fallback"
        self._emit_lifecycle(
            event_type,
            session_id=turn.session_id,
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            payload=lifecycle_payload,
        )
        if cancel_run is not None:
            self._emit_lifecycle(
                "gateway.run.cancel_requested",
                session_id=turn.session_id,
                run_id=cancel_run.run_id,
                turn_id=cancel_run.turn_id,
                payload={
                    "source": "gateway_interrupt",
                    "reason": cancel_reason,
                    "decision_id": decision.decision_id,
                    **_active_run_correlation_payload(cancel_run),
                },
            )
        if complete_control_turn:
            if normalized_disposition == "CANCEL_ONLY":
                apply_cancel_only_arbitration_to_task_state(
                    user_id=turn.user_id,
                    session_id=turn.session_id,
                    turn_id=turn.turn_id,
                    run_id=turn.run_id,
                    user_text=turn.user_text,
                    decision=decision,
                    store=self._realtime_task_state_store,
                )
            await self._complete_arbitrated_control_turn(turn, decision=decision)
            return
        if schedule_turn is not None and not self._closed:
            self._schedule_dispatch(schedule_turn)

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
                "queue_depth": session_depth,
                "session_queue_depth": session_depth,
                "global_queue_depth": snapshot.queued_turns,
            },
        )

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
        self._emit_lifecycle(
            "gateway.run.queue_rejected",
            session_id=turn.session_id,
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            payload={"reason": code, "scope": scope, "limit": limit},
        )

    def _set_turn_state(self, turn: QueuedTurn, state: str) -> None:
        turn.state = state
        record = self._identity_records_by_run_id.get(turn.run_id)
        if record is not None:
            self._identity_index.update_state(record, state)
            if state == "terminal":
                self._identity_records_by_run_id.pop(turn.run_id, None)

    async def _expire_queued_turn(self, turn: QueuedTurn) -> None:
        await asyncio.sleep(
            max(0.0, turn.queue_deadline_monotonic - time.monotonic())
        )
        await self._cancel_queued_turn(
            turn,
            source="queue_timeout",
            reason="queue_wait_timeout",
            emit_cancel_requested=False,
        )

    def _cancel_queue_timeout(self, turn: QueuedTurn) -> None:
        task = turn.timeout_task
        turn.timeout_task = None
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()

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
                promote = self._promote_next_locked(turn.session_id)
            else:
                pending = self._pending_by_session.get(turn.session_id)
                if pending is not None:
                    kept = deque(item for item in pending if item is not turn)
                    if kept:
                        self._pending_by_session[turn.session_id] = kept
                    else:
                        self._pending_by_session.pop(turn.session_id, None)
            self._set_turn_state(turn, "terminal")
            self._turns_by_run_id.pop(turn.run_id, None)
            ticket = turn.admission_ticket
            reservation = turn.reservation
            dispatch_task = turn.dispatch_task
            arbitration_task = turn.arbitration.task
            turn.arbitration.task = None
            turn.arbitration.pending = False

        self._cancel_queue_timeout(turn)
        if (
            arbitration_task is not None
            and arbitration_task is not asyncio.current_task()
            and not arbitration_task.done()
        ):
            arbitration_task.cancel()
        ticket_granted = bool(ticket is not None and ticket.granted)
        if ticket is not None and not ticket_granted:
            await self._admission.cancel_ticket(ticket)
            ticket_granted = ticket.granted
        elif ticket is None and reservation is not None:
            await self._admission.release_reservation(reservation)
        if (
            not ticket_granted
            and dispatch_task is not None
            and dispatch_task is not asyncio.current_task()
            and not dispatch_task.done()
        ):
            dispatch_task.cancel()

        if source == "queue_timeout":
            self._emit_lifecycle(
                "gateway.run.queue_expired",
                session_id=turn.session_id,
                run_id=turn.run_id,
                turn_id=turn.turn_id,
                payload={
                    "reason": "queue_wait_timeout",
                    "queue_wait_ms": max(
                        0,
                        int(
                            (time.monotonic() - turn.accepted_at_monotonic)
                            * 1000
                        ),
                    ),
                },
            )

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
                    payload=_run_end_payload(
                        result=result,
                        expects_reply=True,
                        run_id=turn.run_id,
                    ),
                )
            )
        finally:
            self._emit_lifecycle(
                "gateway.run.cancelled",
                session_id=turn.session_id,
                run_id=turn.run_id,
                turn_id=turn.turn_id,
                payload={
                    "reason": "cancelled",
                    "source": source,
                    "phase": "before_llm",
                },
            )
            if promote is not None and not self._closed:
                self._schedule_dispatch(promote)
        return True

    async def _complete_arbitrated_control_turn(
        self,
        turn: QueuedTurn,
        *,
        decision: RealtimeTurnArbitrationDecision,
    ) -> bool:
        promote: QueuedTurn | None = None
        async with self._lock:
            if turn.state in {"running", "terminal"}:
                return False
            current = self._current_by_session.get(turn.session_id)
            if current is turn:
                self._current_by_session.pop(turn.session_id, None)
                promote = self._promote_next_locked(turn.session_id)
            else:
                pending = self._pending_by_session.get(turn.session_id)
                if pending is not None:
                    kept = deque(item for item in pending if item is not turn)
                    if kept:
                        self._pending_by_session[turn.session_id] = kept
                    else:
                        self._pending_by_session.pop(turn.session_id, None)
            self._set_turn_state(turn, "terminal")
            self._turns_by_run_id.pop(turn.run_id, None)
            ticket = turn.admission_ticket
            reservation = turn.reservation
            dispatch_task = turn.dispatch_task
            turn.arbitration.pending = False
            turn.arbitration.task = None

        self._cancel_queue_timeout(turn)
        if ticket is not None and not ticket.granted:
            await self._admission.cancel_ticket(ticket)
        elif ticket is None and reservation is not None:
            await self._admission.release_reservation(reservation)
        elif ticket is not None and ticket.granted:
            permit = await ticket.ready
            await self._admission.release_permit(permit)
        if (
            dispatch_task is not None
            and dispatch_task is not asyncio.current_task()
            and not dispatch_task.done()
        ):
            dispatch_task.cancel()

        payload = {
            "expects_reply": False,
            "supersedes": [progress_replacement_key(turn.run_id)],
            "handled_by": "turn_arbiter",
            "arbitration": _prompt_safe_arbitration_summary(decision),
        }
        await turn.endpoint.send(
            frame(
                type="run.end",
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
                reason="completed",
                payload=payload,
            )
        )
        self._emit_lifecycle(
            "gateway.run.completed",
            session_id=turn.session_id,
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            payload={
                "reason": "completed",
                "expects_reply": False,
                "handled_by": "turn_arbiter",
                "disposition": decision.disposition,
            },
        )
        if promote is not None and not self._closed:
            self._schedule_dispatch(promote)
        return True

    def _promote_next_locked(self, session_id: str) -> QueuedTurn | None:
        pending = self._pending_by_session.get(session_id)
        if not pending:
            return None
        next_turn = pending.popleft()
        self._current_by_session[session_id] = next_turn
        if not pending:
            self._pending_by_session.pop(session_id, None)
        if next_turn.arbitration.pending:
            return None
        return next_turn

    def _schedule_dispatch(self, turn: QueuedTurn) -> None:
        task = asyncio.create_task(
            self._dispatch_turn(turn),
            name=f"gateway-dispatch-{turn.run_id}",
        )
        turn.dispatch_task = task
        _consume_background_task(task)

    async def _dispatch_turn(self, turn: QueuedTurn) -> None:
        if turn.reservation is None:
            raise RuntimeError("accepted turn is missing queue reservation")
        ticket = await self._admission.request_permit(turn.reservation)
        turn.admission_ticket = ticket
        if not ticket.ready.done():
            self._set_turn_state(turn, "admission_queued")
            turn.queue_reason = "global_capacity"
            await self._send_queued(turn)
        permit = await ticket.ready
        self._cancel_queue_timeout(turn)

        current_task = asyncio.current_task()
        assert current_task is not None
        release_unused_permit = False
        async with self._lock:
            if (
                self._current_by_session.get(turn.session_id) is not turn
                or turn.state == "terminal"
            ):
                release_unused_permit = True
            else:
                self._set_turn_state(turn, "running")
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
        if release_unused_permit:
            await self._admission.release_permit(permit)
            return
        snapshot = await self._admission.snapshot()
        self._emit_lifecycle(
            "gateway.run.admitted",
            session_id=turn.session_id,
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            payload={
                "queue_wait_ms": max(
                    0,
                    int(
                        (time.monotonic() - turn.accepted_at_monotonic) * 1000
                    ),
                ),
                "active_runs": snapshot.active_runs,
                "max_active_runs": snapshot.max_active_runs,
                "global_queue_depth": snapshot.queued_turns,
            },
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
            interrupts_active_run=turn.interrupts_active_run,
            cancel=cancel,
        )

    async def _run_backend_turn(
        self,
        *,
        ep: Endpoint,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_id: str,
        user_text: str,
        history: list[str],
        payload: dict[str, Any],
        interrupts_active_run: bool,
        cancel: CancelToken,
    ) -> None:
        expects_reply = True
        end_reason = "error"
        result: RealtimeAgentResult | None = None
        active_trace_id: str | None = None
        try:
            self._emit_lifecycle(
                "gateway.run.started",
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
            )
            await ep.send(
                frame(type="run.started", session_id=session_id, turn_id=turn_id, run_id=run_id)
            )
            request = self._build_request(
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                user_text=user_text,
                history=history,
                payload=payload,
                interrupts_active_run=interrupts_active_run,
            )
            result = await self._run_backend(request, ep=ep, turn_id=turn_id, cancel=cancel)
            expects_reply = bool(result.expects_reply)

            if cancel.is_cancelled() or result.status == "cancelled":
                end_reason = "cancelled"
                expects_reply = True
            elif result.status == "error":
                end_reason = "error"
            else:
                end_reason = "completed"

            if end_reason == "error":
                await ep.send(
                    frame(
                        type="run.end",
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        reason="error",
                        error=_result_error(result),
                        payload=_run_end_payload(
                            result=result,
                            expects_reply=True,
                            run_id=run_id,
                        ),
                    )
                )
            else:
                await ep.send(
                    frame(
                        type="run.end",
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        reason=end_reason,
                        payload=_run_end_payload(
                            result=result,
                            expects_reply=expects_reply,
                            run_id=run_id,
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - protocol boundary.
            if cancel.is_cancelled():
                end_reason = "cancelled"
                await ep.send(
                    frame(
                        type="run.end",
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        reason="cancelled",
                        payload=_run_end_payload(
                            result=RealtimeAgentResult(
                                status="cancelled",
                                run_id=run_id,
                                expects_reply=True,
                                metadata={
                                    **cancel.cancel_metadata,
                                    "cancel_phase": "gateway_exception",
                                    "best_effort": True,
                                },
                            ),
                            expects_reply=True,
                            run_id=run_id,
                        ),
                    )
                )
            else:
                end_reason = "error"
                await ep.send(
                    frame(
                        type="run.end",
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        reason="error",
                        error={"message": str(exc), "error_type": type(exc).__name__},
                        payload={
                            "expects_reply": True,
                            "supersedes": [progress_replacement_key(run_id)],
                        },
                    )
                )
        finally:
            deadline_task: asyncio.Task[None] | None = None
            next_turn: QueuedTurn | None = None
            permit: RunPermit | None = None
            async with self._lock:
                cur = self._active_by_session.get(session_id)
                if cur and cur.run_id == run_id:
                    deadline_task = cur.deadline_task
                    permit = cur.permit
                    active_trace_id = cur.trace_id
                    self._active_by_session.pop(session_id, None)
                current = self._current_by_session.get(session_id)
                if current is not None and current.run_id == run_id:
                    self._set_turn_state(current, "terminal")
                    self._current_by_session.pop(session_id, None)
                    self._turns_by_run_id.pop(run_id, None)
                    next_turn = self._promote_next_locked(session_id)
            if deadline_task is not None:
                deadline_task.cancel()
                await asyncio.gather(deadline_task, return_exceptions=True)
            if permit is not None:
                await self._admission.release_permit(permit)
            terminal_payload: dict[str, Any] = {
                "reason": end_reason,
                "expects_reply": expects_reply,
            }
            if result is not None and result.trace_id:
                terminal_payload["trace_id"] = result.trace_id
            elif active_trace_id:
                terminal_payload["trace_id"] = active_trace_id
            self._emit_lifecycle(
                _terminal_lifecycle_event_type(end_reason),
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                payload=terminal_payload,
            )
            if next_turn is not None and not self._closed:
                self._schedule_dispatch(next_turn)

    async def _run_backend(
        self,
        request: RealtimeAgentRequest,
        *,
        ep: Endpoint,
        turn_id: str,
        cancel: CancelToken,
    ) -> RealtimeAgentResult:
        queue: asyncio.Queue[Frame] = asyncio.Queue()

        async def event_sink(event: RealtimeAgentEvent) -> None:
            await self._observe_active_run_correlation(request, event)
            if cancel.is_cancelled():
                return
            mapped = realtime_event_to_frame(
                event,
                session_id=request.session_id,
                turn_id=turn_id,
                run_id=request.run_id or "",
            )
            if mapped is not None and not cancel.is_cancelled():
                await queue.put(mapped)

        task = asyncio.create_task(
            self._resolve_backend().run_turn(
                request,
                event_sink=event_sink,
                cancel_token=cancel,
            )
        )
        cancel_wait = asyncio.create_task(cancel.cancelled())
        queue_wait: asyncio.Task[Frame] | None = None

        try:
            while True:
                if cancel.is_cancelled():
                    _discard_queued_frames(queue)
                    return await _finish_cancelled_backend(
                        task=task,
                        request=request,
                        cancel=cancel,
                    )

                while not queue.empty():
                    outbound = queue.get_nowait()
                    if cancel.is_cancelled():
                        _discard_queued_frames(queue)
                        return await _finish_cancelled_backend(
                            task=task,
                            request=request,
                            cancel=cancel,
                        )
                    await ep.send(outbound)

                if task.done():
                    return await task

                if queue_wait is None or queue_wait.done():
                    queue_wait = asyncio.create_task(queue.get())

                done, _ = await asyncio.wait(
                    {task, cancel_wait, queue_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if cancel_wait in done or cancel.is_cancelled():
                    _discard_queued_frames(queue)
                    return await _finish_cancelled_backend(
                        task=task,
                        request=request,
                        cancel=cancel,
                    )

                if queue_wait in done:
                    outbound = queue_wait.result()
                    queue_wait = None
                    if not cancel.is_cancelled():
                        await ep.send(outbound)
        finally:
            cancel_wait.cancel()
            pending: list[asyncio.Task[Any]] = [cancel_wait]
            if queue_wait is not None and not queue_wait.done():
                queue_wait.cancel()
                pending.append(queue_wait)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _observe_active_run_correlation(
        self,
        request: RealtimeAgentRequest,
        event: RealtimeAgentEvent,
    ) -> None:
        if event.type != "run.progress" or event.payload.get("agent_event_type") != "task_started":
            return
        trace_id = _optional_string(event.payload.get("trace_id"))
        if not trace_id:
            return
        async with self._lock:
            active = self._active_by_session.get(request.session_id)
            if active is None or active.run_id != request.run_id:
                return
            active.trace_id = trace_id or active.trace_id

    async def _interrupt_if_needed(
        self,
        *,
        session_id: str,
        expected_run_id: str | None = None,
    ) -> None:
        interrupted: ActiveRun | None = None
        async with self._lock:
            cur = self._active_by_session.get(session_id)
            if not cur or (
                expected_run_id is not None and cur.run_id != expected_run_id
            ):
                return
            cur.cancel.cancel(source="gateway_interrupt")
            interrupted = cur
        if interrupted is None:
            return
        self._emit_lifecycle(
            "gateway.run.cancel_requested",
            session_id=session_id,
            run_id=interrupted.run_id,
            turn_id=interrupted.turn_id,
            payload={
                "source": "gateway_interrupt",
                **_active_run_correlation_payload(interrupted),
            },
        )

    async def _handle_cancel(self, ep: Endpoint, f: Frame) -> None:
        run_id = f.get("run_id")
        session_id = f.get("session_id")
        payload = _payload_dict(f)
        cancel_source = _cancel_source_from_payload(payload)
        cancel_reason = _optional_string(payload.get("reason"))
        did_cancel = False
        cancelled_session_id: str | None = None
        cancelled_run_id: str | None = None
        cancelled_turn_id: str | None = None
        cancelled_trace_id: str | None = None

        queued_targets: list[QueuedTurn] = []
        async with self._lock:
            if session_id and cancel_source in {"gateway_disconnect", "gateway_hangup"}:
                queued_targets = [
                    turn
                    for turn in self._turns_by_run_id.values()
                    if turn.session_id == str(session_id)
                    and turn.state not in {"running", "terminal"}
                ]
            elif run_id:
                turn = self._turns_by_run_id.get(str(run_id))
                if (
                    turn is not None
                    and turn.state not in {"running", "terminal"}
                    and (session_id is None or turn.session_id == str(session_id))
                ):
                    queued_targets = [turn]

        queued_cancelled = False
        for turn in queued_targets:
            queued_cancelled = (
                await self._cancel_queued_turn(
                    turn,
                    source=cancel_source,
                    reason=cancel_reason,
                )
                or queued_cancelled
            )

        if queued_cancelled and cancel_source not in {
            "gateway_disconnect",
            "gateway_hangup",
        }:
            return

        async with self._lock:
            if session_id:
                cur = self._active_by_session.get(session_id)
                if cur and (run_id is None or cur.run_id == run_id):
                    cur.cancel.cancel(source=cancel_source, reason=cancel_reason)
                    did_cancel = True
                    cancelled_session_id = str(session_id)
                    cancelled_run_id = cur.run_id
                    cancelled_turn_id = cur.turn_id
                    cancelled_trace_id = cur.trace_id
            elif run_id:
                for active_session_id, cur in self._active_by_session.items():
                    if cur.run_id == run_id:
                        cur.cancel.cancel(source=cancel_source, reason=cancel_reason)
                        did_cancel = True
                        cancelled_session_id = active_session_id
                        cancelled_run_id = cur.run_id
                        cancelled_turn_id = cur.turn_id
                        cancelled_trace_id = cur.trace_id
                        break

        if did_cancel:
            cancel_payload: dict[str, Any] = {"source": cancel_source}
            if cancel_reason:
                cancel_payload["reason"] = cancel_reason
            if cancelled_trace_id:
                cancel_payload["trace_id"] = cancelled_trace_id
            self._emit_lifecycle(
                "gateway.run.cancel_requested",
                session_id=cancelled_session_id,
                run_id=cancelled_run_id,
                turn_id=cancelled_turn_id,
                payload=cancel_payload,
            )
            return

        if queued_cancelled:
            return

        await ep.send(
            frame(
                type="error",
                error={"code": "run_not_found", "run_id": run_id, "session_id": session_id},
            )
        )

    def _resolve_backend(self) -> RealtimeAgentBackend:
        if self._backend is not None:
            return self._backend
        if self._backend_factory is not None:
            self._backend = self._backend_factory()
        else:
            self._backend = GatewayRuntimeAdapter()
        return self._backend

    def _start_deadline_monitor(
        self,
        *,
        session_id: str,
        run_id: str,
        cancel: CancelToken,
        deadline_ms: int | None,
    ) -> asyncio.Task[None] | None:
        if deadline_ms is None:
            return None

        async def _monitor() -> None:
            await asyncio.sleep(deadline_ms / 1000)
            cancelled_run: ActiveRun | None = None
            async with self._lock:
                cur = self._active_by_session.get(session_id)
                if cur is None or cur.run_id != run_id or cur.cancel is not cancel:
                    return
                cur.cancel.cancel(
                    source="deadline",
                    reason="run_deadline_expired",
                    metadata={"deadline_ms": deadline_ms},
                )
                cancelled_run = cur
            if cancelled_run is None:
                return
            self._emit_lifecycle(
                "gateway.run.cancel_requested",
                session_id=session_id,
                run_id=cancelled_run.run_id,
                turn_id=cancelled_run.turn_id,
                payload={
                    "source": "deadline",
                    "reason": "run_deadline_expired",
                    "deadline_ms": deadline_ms,
                    **_active_run_correlation_payload(cancelled_run),
                },
            )

        return asyncio.create_task(_monitor(), name=f"gateway-run-deadline-{run_id}")

    def _build_request(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_text: str,
        history: list[str],
        payload: dict[str, Any],
        interrupts_active_run: bool = False,
    ) -> RealtimeAgentRequest:
        metadata = _user_message_metadata(payload)
        metadata["turn_mode"] = "replace" if interrupts_active_run else "followup"
        if interrupts_active_run:
            metadata.setdefault("control", "interrupt")
        gateway_metadata = metadata.get("gateway")
        gateway_payload = dict(gateway_metadata) if isinstance(gateway_metadata, dict) else {}
        if metadata.get("control") == "interrupt":
            gateway_payload.setdefault("control", "interrupt")
            gateway_payload.setdefault("interrupt", True)
        gateway_payload["history"] = list(history)
        gateway_payload["session_config"] = dict(self._config)
        _apply_trusted_system_prompt_config(metadata, self._config)
        metadata["gateway"] = gateway_payload
        metadata["runtime"] = gateway_payload

        return RealtimeAgentRequest(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            text=user_text,
            image_ids=_string_list(payload.get("image_ids")),
            video_ids=_string_list(payload.get("video_ids")),
            audio_id=_optional_string(payload.get("audio_id")),
            metadata=metadata,
        )


def _cancelled_realtime_result(
    *,
    request: RealtimeAgentRequest,
    cancel: CancelToken,
) -> RealtimeAgentResult:
    return RealtimeAgentResult(
        status="cancelled",
        run_id=request.run_id,
        expects_reply=True,
        metadata={
            **cancel.cancel_metadata,
            "cancel_phase": "gateway_output_gate",
            "best_effort": True,
        },
    )


async def _finish_cancelled_backend(
    *,
    task: asyncio.Task[RealtimeAgentResult],
    request: RealtimeAgentRequest,
    cancel: CancelToken,
) -> RealtimeAgentResult:
    try:
        await task
    except Exception:
        pass
    return _cancelled_realtime_result(request=request, cancel=cancel)


def _run_end_payload(
    *,
    result: RealtimeAgentResult,
    expects_reply: bool,
    run_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "expects_reply": expects_reply,
        "supersedes": [progress_replacement_key(run_id)],
    }
    if result.trace_id:
        payload["trace_id"] = result.trace_id
    if result.status == "cancelled":
        cancel_payload = _run_end_cancel_payload(result.metadata)
        if cancel_payload:
            payload["cancel"] = cancel_payload
        if not result.trace_id:
            payload["trace"] = {
                "status": "not_available",
                "reason": "cancelled_before_backend_result",
            }
    return payload


def _run_end_cancel_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    source = _prompt_safe_optional_string(metadata.get("cancel_source"))
    if source:
        payload["source"] = source
    reason = _prompt_safe_optional_string(metadata.get("cancel_reason"))
    if reason:
        payload["reason"] = reason
    phase = _prompt_safe_optional_string(metadata.get("cancel_phase"))
    if phase:
        payload["phase"] = phase
    best_effort = metadata.get("best_effort")
    if isinstance(best_effort, bool):
        payload["best_effort"] = best_effort
    deadline_ms = _positive_int(metadata.get("deadline_ms"))
    if deadline_ms is not None:
        payload["deadline_ms"] = deadline_ms
    contract = realtime_turn_cancellation_from_metadata(metadata)
    payload["cancelled_by"] = contract.cancelled_by
    payload["phase"] = contract.phase
    payload["stale_outputs"] = contract.stale_outputs
    payload["can_reuse_tool_result"] = contract.can_reuse_tool_result
    payload["speakable"] = contract.speakable
    return payload


def _prompt_safe_optional_string(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    return sanitize_error_message(text)


def _discard_queued_frames(queue: asyncio.Queue[Frame]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    def _consume(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    if task.done():
        _consume(task)
    else:
        task.add_done_callback(_consume)


@dataclass
class GatewaySessionHandle:
    user_id: str
    endpoint: Endpoint
    created: bool
    resumed: bool
    active_count: int
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class GatewayConfigUpdateResult:
    user_id: str
    online: bool
    config: dict[str, Any] = field(default_factory=dict)


class _TouchableEndpoint:
    """Proxy endpoint that refreshes session activity on every frame."""

    def __init__(self, inner: Endpoint, touch_fn: Callable[[], None]) -> None:
        self._inner = inner
        self._touch = touch_fn

    async def send(self, f: Frame) -> None:
        self._touch()
        await self._inner.send(f)

    def _inject(self, f: Frame) -> None:
        self._touch()
        self._inner._inject(f)

    async def close(self) -> None:
        await self._inner.close()

    async def __aiter__(self) -> AsyncIterator[Frame]:
        async for f in self._inner:
            self._touch()
            yield f


class _GatewaySessionEntry:
    def __init__(
        self,
        *,
        user_id: str,
        service: GatewaySessionService,
        gateway_ep: Endpoint,
        session_ep: Endpoint,
    ) -> None:
        self.user_id = user_id
        self.service = service
        self.gateway_ep = _TouchableEndpoint(gateway_ep, self.touch)
        self.session_ep = session_ep
        self.last_active = time.monotonic()
        self.task: asyncio.Task[None] | None = None

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_active

    def start(self) -> None:
        self.task = asyncio.create_task(
            self.service.serve(self.session_ep),
            name=f"gateway-session-{self.user_id}",
        )

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()


class GatewaySessionManager:
    """Manage per-user GatewaySessionService instances.

    The manager keeps one in-process logical AgentSession per user, reuses it
    across transport reconnects, updates live config, and evicts idle sessions.
    Explicit hangup destroys the session through ``destroy``; execution runtimes
    remain owned by the separate application runtime pool.
    """

    def __init__(
        self,
        *,
        max_sessions: int = 20,
        idle_timeout_s: float = 300.0,
        reaper_interval_s: float = 30.0,
        backend_factory: Callable[[], RealtimeAgentBackend] | None = None,
        service_factory: Callable[[str, Mapping[str, Any]], GatewaySessionService] | None = None,
        start_reaper: bool = True,
        lifecycle_sink: GatewayLifecycleSink | None = None,
        queue_policy: GatewayQueuePolicy | None = None,
        admission_controller: GatewayRunAdmissionController | None = None,
        turn_arbitration_controller: GatewayTurnArbitrationController | None = None,
        session_initializer: (
            Callable[[str, str, Mapping[str, Any]], Awaitable[None]] | None
        ) = None,
    ) -> None:
        self.max_sessions = max_sessions
        self.idle_timeout_s = idle_timeout_s
        self.reaper_interval_s = reaper_interval_s
        self.backend_factory = backend_factory
        self.service_factory = service_factory
        self.lifecycle_sink = lifecycle_sink
        self.queue_policy = queue_policy or GatewayQueuePolicy()
        self.admission_controller = admission_controller or GatewayRunAdmissionController(
            self.queue_policy
        )
        self._owns_admission_controller = admission_controller is None
        self.turn_arbitration_controller = (
            turn_arbitration_controller or GatewayTurnArbitrationController()
        )
        self.session_initializer = session_initializer
        self._entries: dict[str, _GatewaySessionEntry] = {}
        self._deferred_config: dict[str, dict[str, Any]] = {}
        self._initialized_sessions: set[tuple[str, str]] = set()
        self._session_initializations: dict[
            tuple[str, str], asyncio.Task[None]
        ] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._start_reaper = start_reaper

    async def initialize_session(
        self,
        *,
        user_id: str,
        session_id: str,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize one logical session before its first turn is dispatched."""

        key = (user_id, session_id)
        async with self._lock:
            if key in self._initialized_sessions:
                return
            task = self._session_initializations.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._initialize_session(
                        user_id=user_id,
                        session_id=session_id,
                        config=dict(config or {}),
                    ),
                    name="gateway-session-initialize",
                )
                self._session_initializations[key] = task
        try:
            await task
        except BaseException:
            async with self._lock:
                self._session_initializations.pop(key, None)
            raise
        async with self._lock:
            self._session_initializations.pop(key, None)
            self._initialized_sessions.add(key)

    async def _initialize_session(
        self,
        *,
        user_id: str,
        session_id: str,
        config: Mapping[str, Any],
    ) -> None:
        if self.session_initializer is not None:
            await self.session_initializer(user_id, session_id, config)

    async def get_or_create(
        self,
        user_id: str,
        config: Mapping[str, Any] | None = None,
    ) -> Endpoint:
        """Return the Gateway-side endpoint for a user session."""

        return (await self.acquire(user_id=user_id, config=config)).endpoint

    async def acquire(
        self,
        *,
        user_id: str,
        config: Mapping[str, Any] | None = None,
    ) -> GatewaySessionHandle:
        async with self._lock:
            if user_id in self._entries:
                entry = self._entries[user_id]
                if config:
                    entry.service.update_config(config)
                entry.touch()
                self._emit_lifecycle(
                    "gateway.session.acquired",
                    user_id=user_id,
                    payload={
                        "created": False,
                        "resumed": True,
                        "active_count": len(self._entries),
                    },
                )
                return GatewaySessionHandle(
                    user_id=user_id,
                    endpoint=entry.gateway_ep,  # type: ignore[arg-type]
                    created=False,
                    resumed=True,
                    active_count=len(self._entries),
                    config=entry.service.config,
                )

            if len(self._entries) >= self.max_sessions:
                raise RuntimeError(
                    f"gateway_session_limit_reached: max {self.max_sessions} sessions already running"
                )

            merged_config = dict(self._deferred_config.pop(user_id, {}))
            if config:
                merged_config.update(dict(config))
            entry = self._new_entry(user_id=user_id, config=merged_config)
            entry.start()
            self._entries[user_id] = entry
            self._ensure_reaper()
            self._emit_lifecycle(
                "gateway.session.acquired",
                user_id=user_id,
                payload={
                    "created": True,
                    "resumed": False,
                    "active_count": len(self._entries),
                },
            )
            return GatewaySessionHandle(
                user_id=user_id,
                endpoint=entry.gateway_ep,  # type: ignore[arg-type]
                created=True,
                resumed=False,
                active_count=len(self._entries),
                config=entry.service.config,
            )

    async def update_config(
        self,
        user_id: str,
        values: Mapping[str, Any],
    ) -> GatewayConfigUpdateResult:
        payload = {str(key): value for key, value in values.items()}
        async with self._lock:
            entry = self._entries.get(user_id)
            if entry is not None:
                entry.service.update_config(payload)
                return GatewayConfigUpdateResult(
                    user_id=user_id,
                    online=True,
                    config=entry.service.config,
                )
            deferred = self._deferred_config.setdefault(user_id, {})
            deferred.update(payload)
            return GatewayConfigUpdateResult(
                user_id=user_id,
                online=False,
                config=dict(deferred),
            )

    async def destroy(self, user_id: str) -> bool:
        async with self._lock:
            entry = self._entries.pop(user_id, None)
            self._initialized_sessions = {
                key for key in self._initialized_sessions if key[0] != user_id
            }
            initialization_tasks = [
                self._session_initializations.pop(key)
                for key in list(self._session_initializations)
                if key[0] == user_id
            ]
        for task in initialization_tasks:
            task.cancel()
        if initialization_tasks:
            await asyncio.gather(*initialization_tasks, return_exceptions=True)
        if entry is None:
            return False
        await entry.service.close(source="gateway_disconnect")
        entry.stop()
        if entry.task is not None:
            await asyncio.gather(entry.task, return_exceptions=True)
        await entry.gateway_ep.close()
        await entry.session_ep.close()
        self._emit_lifecycle(
            "gateway.session.destroyed",
            user_id=user_id,
            payload={"active_count": self.active_count()},
        )
        return True

    async def reap_once(self) -> list[str]:
        evict: list[str] = []
        async with self._lock:
            for user_id, entry in self._entries.items():
                if entry.idle_seconds() >= self.idle_timeout_s:
                    evict.append(user_id)
        for user_id in evict:
            await self.destroy(user_id)
        return evict

    def active_count(self) -> int:
        return len(self._entries)

    def has_active_session(self, user_id: str) -> bool:
        return user_id in self._entries

    async def has_active_run(self, user_id: str) -> bool:
        """Return active-run state without creating or touching a session."""

        async with self._lock:
            entry = self._entries.get(user_id)
        if entry is None:
            return False
        return await entry.service.has_active_run()

    def _emit_lifecycle(
        self,
        event_type: str,
        *,
        user_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        emit_gateway_lifecycle_event(
            self.lifecycle_sink,
            type=event_type,
            user_id=user_id,
            payload=payload,
        )

    async def close(self) -> None:
        """Stop the reaper and close all managed user sessions."""

        if self._reaper_task is not None and not self._reaper_task.done():
            self._reaper_task.cancel()
            await asyncio.gather(self._reaper_task, return_exceptions=True)
        self._reaper_task = None
        async with self._lock:
            user_ids = list(self._entries)
        for user_id in user_ids:
            await self.destroy(user_id)
        if self._owns_admission_controller:
            await self.admission_controller.close()

    def session_config(self, user_id: str) -> dict[str, Any] | None:
        entry = self._entries.get(user_id)
        if entry is not None:
            return entry.service.config
        deferred = self._deferred_config.get(user_id)
        return dict(deferred) if deferred is not None else None

    def _new_entry(self, *, user_id: str, config: Mapping[str, Any]) -> _GatewaySessionEntry:
        gateway_ep, session_ep = InMemoryDuplex.create_pair()
        if self.service_factory is not None:
            service = self.service_factory(user_id, config)
        else:
            service = GatewaySessionService(
                user_id=user_id,
                backend_factory=self.backend_factory,
                config=config,
                lifecycle_sink=self.lifecycle_sink,
                queue_policy=self.queue_policy,
                admission_controller=self.admission_controller,
                turn_arbitration_controller=self.turn_arbitration_controller,
            )
        if self.service_factory is not None:
            service.bind_queueing(
                queue_policy=self.queue_policy,
                admission_controller=self.admission_controller,
            )
            service.bind_turn_arbitration(self.turn_arbitration_controller)
        return _GatewaySessionEntry(
            user_id=user_id,
            service=service,
            gateway_ep=gateway_ep,
            session_ep=session_ep,
        )

    def _ensure_reaper(self) -> None:
        if not self._start_reaper:
            return
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reaper_loop(),
                name="gateway-session-reaper",
            )

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reaper_interval_s)
            await self.reap_once()


def _payload_dict(f: Frame) -> dict[str, Any]:
    payload = f.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _terminal_lifecycle_event_type(reason: str) -> str:
    if reason == "completed":
        return "gateway.run.completed"
    if reason == "cancelled":
        return "gateway.run.cancelled"
    return "gateway.run.errored"


def _run_timeout_ms(payload: Mapping[str, Any], session_config: Mapping[str, Any]) -> int | None:
    metadata = payload.get("metadata")
    gateway_metadata = metadata.get("gateway") if isinstance(metadata, Mapping) else None
    if isinstance(gateway_metadata, Mapping) and "run_timeout_ms" in gateway_metadata:
        return _positive_int(gateway_metadata.get("run_timeout_ms"))
    return _positive_int(session_config.get("run_timeout_ms"))


def _user_message_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(payload.get("metadata") or {})
    trusted_source = _trusted_entry_source(metadata)
    for key in ("system_prompt_profile", "channel", "source"):
        metadata.pop(key, None)
    if trusted_source is not None:
        metadata["source"] = trusted_source
    return metadata


def _trusted_entry_source(metadata: Mapping[str, Any]) -> str | None:
    source = _optional_string(metadata.get("source"))
    if source != "gateway_websocket":
        return None
    if metadata.get("transport") != "websocket":
        return None
    return source if isinstance(metadata.get("request_identity"), Mapping) else None


def _apply_trusted_system_prompt_config(metadata: dict[str, Any], session_config: Mapping[str, Any]) -> None:
    profile = _optional_string(session_config.get("system_prompt_profile"))
    if profile == "text_default":
        metadata["system_prompt_profile"] = profile
    elif profile == "realtime_phone":
        metadata["system_prompt_profile"] = "text_default"
    channel = _optional_string(session_config.get("channel"))
    if channel in {"text", "phone", "realtime_phone"}:
        metadata["channel"] = channel


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _cancel_source_from_payload(payload: Mapping[str, Any]) -> str:
    source = payload.get("source")
    if source in {"gateway_cancel", "gateway_interrupt", "gateway_hangup", "gateway_disconnect"}:
        return str(source)
    return "gateway_cancel"


def _message_requests_interrupt(payload: Mapping[str, Any], session_config: Mapping[str, Any]) -> bool:
    if payload.get("interrupt") is True:
        return True
    control = _metadata_control(payload)
    if control in {"interrupt", "barge_in", "cancel_previous"}:
        return True
    policy = _optional_string(session_config.get("interrupt_policy"))
    return policy in {"interrupt", "barge_in", "cancel_previous"}


def _message_turn_mode(payload: Mapping[str, Any]) -> tuple[str, bool]:
    raw = payload.get("mode")
    if raw is None:
        raw = payload.get("turn_mode")
    if raw is None:
        return "followup", False
    mode = _optional_string(raw)
    if mode not in {"followup", "replace"}:
        raise ValueError("message.user mode must be followup or replace")
    return mode, True


def _metadata_control(payload: Mapping[str, Any]) -> str | None:
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        return _optional_string(metadata.get("control"))
    return None


def _attach_arbitration_metadata(
    turn: QueuedTurn,
    decision: RealtimeTurnArbitrationDecision,
) -> None:
    metadata_value = turn.payload.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    metadata["barge_in_source"] = "transcript"
    metadata[REALTIME_TURN_ARBITRATION_METADATA_KEY] = decision.model_dump(mode="json")
    turn.payload["metadata"] = metadata


def _arbitration_lifecycle_payload(
    decision: RealtimeTurnArbitrationDecision,
    *,
    controller_status: str,
    normalized_disposition: str,
    expected_run_matched: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "decision_id": decision.decision_id,
        "source": decision.source,
        "disposition": decision.disposition,
        "normalized_disposition": normalized_disposition,
        "confidence_bucket": _confidence_bucket(decision.confidence),
        "reason_code": decision.reason_code,
        "latency_ms": decision.latency_ms,
        "controller_status": controller_status,
        "expected_run_matched": expected_run_matched,
    }
    if decision.fallback_reason:
        payload["fallback_reason"] = decision.fallback_reason
    return payload


def _prompt_safe_arbitration_summary(
    decision: RealtimeTurnArbitrationDecision,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": decision.schema_version,
        "decision_id": decision.decision_id,
        "source": decision.source,
        "disposition": decision.disposition,
        "confidence_bucket": _confidence_bucket(decision.confidence),
        "reason_code": decision.reason_code,
        "expected_run_id": decision.expected_run_id,
        "latency_ms": decision.latency_ms,
    }
    if decision.revision_type:
        payload["revision_type"] = decision.revision_type
    if decision.fallback_reason:
        payload["fallback_reason"] = decision.fallback_reason
    return payload


def _confidence_bucket(value: float) -> str:
    if value >= 0.90:
        return "high"
    if value >= 0.80:
        return "medium"
    return "low"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _active_run_correlation_payload(active: ActiveRun) -> dict[str, str]:
    payload: dict[str, str] = {}
    if active.trace_id:
        payload["trace_id"] = active.trace_id
    return payload


def _result_error(result: RealtimeAgentResult) -> dict[str, Any]:
    metadata = dict(result.metadata)
    return {
        "message": metadata.get("error_message") or "assistant_agent backend error",
        "error_type": metadata.get("error_type"),
        "metadata": metadata,
    }
