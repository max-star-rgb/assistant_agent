from __future__ import annotations

import asyncio
import time
import unittest
from threading import Event
from types import SimpleNamespace

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.system_prompt_policy import SystemPromptProfile, render_system_instruction
from assistant_agent.config import ProviderConfig
from assistant_agent.gateway import InMemoryDuplex, GatewaySessionService, dumps_frame, frame, loads_frame
from assistant_agent.realtime import GatewayAgentAdapter, RealtimeAgentEvent, RealtimeAgentResult
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.services.assistant_run_service import InMemoryConversationStore, run_assistant_request
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.realtime_task_state import InMemoryRealtimeTaskStateStore


async def _close_session(client_ep, session_ep, session_task) -> None:
    await client_ep.close()
    await session_ep.close()
    session_task.cancel()
    await asyncio.gather(session_task, return_exceptions=True)


async def _collect_until_run_end(client_ep, *, timeout_s: float = 3.0):
    frames = []

    async def _read():
        async for received in client_ep:
            frames.append(received)
            if received["type"] == "run.end":
                return frames
        raise AssertionError("endpoint closed before run.end")

    return await asyncio.wait_for(_read(), timeout=timeout_s)


async def _assert_no_frame(client_ep, *, timeout_s: float = 0.08) -> None:
    async def _read_one():
        async for received in client_ep:
            return received
        return None

    try:
        received = await asyncio.wait_for(_read_one(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return
    raise AssertionError(f"unexpected frame after run end: {received}")


def _assert_gateway_cancel_payload(
    payload: dict,
    *,
    cancelled_by: str,
    phase: str,
) -> None:
    cancel = payload["cancel"]
    assert cancel["cancelled_by"] == cancelled_by
    assert cancel["phase"] == phase
    assert cancel["stale_outputs"] is True
    assert cancel["can_reuse_tool_result"] is False
    assert cancel["speakable"] is False


class CapturingChatAdapter:
    provider = "scripted-native"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            response_text="电话口径回答。",
            finish_reason="stop",
            message_kind="final_answer",
            provider=self.provider,
            model="gateway-profile-test",
        )


class GatewaySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_user_streams_via_realtime_backend(self) -> None:
        class RecordingBackend:
            def __init__(self) -> None:
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="assistant smoke"))
                return RealtimeAgentResult(status="completed", response_text="assistant smoke", expects_reply=True)

        backend = RecordingBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="smoke-session",
                    user_id="smoke-user",
                    payload={"text": "hello realtime", "turn_id": "turn-1"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == [
            "run.started",
            "stream.chunk",
            "run.end",
        ]
        assert frames[1]["payload"]["text"] == "assistant smoke"
        assert frames[-1]["reason"] == "completed"
        assert frames[-1]["payload"]["expects_reply"] is True
        assert len(backend.requests) == 1
        assert backend.requests[0].text == "hello realtime"
        assert backend.requests[0].user_id == "smoke-user"
        assert backend.requests[0].metadata["runtime"]["history"] == ["hello realtime"]

    async def test_user_payload_metadata_cannot_select_system_prompt_profile(self) -> None:
        class RecordingBackend:
            def __init__(self) -> None:
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        backend = RecordingBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="profile-session",
                    user_id="profile-user",
                    payload={
                        "text": "普通用户文本",
                        "metadata": {
                            "system_prompt_profile": "final_only",
                            "channel": "realtime_phone",
                            "source": "phone_runtime",
                        },
                    },
                )
            )
            await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert len(backend.requests) == 1
        request = backend.requests[0]
        assert "system_prompt_profile" not in request.metadata
        assert request.metadata.get("channel") != "realtime_phone"
        assert request.metadata.get("source") != "phone_runtime"

    async def test_session_config_can_select_realtime_phone_system_prompt_profile(self) -> None:
        class RecordingBackend:
            def __init__(self) -> None:
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        backend = RecordingBackend()
        session = GatewaySessionService(
            backend=backend,
            config={"system_prompt_profile": "realtime_phone", "channel": "realtime_phone"},
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="phone-profile-session",
                    user_id="phone-profile-user",
                    payload={"text": "喂，帮我查一下订单"},
                )
            )
            await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert len(backend.requests) == 1
        request = backend.requests[0]
        assert request.metadata["system_prompt_profile"] == "realtime_phone"
        assert request.metadata["channel"] == "realtime_phone"
        assert request.metadata["gateway"]["session_config"]["system_prompt_profile"] == "realtime_phone"

    async def test_message_user_streams_progress_via_realtime_backend(self) -> None:
        class ProgressBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                assert event_sink is not None
                await event_sink(
                    RealtimeAgentEvent(
                        type="run.progress",
                        text="Calling product_search.",
                        payload={
                            "stage": "tool",
                            "status": "working",
                            "current_step": "product_search",
                        },
                        display_only=True,
                    )
                )
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        session = GatewaySessionService(backend=ProgressBackend())
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="progress-session",
                    payload={"text": "show progress"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == [
            "run.started",
            "event.progress",
            "run.end",
        ]
        assert frames[1]["payload"]["text"] == "Calling product_search."
        assert frames[1]["payload"]["stage"] == "tool"
        assert frames[1]["payload"]["status"] == "working"
        assert frames[1]["payload"]["display_only"] is True

    async def test_run_end_includes_backend_trace_id(self) -> None:
        class TraceBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    trace_id="trace-realtime-1",
                    expects_reply=False,
                )

        session = GatewaySessionService(backend=TraceBackend())
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="trace-session",
                    payload={"text": "show trace"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["type"] == "run.end"
        assert frames[-1]["reason"] == "completed"
        assert frames[-1]["payload"]["trace_id"] == "trace-realtime-1"
        assert frames[-1]["payload"]["expects_reply"] is False

    async def test_message_user_streams_through_gateway_agent_adapter(self) -> None:
        captured: dict[str, object] = {}

        def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
            captured["request"] = request
            captured["cancel_token"] = kwargs.get("cancel_token")
            kwargs["event_sink"].emit(
                AgentEvent(
                    type="response_delta",
                    session_id=request.session_id,
                    run_id="main-runtime-run-1",
                    text="main runtime chunk",
                )
            )
            state = AgentState.from_request(request, run_id="main-runtime-run-1")
            state.set_response(AgentResponse(message="main runtime final"))
            return SimpleNamespace(state=state)

        session = GatewaySessionService(
            backend=GatewayAgentAdapter(run_request=fake_run_assistant_request)
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="adapter-session",
                    user_id="adapter-user",
                    payload={
                        "text": "hello adapter",
                        "turn_id": "turn-single",
                        "run_id": "gateway-run-single",
                    },
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == [
            "run.started",
            "stream.chunk",
            "run.end",
        ]
        assert frames[0]["turn_id"] == "turn-single"
        assert frames[0]["run_id"] == "gateway-run-single"
        assert frames[1]["payload"]["text"] == "main runtime chunk"
        assert frames[-1]["reason"] == "completed"
        request = captured["request"]
        assert isinstance(request, UserRequest)
        assert request.text == "hello adapter"
        assert request.metadata["realtime"]["turn_id"] == "turn-single"
        assert request.metadata["realtime"]["run_id"] == "gateway-run-single"
        assert captured["cancel_token"] is not None

    async def test_gateway_session_config_reaches_native_chat_request_system_prompt(self) -> None:
        adapter = CapturingChatAdapter()
        runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)
        conversation_store = InMemoryConversationStore()
        task_state_store = InMemoryRealtimeTaskStateStore()

        def run_with_runtime(request: UserRequest, **kwargs) -> SimpleNamespace:
            run_kwargs = dict(kwargs)
            run_kwargs.pop("load_env", None)
            run_kwargs.pop("enable_conversation_history", None)
            return run_assistant_request(
                request,
                runtime=runtime,
                conversation_store=conversation_store,
                realtime_task_state_store=task_state_store,
                enable_conversation_history=False,
                load_env=False,
                **run_kwargs,
            )

        session = GatewaySessionService(
            backend=GatewayAgentAdapter(run_request=run_with_runtime, load_env=False),
            config={"system_prompt_profile": "realtime_phone", "channel": "realtime_phone"},
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="phone-native-session",
                    user_id="phone-native-user",
                    payload={"text": "喂，帮我查一下订单", "turn_id": "turn-phone"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "completed"
        assert adapter.requests
        chat_request = adapter.requests[0]
        system_message = chat_request.messages[0]
        assert system_message["role"] == "system"
        assert system_message["content"] == render_system_instruction(
            SystemPromptProfile.REALTIME_PHONE
        )
        assert "实时电话助手" in system_message["content"]
        assert "自然口语" in system_message["content"]
        assert "用户说话或打断时，优先听新输入" in system_message["content"]
        assert "工具慢时给进度话术" in system_message["content"]
        assert "Display / spoken boundary" in system_message["content"]
        assert "电话里只说摘要" in system_message["content"]
        assert chat_request.tools
        assert chat_request.tool_choice == "auto"

    async def test_gateway_cancel_reaches_gateway_agent_adapter_runtime(self) -> None:
        cancel_seen = Event()
        captured: dict[str, object] = {}

        def fake_run_assistant_request(request: UserRequest, **kwargs) -> SimpleNamespace:
            token = kwargs["cancel_token"]
            captured["cancel_token"] = token
            deadline = time.monotonic() + 2.0
            while not token.is_cancelled() and time.monotonic() < deadline:
                time.sleep(0.005)
            if token.is_cancelled():
                cancel_seen.set()
            state = AgentState.from_request(request, run_id="main-runtime-cancel-run")
            state.cancel(details={**token.cancel_metadata, "cancel_phase": "runtime_wait"})
            return SimpleNamespace(state=state)

        session = GatewaySessionService(
            backend=GatewayAgentAdapter(run_request=fake_run_assistant_request)
        )
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames = []

        async def _read_cancel_flow():
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="adapter-cancel-session",
                            run_id=received["run_id"],
                        )
                    )
                if received["type"] == "run.end":
                    return frames
            raise AssertionError("endpoint closed before run.end")

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="adapter-cancel-session",
                    payload={"text": "cancel adapter", "run_id": "gateway-run-cancel"},
                )
            )
            frames = await asyncio.wait_for(_read_cancel_flow(), timeout=3.0)
            await asyncio.wait_for(asyncio.to_thread(cancel_seen.wait, 2.0), timeout=3.0)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "cancelled"
        assert cancel_seen.is_set()
        assert captured["cancel_token"].cancel_metadata["cancel_source"] == "gateway_cancel"

    async def test_cancel_preserves_cancelled_run_end(self) -> None:
        class CancellableBackend:
            def __init__(self) -> None:
                self.cancel_seen = asyncio.Event()
                self.release = asyncio.Event()
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                while not cancel_token.is_cancelled():
                    await asyncio.sleep(0.01)
                self.cancel_seen.set()
                self.cancel_metadata = cancel_token.cancel_metadata
                await self.release.wait()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        backend = CancellableBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames = []

        async def _read_cancel_flow():
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="cancel-session",
                            run_id=received["run_id"],
                        )
                    )
                    await asyncio.wait_for(backend.cancel_seen.wait(), timeout=2.0)
                    backend.release.set()
                if received["type"] == "run.end":
                    return frames
            raise AssertionError("endpoint closed before run.end")

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="cancel-session",
                    payload={"text": "cancel realtime", "turn_id": "turn-cancel"},
                )
            )
            frames = await asyncio.wait_for(_read_cancel_flow(), timeout=3.0)
        finally:
            backend.release.set()
            await _close_session(client_ep, session_ep, session_task)

        assert frames[0]["type"] == "run.started"
        assert frames[-1]["type"] == "run.end"
        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["expects_reply"] is True
        assert frames[-1]["payload"]["supersedes"] == [
            f"{frames[-1]['run_id']}:progress"
        ]
        assert len(backend.requests) == 1
        assert backend.requests[0].text == "cancel realtime"
        assert backend.cancel_metadata["cancel_source"] == "gateway_cancel"
        assert backend.cancel_metadata["realtime_turn_cancellation"]["cancelled_by"] == "run.cancel"
        _assert_gateway_cancel_payload(
            frames[-1]["payload"],
            cancelled_by="run.cancel",
            phase="final_streaming",
        )

    async def test_cancel_suppresses_backend_events_emitted_after_cancel(self) -> None:
        class StaleEventBackend:
            def __init__(self) -> None:
                self.finished = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                assert event_sink is not None
                for event in [
                    RealtimeAgentEvent(type="response.chunk", text="stale chunk"),
                    RealtimeAgentEvent(type="response.final", text="stale final"),
                    RealtimeAgentEvent(
                        type="tool.started",
                        text="stale tool",
                        payload={"tool_name": "stale_tool"},
                    ),
                    RealtimeAgentEvent(
                        type="trace.decision",
                        text="stale trace",
                        payload={"decision_trace": {"action": "stale"}},
                    ),
                    RealtimeAgentEvent(type="error", text="stale error"),
                ]:
                    await event_sink(event)
                self.finished.set()
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    response_text="stale final",
                    expects_reply=False,
                )

        backend = StaleEventBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames = []

        async def _read_cancel_flow():
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="stale-cancel-session",
                            run_id=received["run_id"],
                        )
                    )
                if received["type"] == "run.end":
                    return frames
            raise AssertionError("endpoint closed before run.end")

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="stale-cancel-session",
                    payload={"text": "cancel stale events"},
                )
            )
            frames = await asyncio.wait_for(_read_cancel_flow(), timeout=3.0)
            await asyncio.wait_for(backend.finished.wait(), timeout=2.0)
            await _assert_no_frame(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == ["run.started", "run.end"]
        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["expects_reply"] is True

    async def test_interrupt_cancels_previous_run_then_starts_new(self) -> None:
        class InterruptBackend:
            def __init__(self) -> None:
                self.first_cancel_metadata = None
                self.first_cancel_seen = asyncio.Event()
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                if request.text == "first":
                    while not cancel_token.is_cancelled():
                        await asyncio.sleep(0.01)
                    self.first_cancel_metadata = cancel_token.cancel_metadata
                    self.first_cancel_seen.set()
                    return RealtimeAgentResult(status="cancelled", run_id=request.run_id)
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="second done"))
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        backend = InterruptBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        first_run = None
        second_run = None
        saw_first_cancelled = False
        saw_second_completed = False

        try:
            await client_ep.send(
                frame(type="message.user", session_id="interrupt-session", payload={"text": "first"})
            )

            async def _read_until_both_runs_end() -> None:
                nonlocal first_run, second_run, saw_first_cancelled, saw_second_completed
                async for received in client_ep:
                    if received["type"] == "run.started" and first_run is None:
                        first_run = received["run_id"]
                        await client_ep.send(
                            frame(
                                type="message.user",
                                session_id="interrupt-session",
                                payload={"text": "second", "interrupt": True},
                            )
                        )
                    elif received["type"] == "run.end" and received.get("run_id") == first_run:
                        saw_first_cancelled = received.get("reason") == "cancelled"
                    elif received["type"] == "run.started" and first_run is not None:
                        second_run = received["run_id"]
                    elif received["type"] == "run.end" and second_run is not None:
                        assert received["run_id"] == second_run
                        saw_second_completed = received.get("reason") == "completed"
                    if saw_first_cancelled and saw_second_completed:
                        return

            await asyncio.wait_for(_read_until_both_runs_end(), timeout=3.0)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert first_run is not None
        assert second_run is not None
        assert first_run != second_run
        assert saw_first_cancelled is True
        assert saw_second_completed is True
        await asyncio.wait_for(backend.first_cancel_seen.wait(), timeout=2.0)
        assert backend.first_cancel_metadata["cancel_source"] == "gateway_interrupt"
        assert [request.text for request in backend.requests] == ["first", "second"]
        assert backend.requests[1].metadata["control"] == "interrupt"
        assert backend.requests[1].metadata["gateway"]["interrupt"] is True

    async def test_message_user_queues_behind_active_run_without_interrupt(self) -> None:
        class QueueBackend:
            def __init__(self) -> None:
                self.release_first = asyncio.Event()
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                assert event_sink is not None
                if request.text == "first":
                    await self.release_first.wait()
                    return RealtimeAgentResult(status="completed", run_id=request.run_id)
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="second done"))
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        backend = QueueBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames = []
        first_run = None
        second_run = None

        try:
            await client_ep.send(
                frame(type="message.user", session_id="queue-session", payload={"text": "first"})
            )

            async def _read_until_second_run_end() -> None:
                nonlocal first_run, second_run
                async for received in client_ep:
                    frames.append(received)
                    if received["type"] == "run.started" and first_run is None:
                        first_run = received["run_id"]
                        await client_ep.send(
                            frame(
                                type="message.user",
                                session_id="queue-session",
                                payload={"text": "second"},
                            )
                        )
                        backend.release_first.set()
                    elif received["type"] == "run.started":
                        second_run = received["run_id"]
                    elif received["type"] == "run.end" and second_run is not None:
                        return

            await asyncio.wait_for(_read_until_second_run_end(), timeout=3.0)
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == [
            "run.started",
            "run.end",
            "run.started",
            "stream.chunk",
            "run.end",
        ]
        assert frames[1]["run_id"] == first_run
        assert frames[1]["reason"] == "completed"
        assert frames[-1]["run_id"] == second_run
        assert frames[-1]["reason"] == "completed"
        assert first_run != second_run
        assert [request.text for request in backend.requests] == ["first", "second"]
        assert backend.requests[1].metadata["runtime"]["history"] == ["first", "second"]
        assert "control" not in backend.requests[1].metadata
        assert "interrupt" not in backend.requests[1].metadata["gateway"]

    async def test_lifecycle_sink_records_queued_and_terminal_run_events(self) -> None:
        class QueueBackend:
            def __init__(self) -> None:
                self.release_first = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                if request.text == "first":
                    await self.release_first.wait()
                    return RealtimeAgentResult(status="completed", run_id=request.run_id)
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="second done"))
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        events = []
        backend = QueueBackend()
        session = GatewaySessionService(backend=backend, lifecycle_sink=events.append)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(type="message.user", session_id="lifecycle-queue", payload={"text": "first"})
            )

            async def _read_until_second_run_end() -> None:
                saw_first_run = False
                saw_second_run = False
                async for received in client_ep:
                    if received["type"] == "run.started" and not saw_first_run:
                        saw_first_run = True
                        await client_ep.send(
                            frame(
                                type="message.user",
                                session_id="lifecycle-queue",
                                payload={"text": "second"},
                            )
                        )
                        backend.release_first.set()
                    elif received["type"] == "run.started":
                        saw_second_run = True
                    elif received["type"] == "run.end" and saw_second_run:
                        return

            await asyncio.wait_for(_read_until_second_run_end(), timeout=3.0)
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        event_types = [event.type for event in events]
        assert event_types == [
            "gateway.run.started",
            "gateway.run.queued",
            "gateway.run.completed",
            "gateway.run.started",
            "gateway.run.completed",
        ]
        queued = events[1]
        assert queued.session_id == "lifecycle-queue"
        assert queued.payload["queue_depth"] == 1

    async def test_lifecycle_sink_records_cancel_request_and_cancelled_run_end(self) -> None:
        class CancellableBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        events = []
        session = GatewaySessionService(backend=CancellableBackend(), lifecycle_sink=events.append)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        async def _read_cancel_flow() -> None:
            async for received in client_ep:
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="lifecycle-cancel",
                            run_id=received["run_id"],
                        )
                    )
                if received["type"] == "run.end":
                    return
            raise AssertionError("endpoint closed before run.end")

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="lifecycle-cancel",
                    payload={"text": "cancel me"},
                )
            )
            await asyncio.wait_for(_read_cancel_flow(), timeout=3.0)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        event_types = [event.type for event in events]
        assert event_types == [
            "gateway.run.started",
            "gateway.run.cancel_requested",
            "gateway.run.cancelled",
        ]
        cancel_requested = events[1]
        assert cancel_requested.session_id == "lifecycle-cancel"
        assert cancel_requested.payload["source"] == "gateway_cancel"

    async def test_interrupt_suppresses_previous_run_events_after_new_message(self) -> None:
        class InterruptStaleEventBackend:
            def __init__(self) -> None:
                self.first_finished = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                assert event_sink is not None
                if request.text == "first":
                    await cancel_token.cancelled()
                    await event_sink(RealtimeAgentEvent(type="response.chunk", text="first stale"))
                    await event_sink(
                        RealtimeAgentEvent(
                            type="tool.finished",
                            text="first tool stale",
                            payload={"tool_name": "first_tool"},
                        )
                    )
                    self.first_finished.set()
                    return RealtimeAgentResult(
                        status="completed",
                        run_id=request.run_id,
                        response_text="first stale final",
                    )
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="second done"))
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    response_text="second done",
                    expects_reply=True,
                )

        backend = InterruptStaleEventBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        first_run = None
        second_run = None
        ended: dict[str, str] = {}
        chunks_by_run: dict[str, list[str]] = {}

        try:
            await client_ep.send(
                frame(type="message.user", session_id="interrupt-stale-session", payload={"text": "first"})
            )

            async def _read_until_both_runs_end() -> None:
                nonlocal first_run, second_run
                async for received in client_ep:
                    if received["type"] == "run.started" and first_run is None:
                        first_run = received["run_id"]
                        await client_ep.send(
                            frame(
                                type="message.user",
                                session_id="interrupt-stale-session",
                                payload={"text": "second", "interrupt": True},
                            )
                        )
                    elif received["type"] == "run.started":
                        second_run = received["run_id"]
                    elif received["type"] == "stream.chunk":
                        chunks_by_run.setdefault(received["run_id"], []).append(
                            received["payload"]["text"]
                        )
                    elif received["type"] == "run.end":
                        ended[received["run_id"]] = received["reason"]
                    if first_run is not None and second_run is not None:
                        if ended.get(first_run) == "cancelled" and ended.get(second_run) == "completed":
                            return

            await asyncio.wait_for(_read_until_both_runs_end(), timeout=3.0)
            await asyncio.wait_for(backend.first_finished.wait(), timeout=2.0)
            await _assert_no_frame(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert first_run is not None
        assert second_run is not None
        assert first_run != second_run
        assert chunks_by_run.get(first_run, []) == []
        assert chunks_by_run.get(second_run) == ["second done"]

    async def test_run_deadline_from_session_config_cancels_backend(self) -> None:
        class DeadlineBackend:
            def __init__(self) -> None:
                self.cancel_seen = asyncio.Event()
                self.cancel_metadata = None
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                await cancel_token.cancelled()
                self.cancel_metadata = cancel_token.cancel_metadata
                self.cancel_seen.set()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        backend = DeadlineBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 30})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-session",
                    payload={"text": "deadline please"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["expects_reply"] is True
        await asyncio.wait_for(backend.cancel_seen.wait(), timeout=2.0)
        assert backend.cancel_seen.is_set()
        assert backend.cancel_metadata["deadline_ms"] == 30
        assert backend.cancel_metadata["cancel_source"] == "deadline"
        assert backend.cancel_metadata["cancel_reason"] == "run_deadline_expired"
        assert backend.cancel_metadata["realtime_turn_cancellation"]["cancelled_by"] == "deadline"
        _assert_gateway_cancel_payload(
            frames[-1]["payload"],
            cancelled_by="deadline",
            phase="final_streaming",
        )
        assert backend.requests[0].metadata["runtime"]["session_config"]["run_timeout_ms"] == 30

    async def test_run_deadline_from_message_metadata_overrides_session_config(self) -> None:
        class DeadlineBackend:
            def __init__(self) -> None:
                self.cancel_metadata = None
                self.cancel_seen = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                self.cancel_metadata = cancel_token.cancel_metadata
                self.cancel_seen.set()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        backend = DeadlineBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 5000})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-override-session",
                    payload={
                        "text": "deadline override",
                        "metadata": {"gateway": {"run_timeout_ms": 25}},
                    },
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "cancelled"
        await asyncio.wait_for(backend.cancel_seen.wait(), timeout=2.0)
        assert backend.cancel_metadata["cancel_source"] == "deadline"
        assert backend.cancel_metadata["deadline_ms"] == 25

    async def test_deadline_suppresses_backend_events_emitted_after_timeout(self) -> None:
        class DeadlineStaleEventBackend:
            def __init__(self) -> None:
                self.finished = asyncio.Event()
                self.cancel_metadata = None

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                self.cancel_metadata = cancel_token.cancel_metadata
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="deadline stale"))
                await event_sink(RealtimeAgentEvent(type="error", text="deadline stale error"))
                self.finished.set()
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        backend = DeadlineStaleEventBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 20})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-stale-session",
                    payload={"text": "deadline stale events"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
            await asyncio.wait_for(backend.finished.wait(), timeout=2.0)
            await _assert_no_frame(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == ["run.started", "run.end"]
        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["expects_reply"] is True
        assert backend.cancel_metadata["deadline_ms"] == 20
        assert backend.cancel_metadata["cancel_source"] == "deadline"
        assert backend.cancel_metadata["cancel_reason"] == "run_deadline_expired"
        assert backend.cancel_metadata["realtime_turn_cancellation"]["cancelled_by"] == "deadline"
        _assert_gateway_cancel_payload(
            frames[-1]["payload"],
            cancelled_by="deadline",
            phase="final_streaming",
        )

    async def test_completed_run_cleans_deadline_monitor(self) -> None:
        class FastBackend:
            def __init__(self) -> None:
                self.cancel_token = None

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.cancel_token = cancel_token
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        backend = FastBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 50})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-cleanup-session",
                    payload={"text": "fast"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
            await asyncio.sleep(0.08)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "completed"
        assert backend.cancel_token is not None
        assert backend.cancel_token.is_cancelled() is False

    async def test_message_timeout_zero_disables_session_config_deadline(self) -> None:
        class SlowCompletedBackend:
            def __init__(self) -> None:
                self.cancel_token = None

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.cancel_token = cancel_token
                await asyncio.sleep(0.04)
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        backend = SlowCompletedBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 10})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-disabled-session",
                    payload={
                        "text": "disable deadline",
                        "metadata": {"gateway": {"run_timeout_ms": 0}},
                    },
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "completed"
        assert backend.cancel_token is not None
        assert backend.cancel_token.is_cancelled() is False

    async def test_multiturn_history_is_passed_to_backend_metadata(self) -> None:
        class HistoryBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                assert event_sink is not None
                history = request.metadata["runtime"]["history"]
                await event_sink(
                    RealtimeAgentEvent(
                        type="response.chunk",
                        text=f"echo:{request.text};history:{'|'.join(history)}",
                    )
                )
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        session = GatewaySessionService(backend=HistoryBackend())
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        async def one_turn(text: str) -> str:
            await client_ep.send(frame(type="message.user", session_id="history-session", payload={"text": text}))
            chunks: list[str] = []
            async for received in client_ep:
                if received["type"] == "stream.chunk":
                    chunks.append(received["payload"]["text"])
                if received["type"] == "run.end":
                    break
            return "".join(chunks)

        try:
            out1 = await one_turn("one")
            out2 = await one_turn("two")
            out3 = await one_turn("three")
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert "history:one" in out1
        assert "history:one|two" in out2
        assert "history:one|two|three" in out3


def test_websocket_frame_json_roundtrip() -> None:
    source = frame(type="stream.chunk", session_id="s1", payload={"text": "hello"})

    assert loads_frame(dumps_frame(source)) == source


if __name__ == "__main__":
    unittest.main()
