from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import TypeAdapter, ValidationError

from assistant_agent.runtime.assistant_interrupts import (
    AssistantApproveResume,
    AssistantInputResume,
    AssistantInterruptContractError,
    AssistantInterruptRequest,
    AssistantRejectResume,
    AssistantResume,
    assistant_turn_action_ref,
)
from assistant_agent.runtime.assistant_graph_app import (
    GraphExecutionError,
    GraphExecutionIdentity,
)
from assistant_agent.runtime.assistant_loop_graph import build_assistant_loop_graph
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.tool_operation_barrier import SQLiteToolOperationStore
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.run_history import RunHistoryStore
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import (
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


class _WriteProbe(ProbeTool):
    name = "interrupt_write_probe"
    category = "write"


class _LifecycleProbe(ProbeTool):
    name = "interrupt_lifecycle_probe"

    def __init__(self) -> None:
        self.terminals: list[tuple[str, str]] = []

    def on_run_terminal(self, run_id: str, status: str) -> None:
        self.terminals.append((run_id, status))


class _MixedStreamingToolCallAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, *, tool_name: str) -> None:
        self.tool_name = tool_name

    def chat(self, request):
        if request.stream_callback is not None:
            request.stream_callback(
                "provisional text that must not be delivered",
                {
                    "provider": self.provider,
                    "model": self.model,
                    "finish_reason": None,
                },
            )
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="tool_calls",
            response_text="provisional text that must not be delivered",
            tool_calls=[
                NativeToolCall(
                    id="provider-mixed-stream",
                    name=self.tool_name,
                    arguments={"value": "guarded"},
                )
            ],
        )


def _request() -> UserRequest:
    return UserRequest(
        user_id="user-interrupt",
        session_id="session-interrupt",
        text="Run the trusted probe.",
    )


def _runtime(
    saver: InMemorySaver,
    responses: list[ChatResult],
) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(responses),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )


def _prepare(
    runtime: AgentGraphRuntime,
    *,
    run_id: str,
    interrupt_request: AssistantInterruptRequest | None = None,
):
    return runtime._prepare_graph_run(  # noqa: SLF001 - internal Graph API TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        pre_terminal_state_hook=None,
        run_id=run_id,
        interrupt_request=interrupt_request,
    )


def test_interrupt_contract_is_strict_and_graph_has_real_await_input_topology() -> None:
    """Removing the discriminated contract or native conditional node must fail."""

    request = AssistantInterruptRequest(
        kind="approval",
        prompt="Approve the pending action?",
        action_ref="provider-call-1",
        allowed_resume_kinds=("approve", "reject"),
    )
    resume_adapter = TypeAdapter(AssistantResume)

    assert request.model_dump(mode="json") == {
        "schema_version": 1,
        "kind": "approval",
        "prompt": "Approve the pending action?",
        "action_ref": "provider-call-1",
        "allowed_resume_kinds": ["approve", "reject"],
    }
    assert isinstance(
        resume_adapter.validate_python(
            {"schema_version": 1, "kind": "approve", "action_ref": "provider-call-1"}
        ),
        AssistantApproveResume,
    )
    assert isinstance(
        resume_adapter.validate_python(
            {
                "schema_version": 1,
                "kind": "reject",
                "action_ref": "provider-call-1",
                "reason": "Not now.",
            }
        ),
        AssistantRejectResume,
    )
    assert isinstance(
        resume_adapter.validate_python(
            {
                "schema_version": 1,
                "kind": "provide_input",
                "action_ref": "assistant-turn:run-1",
                "text": "Use the second option.",
            }
        ),
        AssistantInputResume,
    )
    with pytest.raises(ValidationError):
        AssistantInterruptRequest(
            kind="approval",
            prompt="Approve?",
            action_ref="provider-call-1",
            allowed_resume_kinds=("provide_input",),
        )
    with pytest.raises(ValidationError):
        resume_adapter.validate_python(
            {"schema_version": 1, "kind": "cancel", "action_ref": "provider-call-1"}
        )
    with pytest.raises(ValidationError):
        AssistantInterruptRequest(
            kind="input",
            prompt="Authorization: Bearer leaked-secret",
            action_ref="assistant-turn:run-1",
            allowed_resume_kinds=("provide_input",),
        )

    graph = build_assistant_loop_graph()
    drawable = graph.get_graph()
    assert "await_input" in drawable.nodes
    edges = {(edge.source, edge.target) for edge in drawable.edges}
    assert ("assistant", "time_travel_anchor") in edges
    assert ("await_input", "time_travel_anchor") in edges
    assert ("time_travel_anchor", "prepare_invocation") in edges
    assert ("prepare_invocation", "await_input") in edges
    assert ("prepare_invocation", "execute_tool") in edges
    assert ("prepare_invocation", "assistant") in edges


