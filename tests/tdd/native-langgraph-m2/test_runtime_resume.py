from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.runtime.assistant_graph_app import GraphExecutionError
from assistant_agent.runtime.assistant_interrupts import (
    AssistantApproveResume,
    AssistantInterruptRequest,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import (
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


class _LifecycleProbe(ProbeTool):
    name = "runtime_resume_probe"

    def __init__(self) -> None:
        self.terminals: list[tuple[str, str]] = []

    def on_run_terminal(self, run_id: str, status: str) -> None:
        self.terminals.append((run_id, status))


def _request() -> UserRequest:
    return UserRequest(
        user_id="user-runtime-resume",
        session_id="session-runtime-resume",
        text="Run the guarded probe.",
    )


def _runtime(
    saver: InMemorySaver,
    tool: _LifecycleProbe,
    responses: list[ChatResult],
    *,
    allow_interrupt: bool,
) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(responses),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        allow_interrupt=allow_interrupt,
    )


def test_internal_runtime_waits_then_rebuilt_resume_commits_one_terminal() -> None:
    """Finalizing the waiting invoke or bypassing native Command(resume) must fail."""

    saver = InMemorySaver()
    tool = _LifecycleProbe()
    waiting_sink = ListEventSink()
    first = _runtime(
        saver,
        tool,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-runtime-resume",
                        name=tool.name,
                        arguments={"value": "approved-value"},
                    )
                ],
            )
        ],
        allow_interrupt=True,
    )
    try:
        waiting = asyncio.run(
            first.arun_state(
                _request(),
                event_sink=waiting_sink,
                run_id="run-before-runtime-resume",
                interrupt_request=AssistantInterruptRequest(
                    kind="approval",
                    prompt="Approve the guarded probe?",
                    action_ref="provider-runtime-resume",
                    allowed_resume_kinds=("approve", "reject"),
                ),
            )
        )

        assert waiting.status == "waiting_user"
        assert waiting.response is None
        assert [event.type for event in waiting_sink.events] == ["task_started"]
        assert tool.terminals == []
    finally:
        first.close()

    terminal_sink = ListEventSink()
    rebuilt = _runtime(
        saver,
        tool,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="approved-final",
            )
        ],
        allow_interrupt=True,
    )
    try:
        terminal = asyncio.run(
            rebuilt.aresume_state(
                _request(),
                resume=AssistantApproveResume(
                    action_ref="provider-runtime-resume",
                ),
                event_sink=terminal_sink,
                run_id="run-after-runtime-resume",
            )
        )

        assert terminal.status == "completed"
        assert terminal.run_id == "run-after-runtime-resume"
        assert terminal.response is not None
        assert terminal.response.message == "approved-final"
        assert terminal.trace_id == waiting.trace_id
        assert {
            event.payload.get("trace_id")
            for event in terminal_sink.events
            if event.payload.get("trace_id") is not None
        } == {waiting.trace_id}
        assert [event.type for event in terminal_sink.events].count("final_response") == 1
        assert tool.terminals == [("run-after-runtime-resume", "completed")]
    finally:
        rebuilt.close()


def test_default_runtime_rejects_interrupt_resume_and_internal_state_stream() -> None:
    """Allowing product composition roots to expose waiting/resume must fail."""

    saver = InMemorySaver()
    tool = _LifecycleProbe()
    runtime = _runtime(
        saver,
        tool,
        [],
        allow_interrupt=False,
    )

    async def exercise() -> None:
        request = _request()
        approval = AssistantInterruptRequest(
            kind="approval",
            prompt="Approve?",
            action_ref="provider-runtime-disabled",
            allowed_resume_kinds=("approve", "reject"),
        )
        resume = AssistantApproveResume(action_ref="provider-runtime-disabled")

        with pytest.raises(GraphExecutionError) as interrupt_error:
            await runtime.arun_state(
                request,
                interrupt_request=approval,
                run_id="run-disabled-interrupt",
            )
        with pytest.raises(GraphExecutionError) as resume_error:
            await runtime.aresume_state(
                request,
                resume=resume,
                run_id="run-disabled-resume",
            )
        with pytest.raises(GraphExecutionError) as stream_error:
            runtime.astream_state(
                request,
                run_id="run-disabled-stream",
            )

        assert interrupt_error.value.code == "graph_interrupt_disabled"
        assert resume_error.value.code == "graph_interrupt_disabled"
        assert stream_error.value.code == "graph_interrupt_disabled"

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()


def test_interrupt_flag_rejects_truthy_non_boolean_values() -> None:
    """Treating a string such as ``false`` as enabled would break fail-closed setup."""

    with pytest.raises(TypeError, match="allow_interrupt must be a boolean"):
        AgentGraphRuntime(
            registry=sealed_registry(),
            config=offline_config(),
            session_store=InMemorySessionStore(),
            allow_interrupt="false",  # type: ignore[arg-type]
        )


def test_internal_state_stream_uses_async_graph_for_wait_and_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring a worker-thread graph bridge must fail the resumable stream path."""

    async def forbidden_to_thread(*args, **kwargs):
        raise AssertionError("assistant graph execution must remain async-native")

    monkeypatch.setattr(asyncio, "to_thread", forbidden_to_thread)
    saver = InMemorySaver()
    tool = _LifecycleProbe()
    first = _runtime(
        saver,
        tool,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-stream-resume",
                        name=tool.name,
                        arguments={"value": "stream-value"},
                    )
                ],
            )
        ],
        allow_interrupt=True,
    )

    async def interrupt_then_resume() -> None:
        waiting_stream = first.astream_state(
            _request(),
            interrupt_request=AssistantInterruptRequest(
                kind="approval",
                prompt="Approve the stream probe?",
                action_ref="provider-stream-resume",
                allowed_resume_kinds=("approve", "reject"),
            ),
            run_id="run-before-stream-resume",
        )
        waiting_events = [event async for event in waiting_stream]
        waiting = await waiting_stream.result()
        assert waiting.status == "waiting_user"
        assert [event.type for event in waiting_events] == ["task_started"]

        rebuilt = _runtime(
            saver,
            tool,
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="stream-final",
                )
            ],
            allow_interrupt=True,
        )
        try:
            resumed_stream = rebuilt.astream_state(
                _request(),
                resume=AssistantApproveResume(action_ref="provider-stream-resume"),
                run_id="run-after-stream-resume",
            )
            resumed_events = [event async for event in resumed_stream]
            resumed = await resumed_stream.result()

            assert resumed.status == "completed"
            assert resumed.response is not None
            assert resumed.response.message == "stream-final"
            assert [event.type for event in resumed_events].count("final_response") == 1
        finally:
            rebuilt.close()

    try:
        asyncio.run(interrupt_then_resume())
    finally:
        first.close()
