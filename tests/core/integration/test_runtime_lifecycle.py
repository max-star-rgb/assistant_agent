from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.gateway.runtime_event_mapping import map_agent_event_stream
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.assistant_graph_app import GraphExecutionIdentity
from assistant_agent.runtime.assistant_graph_profiles import (
    ASSISTANT_GRAPH_PROFILES,
    ProfileInvocationInput,
    profile_input_adapter,
)
from assistant_agent.runtime.assistant_graph_state import (
    ASSISTANT_GRAPH_NAME,
    ASSISTANT_GRAPH_VERSION,
    ASSISTANT_STATE_SCHEMA_VERSION,
)
from assistant_agent.runtime.assistant_interrupts import (
    AssistantApproveResume,
    AssistantInterruptRequest,
)
from assistant_agent.runtime.chat_adapter import ChatProviderError, ChatResult
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.models import ToolResult
from tests.core.support import (
    CancelledToken,
    ProbeInput,
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


class _RunLifecycleProbeTool(ProbeTool):
    name = "run_lifecycle_probe_tool"

    def __init__(self) -> None:
        self.terminals: list[tuple[str, str]] = []

    def on_run_terminal(self, run_id: str, status: str) -> None:
        self.terminals.append((run_id, status))


class _NonrecoverableProbeTool(ProbeTool):
    name = "nonrecoverable_probe_tool"

    def __init__(self) -> None:
        self.executed_values: list[str] = []

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        self.executed_values.append(input.value)
        return ToolResult(
            tool_name=self.name,
            success=False,
            error="nonrecoverable-sentinel",
            contract=build_capability_output_contract(
                capability=self.name,
                status="failed",
                errors=[
                    {
                        "code": "provider_auth_failed",
                        "message": "nonrecoverable-sentinel",
                        "recoverable": False,
                    }
                ],
            ),
        )


class _IndependentProbeTool(ProbeTool):
    name = "independent_probe_tool"

    def __init__(self) -> None:
        self.executed_values: list[str] = []

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        self.executed_values.append(input.value)
        return super()._run(input, context)


@pytest.fixture(autouse=True)
def default_registry_assembly_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_default_registry(*args, **kwargs):
        raise AssertionError("default-registry-called")

    monkeypatch.setattr(
        "assistant_agent.runtime.runtime.create_default_registry",
        reject_default_registry,
    )


@pytest.mark.core_invariant("BOOT-001")
def test_runtime_initializes_offline() -> None:
    package = importlib.import_module("assistant_agent")
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
    )
    try:
        assert package is not None
        assert runtime.config.provider_mode == "mock"
        assert runtime.chat_adapter.provider == "mock"
        assert runtime.registry.sealed is True
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("OBS-001")
def test_plain_text_run_reaches_completed_terminal_state() -> None:
    sink = ListEventSink()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="final-sentinel",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            event_sink=sink,
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.message
        assert sink.events[0].type == "task_started"
        assert sink.events[-1].type == "final_response"
        assert not {
            "graph_node_started",
            "graph_node_finished",
        }.intersection(event.type for event in sink.events)
        trace_events = runtime.trace_store.list_by_run(state.run_id)
        run_started = next(
            event for event in trace_events if event.canonical_event == "run.started"
        )
        run_completed = next(
            event
            for event in trace_events
            if event.canonical_event == "run.completed"
        )
        response_delivered = next(
            event
            for event in trace_events
            if event.canonical_event == "response.delivered"
        )
        assert sink.events[0].created_at == run_started.created_at
        assert sink.events[-1].created_at == response_delivered.created_at
        assert response_delivered.created_at <= run_completed.created_at
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
def test_native_async_run_reaches_the_same_completed_terminal_contract() -> None:
    def create_runtime() -> AgentGraphRuntime:
        return AgentGraphRuntime(
            registry=sealed_registry(),
            config=offline_config(),
            chat_adapter=ScriptedChatAdapter(
                [
                    ChatResult(
                        provider="scripted",
                        model="scripted-model",
                        finish_reason="stop",
                        response_text="final-sentinel",
                    )
                ]
            ),
            session_store=InMemorySessionStore(),
        )

    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="input-sentinel",
    )
    sync_sink = ListEventSink()
    async_sink = ListEventSink()
    sync_runtime = create_runtime()
    async_runtime = create_runtime()
    try:
        sync_state = sync_runtime.run_state(request, event_sink=sync_sink)
        async_state = asyncio.run(
            async_runtime.arun_state(
                request,
                event_sink=async_sink,
            )
        )

        assert sync_state.status == async_state.status == "completed"
        assert sync_state.response is not None
        assert async_state.response is not None
        assert sync_state.response.message == async_state.response.message
        assert [sync_sink.events[0].type, sync_sink.events[-1].type] == [
            async_sink.events[0].type,
            async_sink.events[-1].type,
        ] == ["task_started", "final_response"]
    finally:
        sync_runtime.close()
        async_runtime.close()