def test_approval_interrupt_is_a_native_pending_checkpoint_before_tool_execution() -> None:
    """Bypassing await_input or executing the Tool before its checkpoint must fail."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-approval-1",
                        name=ProbeTool.name,
                        arguments={"value": "guarded-value"},
                    )
                ],
            )
        ],
    )
    prepared = _prepare(
        runtime,
        run_id="run-interrupt-1",
        interrupt_request=AssistantInterruptRequest(
            kind="approval",
            prompt="Approve the pending probe?",
            action_ref="provider-approval-1",
            allowed_resume_kinds=("approve", "reject"),
        ),
    )
    try:
        result = asyncio.run(
            runtime.assistant_graph_app.arun(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            )
        )
        snapshot = asyncio.run(
            runtime.assistant_graph_app.aget_state(prepared.identity)
        )
        history = asyncio.run(
            runtime.assistant_graph_app.aget_state_history(
                prepared.identity,
                limit=10,
            )
        )

        assert result.status == "interrupted"
        assert [item.model_dump(mode="json") for item in result.interrupts] == [
            {
                "schema_version": 1,
                "interrupt_id": result.interrupts[0].interrupt_id,
                "kind": "approval",
                "prompt": "Approve the pending probe?",
                "action_ref": "provider-approval-1",
                "allowed_resume_kinds": ["approve", "reject"],
            }
        ]
        assert result.checkpoint_config == snapshot.config
        assert snapshot.next == ("await_input",)
        assert snapshot.tasks
        assert snapshot.interrupts
        assert snapshot.values["pending_interrupt"]["action_ref"] == "provider-approval-1"
        assert snapshot.values["pending_tool_calls"][0]["provider_call_id"] == (
            "provider-approval-1"
        )
        assert snapshot.values["run"]["tool_calls"] == []
        assert snapshot.values["run"]["tool_results"] == []
        assert any(
            item.values.get("pending_interrupt", {}).get("action_ref")
            == "provider-approval-1"
            and item.values.get("pending_tool_calls")
            for item in history
        )
    finally:
        runtime.close()


def test_rebuilt_app_resumes_approval_on_same_thread_with_new_invocation_run() -> None:
    """Using a new thread/run incorrectly or re-gating after approve must fail."""

    saver = InMemorySaver()
    first = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-approval-rebuild",
                        name=ProbeTool.name,
                        arguments={"value": "approved-value"},
                    )
                ],
            )
        ],
    )
    original = _prepare(
        first,
        run_id="run-before-approval",
        interrupt_request=AssistantInterruptRequest(
            kind="approval",
            prompt="Approve this action?",
            action_ref="provider-approval-rebuild",
            allowed_resume_kinds=("approve", "reject"),
        ),
    )
    try:
        interrupted = asyncio.run(
            first.assistant_graph_app.arun(
                original.initial_state,
                identity=original.identity,
                context=original.runtime_context,
            )
        )
        assert interrupted.status == "interrupted"
    finally:
        first.close()

    rebuilt = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="approved-final",
            )
        ],
    )
    resumed = _prepare(rebuilt, run_id="run-after-approval")
    try:
        result = asyncio.run(
            rebuilt.assistant_graph_app.aresume(
                identity=resumed.identity,
                context=resumed.runtime_context,
                resume=AssistantApproveResume(
                    action_ref="provider-approval-rebuild"
                ),
            )
        )

        assert resumed.identity.thread_id == original.identity.thread_id
        assert resumed.identity.run_id != original.identity.run_id
        assert result.status == "completed"
        assert result.interrupts == ()
        assert result.final_state["run"]["run_id"] == "run-after-approval"
        assert result.final_state["pending_interrupt"] is None
        assert result.final_state["pending_tool_calls"] == []
        assert [item["tool_name"] for item in result.final_state["run"]["tool_calls"]] == [
            ProbeTool.name
        ]
        assert result.final_state["run"]["tool_calls"][0]["status"] == "succeeded"
        assert result.final_state["final_response"]["message"] == "approved-final"
    finally:
        rebuilt.close()


def test_reject_resume_clears_pending_call_without_executing_tool() -> None:
    """A rejected pending call must not survive routing or reach ToolExecutor."""

    saver = InMemorySaver()
    first = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-reject-1",
                        name=ProbeTool.name,
                        arguments={"value": "must-not-run"},
                    )
                ],
            )
        ],
    )
    original = _prepare(
        first,
        run_id="run-before-reject",
        interrupt_request=AssistantInterruptRequest(
            kind="approval",
            prompt="Approve this action?",
            action_ref="provider-reject-1",
            allowed_resume_kinds=("approve", "reject"),
        ),
    )
    try:
        asyncio.run(
            first.assistant_graph_app.arun(
                original.initial_state,
                identity=original.identity,
                context=original.runtime_context,
            )
        )
    finally:
        first.close()

    rebuilt = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="rejected-final",
            )
        ],
    )
    resumed = _prepare(rebuilt, run_id="run-after-reject")
    try:
        result = asyncio.run(
            rebuilt.assistant_graph_app.aresume(
                identity=resumed.identity,
                context=resumed.runtime_context,
                resume=AssistantRejectResume(
                    action_ref="provider-reject-1",
                    reason="Operator rejected this action.",
                ),
            )
        )

        assert result.status == "completed"
        assert result.final_state["pending_interrupt"] is None
        assert result.final_state["pending_tool_calls"] == []
        assert result.final_state["run"]["tool_calls"] == []
        assert result.final_state["run"]["tool_results"] == []
        assert result.final_state["tool_observations"][0]["status"] == "rejected"
        assert result.final_state["tool_observations"][0]["provider_call_id"] == (
            "provider-reject-1"
        )
        assert result.final_state["final_response"]["message"] == "rejected-final"
    finally:
        rebuilt.close()


def test_provide_input_clears_pending_call_and_reenters_assistant_with_new_text() -> None:
    """A supplied answer must replace stale action state before the next assistant node."""

    saver = InMemorySaver()
    first = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-stale-input-call",
                        name=ProbeTool.name,
                        arguments={"value": "must-not-run"},
                    )
                ],
            )
        ],
    )
    original = _prepare(first, run_id="run-before-input")
    input_ref = assistant_turn_action_ref(original.state.run_id)
    original.initial_state["pending_interrupt"] = AssistantInterruptRequest(
        kind="input",
        prompt="Which option should be used?",
        action_ref=input_ref,
        allowed_resume_kinds=("provide_input",),
    ).model_dump(mode="json")
    try:
        interrupted = asyncio.run(
            first.assistant_graph_app.arun(
                original.initial_state,
                identity=original.identity,
                context=original.runtime_context,
            )
        )
        assert interrupted.status == "interrupted"
    finally:
        first.close()

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="input-final",
            )
        ]
    )
    rebuilt = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )
    resumed = _prepare(rebuilt, run_id="run-after-input")
    try:
        result = asyncio.run(
            rebuilt.assistant_graph_app.aresume(
                identity=resumed.identity,
                context=resumed.runtime_context,
                resume=AssistantInputResume(
                    action_ref=input_ref,
                    text="Use the second option.",
                ),
            )
        )

        assert result.status == "completed"
        assert result.final_state["pending_interrupt"] is None
        assert result.final_state["pending_tool_calls"] == []
        assert result.final_state["run"]["tool_calls"] == []
        assert result.final_state["request"]["text"] == "Use the second option."
        assert result.final_state["request"]["messages"][-1]["text"] == (
            "Use the second option."
        )
        assert adapter.requests[0].user_query == "Use the second option."
        assert result.final_state["final_response"]["message"] == "input-final"
    finally:
        rebuilt.close()


def test_write_category_without_trusted_request_does_not_automatically_interrupt(
    tmp_path,
) -> None:
    """Inferring HITL from a write category instead of structured policy must fail."""

    saver = InMemorySaver()
    tool = _WriteProbe()
    runtime = AgentGraphRuntime(
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
                            id="provider-write-no-gate",
                            name=tool.name,
                            arguments={"value": "ordinary-write"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="write-finished",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        tool_operation_store=SQLiteToolOperationStore(
            tmp_path / "operations.sqlite3"
        ),
    )
    prepared = _prepare(runtime, run_id="run-write-without-gate")
    try:
        result = asyncio.run(
            runtime.assistant_graph_app.arun(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            )
        )

        assert result.status == "completed"
        assert result.interrupts == ()
        assert result.final_state["run"]["tool_calls"][0]["tool_name"] == tool.name
        assert result.final_state["run"]["tool_calls"][0]["status"] == "succeeded"
    finally:
        runtime.close()


def test_resume_preflight_rejects_wrong_ref_kind_thread_and_reused_run() -> None:
    """Any resume not bound to the exact pending thread/action must fail closed."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-preflight-1",
                        name=ProbeTool.name,
                        arguments={"value": "preflight"},
                    )
                ],
            )
        ],
    )
    pending = _prepare(
        runtime,
        run_id="run-preflight-pending",
        interrupt_request=AssistantInterruptRequest(
            kind="approval",
            prompt="Approve?",
            action_ref="provider-preflight-1",
            allowed_resume_kinds=("approve", "reject"),
        ),
    )
    try:
        interrupted = asyncio.run(
            runtime.assistant_graph_app.arun(
                pending.initial_state,
                identity=pending.identity,
                context=pending.runtime_context,
            )
        )
        interrupt_id = interrupted.interrupts[0].interrupt_id

        fresh = _prepare(runtime, run_id="run-preflight-new")
        cases = [
            (
                fresh.identity,
                fresh.runtime_context,
                AssistantApproveResume(action_ref="provider-wrong-ref"),
                "assistant_resume_action_ref_mismatch",
            ),
            (
                fresh.identity,
                fresh.runtime_context,
                AssistantInputResume(
                    action_ref="provider-preflight-1",
                    text="not an approval",
                ),
                "assistant_resume_kind_not_allowed",
            ),
            (
                pending.identity,
                pending.runtime_context,
                AssistantApproveResume(action_ref="provider-preflight-1"),
                "graph_invocation_run_id_reused",
            ),
            (
                GraphExecutionIdentity(
                    thread_id="assistant:wrong-thread",
                    run_id="run-preflight-wrong-thread",
                    agent_id=pending.identity.agent_id,
                ),
                fresh.runtime_context,
                AssistantApproveResume(action_ref="provider-preflight-1"),
                "graph_invocation_identity_mismatch",
            ),
        ]
        for identity, context, resume, expected_code in cases:
            with pytest.raises(GraphExecutionError) as captured:
                asyncio.run(
                    runtime.assistant_graph_app.aresume(
                        identity=identity,
                        context=context,
                        resume=resume,
                    )
                )
            assert captured.value.code == expected_code

        still_pending = asyncio.run(runtime.assistant_graph_app.aget_state(pending.identity))
        assert still_pending.interrupts[0].id == interrupt_id
        assert still_pending.values["pending_interrupt"]["action_ref"] == (
            "provider-preflight-1"
        )
    finally:
        runtime.close()


