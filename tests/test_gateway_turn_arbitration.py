from __future__ import annotations

import asyncio

from assistant_agent.gateway import (
    GatewayQueuePolicy,
    GatewaySessionService,
    GatewayTurnArbitrationController,
    GatewayTurnArbitrationPolicy,
    InMemoryDuplex,
    frame,
)
from assistant_agent.realtime import RealtimeAgentResult
from assistant_agent.schemas.realtime_turn_arbitration import normalize_arbitration_decision
from assistant_agent.services.realtime_task_state import (
    InMemoryRealtimeTaskStateStore,
    RealtimeTaskState,
)


async def _close_session(client_ep, session_ep, session_task) -> None:
    await client_ep.close()
    await session_ep.close()
    session_task.cancel()
    await asyncio.gather(session_task, return_exceptions=True)


async def _read_frame_type(endpoint, frame_type: str, *, run_id: str | None = None):
    async def _read():
        async for received in endpoint:
            if received.get("type") != frame_type:
                continue
            if run_id is None or received.get("run_id") == run_id:
                return received
        raise AssertionError(f"endpoint closed before {frame_type}")

    return await asyncio.wait_for(_read(), timeout=3.0)


def _semantic_payload(text: str, *, run_id: str, turn_id: str) -> dict:
    return {
        "text": text,
        "run_id": run_id,
        "turn_id": turn_id,
        "metadata": {
            "source": "realtime_media_websocket",
            "gateway": {
                "entry_capabilities": {
                    "supports_semantic_interrupt": True,
                    "supports_realtime_task_state": True,
                }
            },
        },
    }


class _ScriptedArbiter:
    def __init__(
        self,
        disposition: str,
        *,
        revision_type: str | None = None,
        wait_for_release: bool = False,
    ) -> None:
        self.disposition = disposition
        self.revision_type = revision_type
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not wait_for_release:
            self.release.set()

    async def arbitrate(self, request):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return normalize_arbitration_decision(
            {
                "disposition": self.disposition,
                "revision_type": self.revision_type,
                "confidence": 0.99,
                "reason_code": "scripted_gateway_test",
            },
            request=request,
            min_confidence=0.0,
            source="semantic_llm",
        )


def _controller(arbiter) -> GatewayTurnArbitrationController:
    return GatewayTurnArbitrationController(
        policy=GatewayTurnArbitrationPolicy(
            enabled=True,
            timeout_ms=1000,
            max_concurrency=2,
            min_confidence=0.80,
        ),
        arbiter=arbiter,
    )


class _ImmediateBackend:
    def __init__(self) -> None:
        self.requests = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        return RealtimeAgentResult(status="completed", run_id=request.run_id)