@pytest.mark.core_invariant("LOOP-001")
def test_runtime_reuses_one_compiled_assistant_graph_across_turns() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="first-sentinel",
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="second-sentinel",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
    )
    try:
        compiled_graph = runtime.assistant_graph_app.graph

        first = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="first-input-sentinel",
            )
        )
        second = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="second-input-sentinel",
            )
        )

        assert first.status == second.status == "completed"
        assert runtime.assistant_graph_app.graph is compiled_graph
    finally:
        runtime.close()


@pytest.mark.core_invariant("LOOP-001")
def test_runtime_exposes_versioned_profile_graph_family() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    try:
        profile_graphs = {
            name: runtime.assistant_graph_app.graph_for_profile(name)
            for name in ASSISTANT_GRAPH_PROFILES
        }
        parent_state = {
            "user_id": "user-profile-sentinel",
            "session_id": "session-profile-sentinel",
            "run_id": "run-profile-sentinel",
            "trace_id": "trace-profile-sentinel",
            "agent_id": "agent-profile-sentinel",
            "available_tool_names": (ProbeTool.name,),
            "registered_tool_specs": tuple(runtime.registry.list_specs()),
        }
        worker_state = profile_input_adapter(
            parent_state,
            ProfileInvocationInput(
                profile="worker",
                assignment_ref="assignment:profile-sentinel",
                objective="objective-sentinel",
                explicit_tool_allowlist=(ProbeTool.name,),
            ),
        )

        assert tuple(profile_graphs) == (
            "standard",
            "planner",
            "worker",
            "verifier",
        )
        assert {
            name: graph.name for name, graph in profile_graphs.items()
        } == {
            name: f"AssistantTurnGraph.{name}" for name in profile_graphs
        }
        assert runtime.assistant_graph_app.graph_for_profile("worker") is (
            profile_graphs["worker"]
        )
        assert worker_state["graph_name"] == ASSISTANT_GRAPH_NAME
        assert worker_state["graph_version"] == ASSISTANT_GRAPH_VERSION
        assert worker_state["state_schema_version"] == ASSISTANT_STATE_SCHEMA_VERSION
        assert worker_state["profile"] == "worker"
        assert worker_state["catalog"]["available_tool_names"] == [ProbeTool.name]
        json.dumps(worker_state)
    finally:
        runtime.close()


@pytest.mark.core_invariant("IDENT-001")
def test_entry_run_and_agent_identity_are_preserved() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        agent_id="agent-sentinel",
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="final-sentinel",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            run_id="run-sentinel",
        )

        assert state.run_id == "run-sentinel"
        assert state.agent_id == "agent-sentinel"
        assert {
            event.run_id
            for event in runtime.trace_store.list_by_run("run-sentinel")
        } == {"run-sentinel"}
    finally:
        runtime.close()