def test_interrupt_request_must_bind_the_current_pending_action() -> None:
    """A trusted request for another Tool call must not create an orphan gate."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-actual-action",
                        name=ProbeTool.name,
                        arguments={"value": "must-not-run"},
                    )
                ],
            )
        ],
    )
    prepared = _prepare(
        runtime,
        run_id="run-action-binding",
        interrupt_request=AssistantInterruptRequest(
            kind="approval",
            prompt="Approve?",
            action_ref="provider-another-action",
            allowed_resume_kinds=("approve", "reject"),
        ),
    )
    try:
        with pytest.raises(AssistantInterruptContractError) as captured:
            asyncio.run(
                runtime.assistant_graph_app.arun(
                    prepared.initial_state,
                    identity=prepared.identity,
                    context=prepared.runtime_context,
                )
            )

        assert captured.value.code == "interrupt_action_ref_mismatch"
        snapshot = asyncio.run(runtime.assistant_graph_app.aget_state(prepared.identity))
        assert snapshot.values["run"]["tool_calls"] == []
        assert snapshot.interrupts == ()
    finally:
        runtime.close()


def test_resume_without_pending_interrupt_fails_closed() -> None:
    """Command(resume) must never start a completed or unrelated thread."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="already-complete",
            )
        ],
    )
    completed = _prepare(runtime, run_id="run-already-complete")
    try:
        result = asyncio.run(
            runtime.assistant_graph_app.arun(
                completed.initial_state,
                identity=completed.identity,
                context=completed.runtime_context,
            )
        )
        assert result.status == "completed"
        fresh = _prepare(runtime, run_id="run-no-pending-resume")

        with pytest.raises(GraphExecutionError) as captured:
            asyncio.run(
                runtime.assistant_graph_app.aresume(
                    identity=fresh.identity,
                    context=fresh.runtime_context,
                    resume=AssistantApproveResume(action_ref="provider-no-pending"),
                )
            )

        assert captured.value.code == "graph_interrupt_not_pending"
    finally:
        runtime.close()


