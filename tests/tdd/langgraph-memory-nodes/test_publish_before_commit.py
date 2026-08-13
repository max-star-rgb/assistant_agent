from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.gateway.runtime_adapter import GatewayRuntimeAdapter
from assistant_agent.gateway.runtime_types import RealtimeAgentRequest
from assistant_agent.memory.backends.disabled import build_disabled_memory_bundle
from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
from assistant_agent.runtime.assistant_graph_state import (
    MemoryCommitState,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.event_stream import AgentRunStream
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from tests.core.support import ScriptedChatAdapter, offline_config, sealed_registry


def test_final_response_is_in_product_stream_while_commit_is_blocked() -> None:
    commit_started = Event()
    release_commit = Event()

    def blocking_commit(state, runtime):
        del runtime
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise TimeoutError("test did not release memory commit")
        updated = dict(validate_assistant_turn_state(state))
        updated["memory_commit"] = MemoryCommitState(
            status="succeeded",
            memory_event_id="memory-event-1",
        ).model_dump(mode="json")
        return validate_assistant_turn_state(updated)

    bundle = replace(
        build_disabled_memory_bundle(),
        backend_id="blocking-probe",
        commit_node=blocking_commit,
    )
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="answer-before-commit",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    runtime.assistant_graph_app = AssistantTurnGraphApp(
        checkpointer=InMemorySaver(),
        memory_bundle=bundle,
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            runtime.arun_state(
                UserRequest(user_id="user-1", session_id="session-1", text="hello"),
                event_sink=sink,
                run_id="run-1",
            )
        )
        try:
            assert await asyncio.to_thread(commit_started.wait, 2)
            finals_before_release = [
                event for event in sink.events if event.type == "final_response"
            ]
            assert [event.text for event in finals_before_release] == [
                "answer-before-commit"
            ]
        finally:
            release_commit.set()
        state = await task
        assert state.status == "completed"
        assert [event.type for event in sink.events].count("final_response") == 1

    try:
        asyncio.run(exercise())
    finally:
        release_commit.set()
        runtime.close()


def test_gateway_forwards_graph_final_once_without_terminal_resynthesis() -> None:
    state = AgentState.from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="hello"),
        run_id="run-1",
        trace_id="trace-1",
    )
    state.set_response(AgentResponse(message="one-final"))

    async def exercise() -> None:
        release_result = asyncio.Event()
        emitted = []

        def stream_with_graph_final(*args, **kwargs):
            del args, kwargs
            stream = AgentRunStream(loop=asyncio.get_running_loop())
            stream.emit(
                AgentEvent(
                    type="final_response",
                    session_id="session-1",
                    run_id="run-1",
                    text="one-final",
                )
            )

            async def finish_after_release() -> None:
                await release_result.wait()
                stream.set_result(SimpleNamespace(state=state, events=[]))

            asyncio.create_task(finish_after_release())
            return stream

        async def collect(event):
            emitted.append(event)

        task = asyncio.create_task(
            GatewayRuntimeAdapter(
                run_request_stream=stream_with_graph_final,
                load_env=False,
                enable_conversation_history=False,
            ).run_turn(
                RealtimeAgentRequest(
                    user_id="user-1",
                    session_id="session-1",
                    run_id="run-1",
                    text="hello",
                ),
                event_sink=collect,
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            assert [event.type for event in emitted] == [
                "response.chunk",
                "response.final",
            ]
        finally:
            release_result.set()
        result = await task

        assert result.status == "completed"
        assert [event.type for event in emitted] == [
            "response.chunk",
            "response.final",
        ]
        assert [event.text for event in emitted] == ["one-final", "one-final"]

    asyncio.run(exercise())