@pytest.mark.core_invariant("IDENT-001")
def test_graph_thread_identity_is_stable_for_one_conversation() -> None:
    first = GraphExecutionIdentity.for_assistant_turn(
        agent_id="agent-sentinel",
        user_id="user-sentinel",
        session_id="session-sentinel",
        run_id="run-one-sentinel",
    )
    second = GraphExecutionIdentity.for_assistant_turn(
        agent_id="agent-sentinel",
        user_id="user-sentinel",
        session_id="session-sentinel",
        run_id="run-two-sentinel",
    )
    other_user = GraphExecutionIdentity.for_assistant_turn(
        agent_id="agent-sentinel",
        user_id="other-user-sentinel",
        session_id="session-sentinel",
        run_id="run-three-sentinel",
    )

    assert first.thread_id == second.thread_id
    assert first.thread_id not in {first.run_id, second.run_id}
    assert other_user.thread_id != first.thread_id
    assert first.runnable_config() == {
        "configurable": {
            "thread_id": first.thread_id,
            "run_id": "run-one-sentinel",
        }
    }


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("LOOP-001")
@pytest.mark.core_invariant("IDENT-001")
def test_interrupted_run_resumes_on_stable_thread_to_one_terminal() -> None:
    request = UserRequest(
        user_id="user-resume-sentinel",
        session_id="session-resume-sentinel",
        text="input-resume-sentinel",
    )
    baseline_tool = _RunLifecycleProbeTool()
    baseline_runtime = AgentGraphRuntime(
        registry=sealed_registry(baseline_tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="provider-resume-sentinel",
                            name=baseline_tool.name,
                            arguments={"value": "value-resume-sentinel"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="final-resume-sentinel",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    try:
        baseline = asyncio.run(
            baseline_runtime.arun_state(
                request.model_copy(deep=True),
                run_id="run-uninterrupted-sentinel",
            )
        )
    finally:
        baseline_runtime.close()

    saver = InMemorySaver()
    tool = _RunLifecycleProbeTool()
    waiting_sink = ListEventSink()
    waiting_runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="provider-resume-sentinel",
                            name=tool.name,
                            arguments={"value": "value-resume-sentinel"},
                        )
                    ],
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        allow_interrupt=True,
    )
    waiting_identity = GraphExecutionIdentity.for_assistant_turn(
        agent_id=waiting_runtime.agent_id,
        user_id=request.user_id,
        session_id=request.session_id,
        run_id="run-before-resume-sentinel",
    )
    try:
        waiting = asyncio.run(
            waiting_runtime.arun_state(
                request,
                event_sink=waiting_sink,
                run_id=waiting_identity.run_id,
                interrupt_request=AssistantInterruptRequest(
                    kind="approval",
                    prompt="approval-prompt-sentinel",
                    action_ref="provider-resume-sentinel",
                    allowed_resume_kinds=("approve", "reject"),
                ),
            )
        )
        snapshot = asyncio.run(
            waiting_runtime.assistant_graph_app.aget_state(waiting_identity)
        )

        assert waiting.status == "waiting_user"
        assert waiting.response is None
        assert [event.type for event in waiting_sink.events] == ["task_started"]
        assert tool.terminals == []
        assert snapshot.next == ("await_input",)
        assert snapshot.values["graph_name"] == ASSISTANT_GRAPH_NAME
        assert snapshot.values["graph_version"] == ASSISTANT_GRAPH_VERSION
        assert snapshot.values["state_schema_version"] == (
            ASSISTANT_STATE_SCHEMA_VERSION
        )
        assert snapshot.values["profile"] == "standard"
        assert snapshot.values["run"]["run_id"] == waiting_identity.run_id
    finally:
        waiting_runtime.close()

    resumed_sink = ListEventSink()
    resumed_runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="final-resume-sentinel",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        allow_interrupt=True,
    )
    resumed_identity = GraphExecutionIdentity.for_assistant_turn(
        agent_id=resumed_runtime.agent_id,
        user_id=request.user_id,
        session_id=request.session_id,
        run_id="run-after-resume-sentinel",
    )
    try:
        resumed = asyncio.run(
            resumed_runtime.aresume_state(
                request,
                resume=AssistantApproveResume(
                    action_ref="provider-resume-sentinel",
                ),
                event_sink=resumed_sink,
                run_id=resumed_identity.run_id,
            )
        )

        assert resumed_identity.thread_id == waiting_identity.thread_id
        assert resumed_identity.run_id != waiting_identity.run_id
        assert resumed.status == "completed"
        assert resumed.run_id == resumed_identity.run_id
        assert resumed.trace_id == waiting.trace_id
        assert resumed.response is not None
        assert resumed.response.message == "final-resume-sentinel"
        assert [call.tool_name for call in resumed.tool_calls] == [tool.name]
        assert [result.success for result in resumed.tool_results] == [True]
        assert [
            (call.tool_name, call.input, call.status)
            for call in resumed.tool_calls
        ] == [
            (call.tool_name, call.input, call.status)
            for call in baseline.tool_calls
        ]
        assert [
            (result.tool_name, result.success, result.output_ref)
            for result in resumed.tool_results
        ] == [
            (result.tool_name, result.success, result.output_ref)
            for result in baseline.tool_results
        ]
        assert [event.type for event in resumed_sink.events].count(
            "final_response"
        ) == 1
        assert tool.terminals == [(resumed_identity.run_id, "completed")]
    finally:
        resumed_runtime.close()