class _FollowupBackend:
    def __init__(self) -> None:
        self.requests = []
        self.release_first = asyncio.Event()
        self.first_cancelled = False

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        if request.text == "first":
            release_wait = asyncio.create_task(self.release_first.wait())
            cancel_wait = asyncio.create_task(cancel_token.cancelled())
            done, pending = await asyncio.wait(
                {release_wait, cancel_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            self.first_cancelled = cancel_wait in done
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return RealtimeAgentResult(
                status="cancelled" if self.first_cancelled else "completed",
                run_id=request.run_id,
            )
        return RealtimeAgentResult(status="completed", run_id=request.run_id)


class _SerializedInterruptBackend:
    def __init__(self) -> None:
        self.requests = []
        self.cancel_seen = asyncio.Event()
        self.release_first = asyncio.Event()
        self.second_started = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if request.text == "first":
                await cancel_token.cancelled()
                self.cancel_seen.set()
                await self.release_first.wait()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)
            self.second_started.set()
            return RealtimeAgentResult(status="completed", run_id=request.run_id)
        finally:
            self.active -= 1


def test_semantic_arbiter_is_not_called_without_active_run() -> None:
    async def scenario() -> None:
        backend = _ImmediateBackend()
        arbiter = _ScriptedArbiter("CANCEL_ONLY")
        session = GatewaySessionService(
            backend=backend,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-no-active",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.end", run_id="r1")
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert len(backend.requests) == 1
        assert arbiter.requests == []

    asyncio.run(scenario())


def test_explicit_interrupt_bypasses_semantic_arbiter() -> None:
    async def scenario() -> None:
        backend = _SerializedInterruptBackend()
        arbiter = _ScriptedArbiter("FOLLOWUP")
        session = GatewaySessionService(
            backend=backend,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-explicit",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            payload = _semantic_payload("stop now", run_id="r2", turn_id="t2")
            payload["interrupt"] = True
            await client_ep.send(
                frame(type="message.user", session_id="semantic-explicit", payload=payload)
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(backend.cancel_seen.wait(), timeout=1.0)
            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r2")
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert arbiter.requests == []
        assert backend.requests[1].metadata["control"] == "interrupt"

    asyncio.run(scenario())


def test_followup_decision_leaves_old_run_active_and_preserves_fifo() -> None:
    async def scenario() -> None:
        backend = _FollowupBackend()
        arbiter = _ScriptedArbiter("FOLLOWUP")
        session = GatewaySessionService(
            backend=backend,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-followup",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-followup",
                    payload=_semantic_payload("new task", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(arbiter.started.wait(), timeout=1.0)
            await asyncio.sleep(0)
            assert backend.first_cancelled is False
            assert [request.text for request in backend.requests] == ["first"]

            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r2")
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert backend.first_cancelled is False
        assert [request.text for request in backend.requests] == ["first", "new task"]
        assert "control" not in backend.requests[1].metadata

    asyncio.run(scenario())


def test_arbitration_runs_in_background_and_receive_loop_stays_responsive() -> None:
    async def scenario() -> None:
        backend = _FollowupBackend()
        arbiter = _ScriptedArbiter("FOLLOWUP", wait_for_release=True)
        session = GatewaySessionService(
            backend=backend,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-background",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-background",
                    payload=_semantic_payload("maybe followup", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(arbiter.started.wait(), timeout=1.0)

            await client_ep.send(frame(type="ping"))
            pong = await _read_frame_type(client_ep, "pong")
            assert pong["type"] == "pong"

            arbiter.release.set()
            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r2")
        finally:
            arbiter.release.set()
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

    asyncio.run(scenario())


def test_revise_decision_cancels_expected_run_and_waits_for_backend_exit() -> None:
    async def scenario() -> None:
        backend = _SerializedInterruptBackend()
        arbiter = _ScriptedArbiter(
            "REVISE_ACTIVE",
            revision_type="replace_constraint",
        )
        session = GatewaySessionService(
            backend=backend,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-revise",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-revise",
                    payload=_semantic_payload("change constraint", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(backend.cancel_seen.wait(), timeout=1.0)
            try:
                await asyncio.wait_for(backend.second_started.wait(), timeout=0.05)
                raise AssertionError("replacement started before cancelled backend exited")
            except asyncio.TimeoutError:
                pass

            backend.release_first.set()
            await asyncio.wait_for(backend.second_started.wait(), timeout=1.0)
            await _read_frame_type(client_ep, "run.end", run_id="r2")
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert backend.max_active == 1
        replacement = backend.requests[1]
        assert replacement.metadata["control"] == "interrupt"
        assert replacement.metadata["realtime_turn_arbitration"]["disposition"] == "REVISE_ACTIVE"
        assert replacement.metadata["realtime_turn_arbitration"]["revision_type"] == "replace_constraint"

    asyncio.run(scenario())


def test_replace_decision_normalizes_replacement_revision_to_change_goal() -> None:
    async def scenario() -> None:
        backend = _SerializedInterruptBackend()
        arbiter = _ScriptedArbiter(
            "REPLACE_ACTIVE",
            revision_type="add_constraint",
        )
        session = GatewaySessionService(
            backend=backend,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-replace",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-replace",
                    payload=_semantic_payload("entirely new goal", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(backend.cancel_seen.wait(), timeout=1.0)
            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r2")
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        replacement = backend.requests[1]
        arbitration = replacement.metadata["realtime_turn_arbitration"]
        assert arbitration["disposition"] == "REPLACE_ACTIVE"
        assert arbitration["revision_type"] == "change_goal"
        assert backend.max_active == 1

    asyncio.run(scenario())


def test_cancel_only_cancels_active_and_completes_control_turn_without_backend() -> None:
    async def scenario() -> None:
        backend = _SerializedInterruptBackend()
        arbiter = _ScriptedArbiter("CANCEL_ONLY")
        task_store = InMemoryRealtimeTaskStateStore()
        task_store.save(
            RealtimeTaskState(
                task_id="rtask:user-1:semantic-cancel-only",
                user_id="default",
                session_id="semantic-cancel-only",
                objective="long-running lookup",
            )
        )
        lifecycle = []
        session = GatewaySessionService(
            backend=backend,
            lifecycle_sink=lifecycle.append,
            turn_arbitration_controller=_controller(arbiter),
            realtime_task_state_store=task_store,
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-cancel-only",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-cancel-only",
                    payload=_semantic_payload("stop secret lookup", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(backend.cancel_seen.wait(), timeout=1.0)
            control_end = await _read_frame_type(client_ep, "run.end", run_id="r2")
            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r1")
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert control_end["reason"] == "completed"
        assert control_end["payload"]["handled_by"] == "turn_arbiter"
        assert control_end["payload"]["expects_reply"] is False
        assert control_end["payload"]["arbitration"]["disposition"] == "CANCEL_ONLY"
        assert "stop secret lookup" not in str(control_end)
        assert [request.text for request in backend.requests] == ["first"]
        state = task_store.get("default", "semantic-cancel-only")
        assert state is not None
        assert state.status == "cancelled"
        assert state.revisions[-1].revision_type == "cancel_goal"
        assert "stop secret lookup" not in str([event.payload for event in lifecycle])

    asyncio.run(scenario())


def test_ack_noop_completes_control_turn_without_cancelling_active() -> None:
    async def scenario() -> None:
        backend = _FollowupBackend()
        arbiter = _ScriptedArbiter("ACK_NOOP")
        session = GatewaySessionService(
            backend=backend,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-ack",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-ack",
                    payload=_semantic_payload("嗯嗯", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            control_end = await _read_frame_type(client_ep, "run.end", run_id="r2")

            assert control_end["reason"] == "completed"
            assert control_end["payload"]["expects_reply"] is False
            assert control_end["payload"]["arbitration"]["disposition"] == "ACK_NOOP"
            assert backend.first_cancelled is False
            assert [request.text for request in backend.requests] == ["first"]

            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r1")
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert [request.text for request in backend.requests] == ["first"]

    asyncio.run(scenario())


def test_late_revise_decision_becomes_followup_instead_of_cancelling_next_run() -> None:
    async def scenario() -> None:
        backend = _FollowupBackend()
        arbiter = _ScriptedArbiter(
            "REVISE_ACTIVE",
            revision_type="add_constraint",
            wait_for_release=True,
        )
        lifecycle = []
        session = GatewaySessionService(
            backend=backend,
            lifecycle_sink=lifecycle.append,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-stale",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-stale",
                    payload=_semantic_payload("late revision", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(arbiter.started.wait(), timeout=1.0)

            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r1")
            arbiter.release.set()
            await _read_frame_type(client_ep, "run.end", run_id="r2")
        finally:
            arbiter.release.set()
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert backend.first_cancelled is False
        assert [request.text for request in backend.requests] == ["first", "late revision"]
        assert "control" not in backend.requests[1].metadata
        stale_events = [event for event in lifecycle if event.type == "gateway.turn.arbitration.stale"]
        assert len(stale_events) == 1
        assert stale_events[0].payload["expected_run_matched"] is False
        assert stale_events[0].payload["normalized_disposition"] == "FOLLOWUP"

    asyncio.run(scenario())


def test_queued_cancel_during_arbitration_never_starts_business_backend() -> None:
    async def scenario() -> None:
        backend = _FollowupBackend()
        arbiter = _ScriptedArbiter("REVISE_ACTIVE", wait_for_release=True)
        session = GatewaySessionService(
            backend=backend,
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-queued-cancel",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-queued-cancel",
                    payload=_semantic_payload("cancel pending", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(arbiter.started.wait(), timeout=1.0)
            await client_ep.send(
                frame(
                    type="run.cancel",
                    session_id="semantic-queued-cancel",
                    run_id="r2",
                    payload={"reason": "user_cancelled_pending_turn"},
                )
            )
            cancelled = await _read_frame_type(client_ep, "run.end", run_id="r2")

            assert cancelled["reason"] == "cancelled"
            assert [request.text for request in backend.requests] == ["first"]
            assert backend.first_cancelled is False

            arbiter.release.set()
            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r1")
        finally:
            arbiter.release.set()
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert [request.text for request in backend.requests] == ["first"]

    asyncio.run(scenario())


def test_queue_timeout_during_arbitration_cannot_revive_terminal_turn() -> None:
    async def scenario() -> None:
        backend = _FollowupBackend()
        arbiter = _ScriptedArbiter("REVISE_ACTIVE", wait_for_release=True)
        session = GatewaySessionService(
            backend=backend,
            queue_policy=GatewayQueuePolicy(queue_wait_timeout_ms=30),
            turn_arbitration_controller=_controller(arbiter),
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-queue-timeout",
                    payload=_semantic_payload("first", run_id="r1", turn_id="t1"),
                )
            )
            await _read_frame_type(client_ep, "run.started", run_id="r1")
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="semantic-queue-timeout",
                    payload=_semantic_payload("timed out revision", run_id="r2", turn_id="t2"),
                )
            )
            await _read_frame_type(client_ep, "run.queued", run_id="r2")
            await asyncio.wait_for(arbiter.started.wait(), timeout=1.0)
            timed_out = await _read_frame_type(client_ep, "run.end", run_id="r2")

            assert timed_out["reason"] == "cancelled"
            assert timed_out["payload"]["cancel"]["source"] == "queue_timeout"
            arbiter.release.set()
            await asyncio.sleep(0.05)
            assert [request.text for request in backend.requests] == ["first"]
            assert backend.first_cancelled is False

            backend.release_first.set()
            await _read_frame_type(client_ep, "run.end", run_id="r1")
        finally:
            arbiter.release.set()
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert [request.text for request in backend.requests] == ["first"]

    asyncio.run(scenario())