def test_runtime_interrupt_is_waiting_nonterminal_and_buffers_mixed_stream_delivery(
    tmp_path,
) -> None:
    """Finalizing waiting state or delivering mixed text before approval must fail."""

    saver = InMemorySaver()
    tool = _LifecycleProbe()
    sink = ListEventSink()
    history = RunHistoryStore(tmp_path / "interrupt-history.jsonl")
    runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=_MixedStreamingToolCallAdapter(tool_name=tool.name),
        session_store=InMemorySessionStore(),
        event_sink=sink,
        run_history=history,
        checkpointer=saver,
        allow_interrupt=True,
    )
    try:
        state = asyncio.run(
            runtime.arun_state(
                _request(),
                run_id="run-runtime-waiting",
                interrupt_request=AssistantInterruptRequest(
                    kind="approval",
                    prompt="Approve the mixed streaming action?",
                    action_ref="provider-mixed-stream",
                    allowed_resume_kinds=("approve", "reject"),
                ),
            )
        )

        assert state.status == "waiting_user"
        assert state.response is None
        assert state.tool_calls == []
        assert state.tool_results == []
        assert [record.status for record in history.read_all()] == ["started"]
        assert tool.terminals == []
        assert [event.type for event in sink.events] == ["task_started"]
    finally:
        runtime.close()