@pytest.mark.core_invariant("TOOL-001")
def test_probe_tool_call_completes_through_governed_runtime() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-sentinel",
                        name=ProbeTool.name,
                        arguments={"value": "value-sentinel"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="final-sentinel",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            )
        )

        assert state.status == "completed"
        assert state.response is not None
        assert [call.tool_name for call in state.tool_calls] == [ProbeTool.name]
        assert len(state.tool_results) == 1
        assert state.tool_results[0].success is True
        assert state.tool_results[0].data == {"value": "value-sentinel"}
        trace_events = runtime.trace_store.list_by_run(state.run_id)
        terminal = next(
            event
            for event in trace_events
            if event.canonical_event == "tool.finished"
        )
        observation = next(
            event
            for event in trace_events
            if event.canonical_event == "tool.observation"
        )
        assert (
            observation.attributes["tool_call_id"]
            == terminal.attributes["tool_call_id"]
        )
        assert observation.attributes["source_tool_span_id"] == terminal.span_id
    finally:
        runtime.close()


@pytest.mark.core_invariant("LOOP-001")
@pytest.mark.core_invariant("TOOL-001")
def test_invalid_tool_input_is_returned_to_model_for_one_repair_attempt() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-invalid-sentinel",
                        name=ProbeTool.name,
                        arguments={"value": ""},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-repaired-sentinel",
                        name=ProbeTool.name,
                        arguments={"value": "repaired-sentinel"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="final-sentinel",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            )
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.message == "final-sentinel"
        assert len(adapter.requests) == 3
        repair_messages = [
            message
            for message in adapter.requests[1].messages
            if message.get("role") == "tool"
        ]
        assert len(repair_messages) == 1
        repair_observation = json.loads(str(repair_messages[0]["content"]))
        assert repair_observation["status"] == "rejected"
        assert repair_observation["error"]["code"] == "invalid_tool_input"
        assert repair_observation["error"]["retryable"] is True
        assert [result.data for result in state.tool_results] == [
            {"value": "repaired-sentinel"}
        ]
    finally:
        runtime.close()


@pytest.mark.core_invariant("LOOP-001")
@pytest.mark.core_invariant("TOOL-001")
def test_repeated_invalid_tool_input_enters_answer_only_finalization() -> None:
    def invalid_call(call_id: str) -> NativeToolCall:
        return NativeToolCall(
            id=call_id,
            name=ProbeTool.name,
            arguments={"value": ""},
        )

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[invalid_call("call-invalid-first")],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    invalid_call("call-invalid-second"),
                    NativeToolCall(
                        id="call-after-limit-sentinel",
                        name=ProbeTool.name,
                        arguments={"value": "must-not-run-sentinel"},
                    ),
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="safe-final-sentinel",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            )
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.message == "safe-final-sentinel"
        assert len(adapter.requests) == 3
        assert adapter.requests[-1].tools == []
        assert adapter.requests[-1].tool_choice == "none"
        assert state.tool_results == []
        finalization_tool_messages = [
            message
            for message in adapter.requests[-1].messages
            if message.get("role") == "tool"
        ]
        assert finalization_tool_messages
        finalization_observations = [
            json.loads(str(message["content"]))
            for message in finalization_tool_messages
        ]
        assert all(
            observation["status"] == "rejected"
            and observation["is_complete"] is False
            and observation["error"]["code"] == "invalid_tool_input"
            and observation["error"]["retryable"] is False
            and "data" not in observation
            and isinstance(observation["summary"], str)
            and observation["summary"]
            and isinstance(observation["error"]["message"], str)
            and observation["error"]["message"]
            for observation in finalization_observations
        )
        assert state.request.metadata["assistant_finalize_reason"] == (
            "invalid_tool_input_limit"
        )
    finally:
        runtime.close()


@pytest.mark.core_invariant("LOOP-001")
@pytest.mark.core_invariant("TOOL-001")
def test_nonrecoverable_failure_blocks_later_same_tool_in_native_batch() -> None:
    failing_tool = _NonrecoverableProbeTool()
    independent_tool = _IndependentProbeTool()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-failing-first",
                        name=failing_tool.name,
                        arguments={"value": "first"},
                    ),
                    NativeToolCall(
                        id="call-failing-second",
                        name=failing_tool.name,
                        arguments={"value": "second"},
                    ),
                    NativeToolCall(
                        id="call-independent",
                        name=independent_tool.name,
                        arguments={"value": "independent"},
                    ),
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="final-sentinel",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(failing_tool, independent_tool),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            )
        )

        assert failing_tool.executed_values == ["first"]
        assert independent_tool.executed_values == ["independent"]
        tool_messages = [
            message
            for message in adapter.requests[1].messages
            if message.get("role") == "tool"
        ]
        assert [message.get("tool_call_id") for message in tool_messages] == [
            "call-failing-first",
            "call-failing-second",
            "call-independent",
        ]
        blocked = json.loads(str(tool_messages[1]["content"]))
        assert blocked["error"]["code"] == "nonrecoverable_tool_retry_blocked"
        assert state.status == "completed"
    finally:
        runtime.close()