def test_input_interrupt_preempts_a_direct_answer_and_resumes_from_assistant() -> None:
    """A direct first answer must not bypass an explicit input interrupt."""

    saver = InMemorySaver()
    first = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="stale-answer-before-input",
            )
        ],
    )
    original = _prepare(first, run_id="run-direct-input-before")
    input_ref = assistant_turn_action_ref(original.state.run_id)
    original.initial_state["pending_interrupt"] = AssistantInterruptRequest(
        kind="input",
        prompt="Provide the missing selection.",
        action_ref=input_ref,
        allowed_resume_kinds=("provide_input",),
    ).model_dump(mode="json")
    try:
        interrupted = asyncio.run(
            first.assistant_graph_app.arun(
                original.initial_state,
                identity=original.identity,
                context=original.runtime_context,
            )
        )
        assert interrupted.status == "interrupted"
        assert interrupted.final_state["run"]["status"] == "running"
        assert interrupted.final_state["final_response"] is None
    finally:
        first.close()

    rebuilt = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="answer-after-input",
            )
        ],
    )
    resumed = _prepare(rebuilt, run_id="run-direct-input-after")
    try:
        result = asyncio.run(
            rebuilt.assistant_graph_app.aresume(
                identity=resumed.identity,
                context=resumed.runtime_context,
                resume=AssistantInputResume(
                    action_ref=input_ref,
                    text="Use option B.",
                ),
            )
        )
        assert result.status == "completed"
        assert result.final_state["final_response"]["message"] == (
            "answer-after-input"
        )
    finally:
        rebuilt.close()


@pytest.mark.parametrize(
    ("checkpoint_update", "expected_code"),
    [
        ({"profile": "worker"}, "graph_profile_mismatch"),
        ({"state_schema_version": 999}, "graph_checkpoint_incompatible"),
    ],
)
def test_resume_preflight_rejects_profile_and_schema_mismatch(
    checkpoint_update: dict[str, object],
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading an incompatible checkpoint as the current graph would corrupt resume."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver,
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-version-1",
                        name=ProbeTool.name,
                        arguments={"value": "version"},
                    )
                ],
            )
        ],
    )
    pending = _prepare(
        runtime,
        run_id="run-version-pending",
        interrupt_request=AssistantInterruptRequest(
            kind="approval",
            prompt="Approve?",
            action_ref="provider-version-1",
            allowed_resume_kinds=("approve",),
        ),
    )
    try:
        asyncio.run(
            runtime.assistant_graph_app.arun(
                pending.initial_state,
                identity=pending.identity,
                context=pending.runtime_context,
            )
        )
        snapshot = asyncio.run(runtime.assistant_graph_app.aget_state(pending.identity))
        corrupted_values = json.loads(json.dumps(snapshot.values))
        corrupted_values.update(checkpoint_update)
        corrupted = SimpleNamespace(
            values=corrupted_values,
            next=snapshot.next,
            tasks=snapshot.tasks,
            interrupts=snapshot.interrupts,
            config=snapshot.config,
        )

        async def corrupted_state(_identity: GraphExecutionIdentity):
            return corrupted

        monkeypatch.setattr(
            runtime.assistant_graph_app,
            "aget_state",
            corrupted_state,
        )
        fresh = _prepare(runtime, run_id="run-version-new")

        with pytest.raises(GraphExecutionError) as captured:
            asyncio.run(
                runtime.assistant_graph_app.aresume(
                    identity=fresh.identity,
                    context=fresh.runtime_context,
                    resume=AssistantApproveResume(action_ref="provider-version-1"),
                )
            )

        assert captured.value.code == expected_code
    finally:
        runtime.close()