@pytest.mark.core_invariant("LOOP-001")
def test_provider_timeout_returns_structured_terminal_reason() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                errors=[
                    ChatProviderError(
                        code="provider_timeout",
                        message="error-sentinel",
                        recoverable=True,
                    )
                ],
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            )
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.data["fallback_reason"] == "provider_timeout"
        assert state.response.message
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
def test_cancelled_run_emits_no_final_response() -> None:
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            event_sink=sink,
            cancel_token=CancelledToken(),
        )

        assert state.status == "cancelled"
        assert state.response is None
        assert sink.events[-1].type == "task_cancelled"
        assert "final_response" not in [event.type for event in sink.events]
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.parametrize("expected_status", ["completed", "failed", "cancelled"])
def test_runtime_notifies_optional_tool_lifecycle_at_every_run_terminal(
    expected_status: str,
) -> None:
    tool = _RunLifecycleProbeTool()
    registry = sealed_registry(tool)
    runtime = AgentGraphRuntime(
        registry=registry,
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="final-sentinel",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
                task_execution_mode=(
                    "durable" if expected_status == "failed" else "auto"
                ),
            ),
            run_id="run-terminal-sentinel",
            cancel_token=(
                CancelledToken() if expected_status == "cancelled" else None
            ),
        )

        assert state.status == expected_status
        assert tool.terminals == [("run-terminal-sentinel", expected_status)]
    finally:
        runtime.close()


@pytest.mark.core_invariant("RUN-001")
def test_registry_terminal_lifecycle_accepts_failed_terminal_status() -> None:
    tool = _RunLifecycleProbeTool()
    registry = sealed_registry(tool)

    issues = registry.notify_run_terminal("run-failed-sentinel", "failed")

    assert issues == []
    assert tool.terminals == [("run-failed-sentinel", "failed")]


@pytest.mark.core_invariant("OBS-001")
def test_core_event_reaches_gateway_frame() -> None:
    realtime_events = map_agent_event_stream(
        AgentEvent(
            type="final_response",
            session_id="session-sentinel",
            run_id="run-sentinel",
            text="value-sentinel",
        )
    )
    frame = realtime_event_to_frame(
        realtime_events[0],
        session_id="session-sentinel",
        turn_id="turn-sentinel",
        run_id="run-sentinel",
    )

    assert frame is not None
    assert frame["type"] == "stream.chunk"
    assert frame["session_id"] == "session-sentinel"
    assert frame["run_id"] == "run-sentinel"
    assert frame["payload"]["text"] == "value-sentinel"


@pytest.mark.core_invariant("IDENT-001")
def test_user_session_runs_are_isolated_and_request_identity_fields_are_preserved() -> None:
    sessions = InMemorySessionStore()
    sessions.touch_run(
        user_id="user-a-sentinel",
        session_id="session-sentinel",
        run_id="run-a-sentinel",
        trace_id="trace-a-sentinel",
        message_preview="value-a-sentinel",
        status="completed",
    )
    sessions.touch_run(
        user_id="user-b-sentinel",
        session_id="session-sentinel",
        run_id="run-b-sentinel",
        trace_id="trace-b-sentinel",
        message_preview="value-b-sentinel",
        status="completed",
    )

    session_a = sessions.get("user-a-sentinel", "session-sentinel")
    session_b = sessions.get("user-b-sentinel", "session-sentinel")
    user_a_identity = RequestIdentity.for_user(
        user_id="user-a-sentinel",
        agent_id="agent-a-sentinel",
        session_id="session-sentinel",
    )
    other_agent_identity = RequestIdentity.for_user(
        user_id="user-a-sentinel",
        agent_id="agent-b-sentinel",
        session_id="session-sentinel",
    )

    assert session_a is not None
    assert session_b is not None
    assert session_a.last_run_id == "run-a-sentinel"
    assert session_b.last_run_id == "run-b-sentinel"
    assert [
        record.last_run_id
        for record in sessions.list_by_user("user-a-sentinel")
    ] == ["run-a-sentinel"]
    assert [
        record.last_run_id
        for record in sessions.list_by_user("user-b-sentinel")
    ] == ["run-b-sentinel"]
    assert user_a_identity.model_dump() == {
        "user_id": "user-a-sentinel",
        "agent_id": "agent-a-sentinel",
        "session_id": "session-sentinel",
    }
    assert other_agent_identity.model_dump() == {
        "user_id": "user-a-sentinel",
        "agent_id": "agent-b-sentinel",
        "session_id": "session-sentinel",
    }
