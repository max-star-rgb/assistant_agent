from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID
from assistant_agent.runtime.assistant_graph_app import GraphExecutionIdentity
from assistant_agent.runtime.assistant_graph_state import (
    ASSISTANT_GRAPH_NAME,
    ASSISTANT_GRAPH_VERSION,
    ASSISTANT_STATE_SCHEMA_VERSION,
    AssistantStateCompatibilityError,
    assistant_loop_state_from_turn_state,
    assistant_turn_state_from_agent_state,
    assistant_turn_state_from_loop_state,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.run_phase import RunPhase
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import AssistantToolCall, NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.models import ToolResult
from tests.core.support import (
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


_FORBIDDEN_TYPES = (
    AgentState,
    UserRequest,
    Path,
    bytes,
    bytearray,
)


def _walk(value: Any) -> None:
    assert not isinstance(value, _FORBIDDEN_TYPES), type(value).__name__
    module = type(value).__module__
    name = type(value).__name__
    assert not (
        module.startswith("assistant_agent")
        and name in {"ToolResult", "ToolExecutor", "ToolRegistry"}
    ), f"forbidden checkpoint object: {module}.{name}"
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item)


def _runtime(
    *, saver: InMemorySaver, adapter: ScriptedChatAdapter
) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )


def _identity(run_id: str) -> GraphExecutionIdentity:
    return GraphExecutionIdentity.for_assistant_turn(
        agent_id=DEFAULT_AGENT_ID,
        user_id="user-state",
        session_id="session-state",
        run_id=run_id,
    )


def test_real_compiled_checkpoint_contains_only_plain_json_state() -> None:
    """Returning legacy runtime models from any node must make this checkpoint audit fail."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver=saver,
        adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="done-state",
                )
            ]
        ),
    )
    try:
        result = runtime.run_state(
            UserRequest(
                user_id="user-state",
                session_id="session-state",
                text="checkpoint me",
                image_ids=["image-ref-1"],
                video_ids=["video-ref-1"],
                audio_id="audio-ref-1",
                metadata={"artifact_body": b"must-not-persist"},
            ),
            run_id="run-state-1",
        )
        assert result.status == "completed"
        snapshot = runtime.assistant_graph_app.graph.get_state(
            _identity("run-state-1").runnable_config()
        )
        values = snapshot.values
        _walk(values)
        json.dumps(values)
        assert values["graph_name"] == ASSISTANT_GRAPH_NAME
        assert values["graph_version"] == ASSISTANT_GRAPH_VERSION
        assert values["state_schema_version"] == ASSISTANT_STATE_SCHEMA_VERSION
        assert values["request"]["media_refs"] == [
            {"kind": "image", "ref": "image-ref-1"},
            {"kind": "video", "ref": "video-ref-1"},
            {"kind": "audio", "ref": "audio-ref-1"},
        ]
        assert "artifact_body" not in json.dumps(values)
    finally:
        runtime.close()


def test_incompatible_state_version_fails_closed() -> None:
    """Accepting an unknown schema version would silently resume with wrong semantics."""

    state = AgentState.from_request(
        UserRequest(user_id="u", session_id="s", text="hello"),
        run_id="run-version",
        trace_id="trace-version",
    )
    persisted = assistant_turn_state_from_agent_state(state)
    persisted["state_schema_version"] = 999

    with pytest.raises(AssistantStateCompatibilityError) as exc_info:
        validate_assistant_turn_state(persisted)

    assert exc_info.value.code == "assistant_state_version_incompatible"


def test_stable_thread_new_turn_overwrites_all_run_scoped_channels() -> None:
    """LangGraph's checkpoint merge must not leak the prior turn trajectory."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver=saver,
        adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="provider-call-1",
                            name=ProbeTool.name,
                            arguments={"value": "first"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="first-finished",
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="second-finished",
                ),
            ]
        ),
    )
    try:
        first = runtime.run_state(
            UserRequest(user_id="user-state", session_id="session-state", text="first"),
            run_id="run-state-1",
        )
        assert first.tool_results
        first_values = runtime.assistant_graph_app.graph.get_state(
            _identity("run-state-1").runnable_config()
        ).values
        assert first_values["tool_observations"][0]["safe_details"] == [
            {"name": "value", "value_json": '"first"'}
        ]
        assert "audit_payload" not in json.dumps(first_values)
        assert "raw_data" not in json.dumps(first_values)
        second = runtime.run_state(
            UserRequest(
                user_id="user-state", session_id="session-state", text="second"
            ),
            run_id="run-state-2",
        )
        assert second.response is not None
        assert second.response.message == "second-finished"
        values = runtime.assistant_graph_app.graph.get_state(
            _identity("run-state-2").runnable_config()
        ).values
        assert values["run"]["run_id"] == "run-state-2"
        assert values["run"]["errors"] == []
        assert values["run"]["tool_calls"] == []
        assert values["run"]["tool_results"] == []
        assert values["outputs_by_step"] == []
        assert values["pending_tool_calls"] == []
        assert values["tool_observations"] == []
        assert values["assistant_iterations"] == 1
        assert values["tool_calls_used"] == 0
        assert values["action_tool_calls_used"] == 0
        assert values["control_tool_calls_used"] == 0
        assert values["pending_interrupt"] is None
        assert values["final_response"]["message"] == "second-finished"
    finally:
        runtime.close()


def _prepare(runtime: AgentGraphRuntime, request: UserRequest, *, run_id: str) -> Any:
    return runtime._prepare_graph_run(  # noqa: SLF001 - explicit graph recovery TDD.
        request,
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id=run_id,
    )


@pytest.mark.parametrize(
    ("interrupt_before", "interrupt_after", "expected_message"),
    [
        (None, ["assistant"], "resumed-final"),
        (None, ["execute_tool"], "resumed-final"),
        (["compose_response"], None, "first-runtime-final"),
    ],
)
def test_fresh_runtime_resumes_intermediate_checkpoint_without_losing_trajectory(
    interrupt_before: list[str] | None,
    interrupt_after: list[str] | None,
    expected_message: str,
) -> None:
    """A rebuilt app must treat checkpoint DTOs—not mutable node objects—as truth."""

    saver = InMemorySaver()
    request = UserRequest(
        user_id="user-state", session_id="session-state", text="resume"
    )
    first_runtime = _runtime(
        saver=saver,
        adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="provider-resume-call",
                            name=ProbeTool.name,
                            arguments={"value": "resume-value"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="first-runtime-final",
                ),
            ]
        ),
    )
    prepared = _prepare(first_runtime, request, run_id="run-resume")
    config = prepared.identity.runnable_config()
    try:
        interrupted = first_runtime.assistant_graph_app.graph.invoke(
            prepared.initial_state,
            config=config,
            context=prepared.runtime_context,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
        )
    finally:
        first_runtime.close()

    resumed_runtime = _runtime(
        saver=saver,
        adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="resumed-final",
                ),
            ]
        ),
    )
    resumed = _prepare(resumed_runtime, request, run_id="run-resume")
    resumed.state.trace_id = interrupted["run"]["trace_id"]
    try:
        final = resumed_runtime.assistant_graph_app.graph.invoke(
            None,
            config=config,
            context=resumed.runtime_context,
        )
        result = resumed_runtime._complete_graph_execution(  # noqa: SLF001
            resumed,
            final,
        )
        assert result.status == "completed"
        assert result.response is not None
        assert result.response.message == expected_message
        assert [call.tool_name for call in result.tool_calls] == [ProbeTool.name]
        assert [item.tool_name for item in result.tool_results] == [ProbeTool.name]
        assert (
            final["tool_observations"][0]["provider_call_id"] == "provider-resume-call"
        )
        assert final["tool_observations"][0]["safe_details"] == [
            {"name": "value", "value_json": '"resume-value"'}
        ]
    finally:
        resumed_runtime.close()


def test_checkpoint_observation_projection_rejects_generic_sensitive_payloads() -> None:
    """A generic JSON fact bag must not become a checkpoint escape hatch."""

    state = AgentState.from_request(
        UserRequest(user_id="u", session_id="s", text="observe"),
        run_id="run-observe",
        trace_id="trace-observe",
    )
    persisted = assistant_turn_state_from_loop_state(
        {
            "state": state,
            "outputs_by_step": {},
            "current_step_index": 0,
            "assistant_output": None,
            "pending_tool_calls": [],
            "assistant_iterations": 1,
            "tool_calls_used": 1,
            "action_tool_calls_used": 1,
            "control_tool_calls_used": 0,
            "run_phase": RunPhase.ACT,
            "tool_observations": [
                {
                    "tool_name": ProbeTool.name,
                    "status": "succeeded",
                    "summary": "safe-summary",
                    "data": {
                        "value": "safe-value",
                        "access_token": "secret-token",
                        "raw_response": {"body": "secret-body"},
                        "media_body": "data:image/png;base64,AAAA",
                        "local_path": "/home/private/file.png",
                        "nested": {
                            "value": "must-not-survive-container",
                            "access_token": "nested-secret",
                        },
                        "unknown_scalar": "must-not-survive-unknown",
                    },
                }
            ],
        }
    )

    encoded = json.dumps(persisted)
    assert "safe-value" in encoded
    assert "secret-token" not in encoded
    assert "secret-body" not in encoded
    assert "data:image" not in encoded
    assert "/home/private" not in encoded
    assert "must-not-survive-container" not in encoded
    assert "nested-secret" not in encoded
    assert "must-not-survive-unknown" not in encoded
    hydrated = assistant_loop_state_from_turn_state(persisted, runtime_state=state)
    assert hydrated["tool_observations"][0]["data"] == {"value": "safe-value"}


class _ContractProbeTool(ProbeTool):
    name = "contract_probe_tool"

    def _run(self, input: Any, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value, "runtime_only": "rich-local-only"},
            model_observation={"value": input.value},
            output_ref="artifact://contract-output",
            contract=build_capability_output_contract(
                capability="contract_probe",
                status="succeeded",
                output_ref="artifact://contract-output",
                data={
                    "summary": "contract-summary",
                    "total": 1,
                },
                metadata={
                    "provider": "probe-provider",
                    "latency_ms": 7,
                },
            ),
        )


class _ContractOnlyRefProbeTool(_ContractProbeTool):
    name = "contract_only_ref_probe_tool"

    def _run(self, input: Any, context: ToolContext) -> ToolResult:
        result = super()._run(input, context)
        return result.model_copy(update={"tool_name": self.name, "output_ref": None})


def _stable_response_payload(state: AgentState) -> dict[str, Any]:
    assert state.response is not None
    payload = state.response.model_dump(mode="json")
    data = dict(payload.get("data") or {})
    data.pop("runtime_only", None)
    payload["data"] = data
    return payload


def test_tool_after_restart_preserves_capability_contract_response_data() -> None:
    """Rebuilding after Tool completion must retain the public contract response."""

    def contract_request() -> UserRequest:
        return UserRequest(
            user_id="user-state", session_id="session-state", text="contract"
        )

    tool = _ContractProbeTool()
    calls = [
        ChatResult(
            provider="scripted",
            model="scripted-model",
            finish_reason="tool_calls",
            tool_calls=[
                NativeToolCall(
                    id="contract-call",
                    name=tool.name,
                    arguments={"value": "contract-value"},
                )
            ],
        ),
        ChatResult(
            provider="scripted",
            model="scripted-model",
            finish_reason="stop",
            response_text="contract-final",
        ),
    ]
    baseline = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(list(calls)),
        session_store=InMemorySessionStore(),
    )
    try:
        uninterrupted = baseline.run_state(
            contract_request(), run_id="run-contract-baseline"
        )
    finally:
        baseline.close()

    saver = InMemorySaver()
    first = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([calls[0]]),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )
    prepared = _prepare(first, contract_request(), run_id="run-contract-resume")
    config = prepared.identity.runnable_config()
    try:
        checkpoint = first.assistant_graph_app.graph.invoke(
            prepared.initial_state,
            config=config,
            context=prepared.runtime_context,
            interrupt_after=["execute_tool"],
        )
    finally:
        first.close()

    rebuilt = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([calls[1]]),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )
    resumed = _prepare(rebuilt, contract_request(), run_id="run-contract-resume")
    resumed.state.trace_id = checkpoint["run"]["trace_id"]
    try:
        final = rebuilt.assistant_graph_app.graph.invoke(
            None,
            config=config,
            context=resumed.runtime_context,
        )
        restarted = rebuilt._complete_graph_execution(resumed, final)  # noqa: SLF001
    finally:
        rebuilt.close()

    baseline_payload = _stable_response_payload(uninterrupted)
    restart_payload = _stable_response_payload(restarted)
    assert restart_payload["message"] == baseline_payload["message"]
    assert restart_payload["output_refs"] == baseline_payload["output_refs"]
    assert restart_payload["data"]["contracts"] == baseline_payload["data"]["contracts"]


def test_contract_only_output_ref_does_not_become_tool_result_output_ref_after_restart() -> (
    None
):
    """A contract-owned ref must not change the existing result output-ref semantics."""

    tool = _ContractOnlyRefProbeTool()
    request = lambda: UserRequest(  # noqa: E731 - fresh mutable request per run.
        user_id="user-state", session_id="session-state", text="contract-only-ref"
    )
    tool_call = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="tool_calls",
        tool_calls=[
            NativeToolCall(
                id="contract-only-call",
                name=tool.name,
                arguments={"value": "contract-value"},
            )
        ],
    )
    final_answer = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="stop",
        response_text="contract-only-final",
    )
    baseline = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([tool_call, final_answer]),
        session_store=InMemorySessionStore(),
    )
    try:
        uninterrupted = baseline.run_state(request(), run_id="contract-only-baseline")
    finally:
        baseline.close()

    saver = InMemorySaver()
    first = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([tool_call]),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )
    prepared = _prepare(first, request(), run_id="contract-only-resume")
    config = prepared.identity.runnable_config()
    try:
        checkpoint = first.assistant_graph_app.graph.invoke(
            prepared.initial_state,
            config=config,
            context=prepared.runtime_context,
            interrupt_after=["execute_tool"],
        )
    finally:
        first.close()
    assert checkpoint["run"]["tool_results"][0]["output_ref"] is None
    assert (
        checkpoint["run"]["tool_results"][0]["capability_contract"]["output_ref"]
        == "artifact://contract-output"
    )

    rebuilt = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([final_answer]),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )
    resumed = _prepare(rebuilt, request(), run_id="contract-only-resume")
    resumed.state.trace_id = checkpoint["run"]["trace_id"]
    try:
        final = rebuilt.assistant_graph_app.graph.invoke(
            None, config=config, context=resumed.runtime_context
        )
        restarted = rebuilt._complete_graph_execution(resumed, final)  # noqa: SLF001
    finally:
        rebuilt.close()

    assert restarted.tool_results[0].output_ref is None
    assert _stable_response_payload(restarted) == _stable_response_payload(
        uninterrupted
    )


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "authorization=Bearer secret-token",
        "https://user:password@example.com/private",
        "https://example.com/file?X-Amz-Signature=secret",
        "data:image/png;base64,AAAA",
        "file:///home/private/file.png",
        "/home/private/file.png",
        "artifact body secret-content",
    ],
)
def test_capability_contract_rejects_unsafe_scalar_strings(
    unsafe_value: str,
) -> None:
    """All contract strings, including independent refs, need checkpoint redaction."""

    state = AgentState.from_request(
        UserRequest(user_id="u", session_id="s", text="contract-safety"),
        run_id="run-contract-safety",
        trace_id="trace-contract-safety",
    )
    record = state.add_tool_call("contract-safety-tool", {})
    result = ToolResult(
        tool_name="contract-safety-tool",
        success=True,
        output_ref=unsafe_value,
        contract=build_capability_output_contract(
            capability="contract-safety",
            status="succeeded",
            output_ref=unsafe_value,
            data={"summary": unsafe_value, "render_ref": unsafe_value},
            metadata={"provider": unsafe_value},
        ),
    )
    state.complete_tool_call(record.tool_call_id, result)
    persisted = assistant_turn_state_from_agent_state(state)
    encoded = json.dumps(persisted)

    assert unsafe_value not in encoded


_UNSAFE_CHECKPOINT_ARGUMENTS = [
    {"access_token": "secret"},
    {"token": "secret"},
    {"cookie": "session=secret"},
    {"client_secret": "secret"},
    {"session": "secret"},
    {"signature": "secret"},
    {"nested": {"authorization": "Bearer secret"}},
    {"query": "access_token=secret"},
    {"query": "Cookie: session=secret"},
    {"query": "https://example.com/file?X-Amz-Signature=secret"},
    {"query": "https://example.com/file?X-Goog-Signature=secret"},
    {"query": "https://example.com/file?OSSAccessKeyId=key&Signature=secret"},
    {"query": "https://example.com/file?sv=1&sp=r&se=tomorrow&sig=secret"},
    {"query": "https://user:secret@example.com/private"},
    {"content": "data:image/png;base64,AAAA"},
    {"content": "payload; base64,AAAA"},
    {"path": "/home/user/private.png"},
    {"path": r"C:\Users\user\private.png"},
    {"path": "../uploads/private.png"},
    {"content": "x" * 16_001},
]

_SEMANTIC_SECRET_KEY_VARIANTS = [
    "access_token_backup",
    "refresh_token",
    "auth_token",
    "private_key",
    "aws_secret_access_key",
]


def _loop_state_with_pending_argument(payload: object) -> dict[str, Any]:
    state = AgentState.from_request(
        UserRequest(user_id="u", session_id="s", text="argument safety"),
        run_id="run-argument-safety",
        trace_id="trace-argument-safety",
    )
    return {
        "state": state,
        "outputs_by_step": {},
        "current_step_index": 0,
        "assistant_output": None,
        "pending_tool_calls": [
            AssistantToolCall(
                tool_name=ProbeTool.name,
                tool_input={"payload": payload},
                provider_tool_call_id="provider-argument-safety",
            )
        ],
        "assistant_iterations": 1,
        "tool_calls_used": 0,
        "action_tool_calls_used": 0,
        "control_tool_calls_used": 0,
        "run_phase": RunPhase.ACT,
        "tool_observations": [],
    }


@pytest.mark.parametrize("payload", _UNSAFE_CHECKPOINT_ARGUMENTS)
@pytest.mark.parametrize("boundary", ["recorded", "pending"])
def test_tool_arguments_fail_closed_on_unsafe_nested_checkpoint_values(
    payload: object,
    boundary: str,
) -> None:
    """Unsafe Tool payloads must abort projection instead of being silently dropped."""

    if boundary == "pending":
        project = lambda: assistant_turn_state_from_loop_state(  # noqa: E731
            _loop_state_with_pending_argument(payload)
        )
    else:
        state = AgentState.from_request(
            UserRequest(user_id="u", session_id="s", text="argument safety"),
            run_id="run-argument-safety",
            trace_id="trace-argument-safety",
        )
        state.add_tool_call(ProbeTool.name, {"payload": payload})
        project = lambda: assistant_turn_state_from_agent_state(state)  # noqa: E731

    with pytest.raises(ValueError, match="assistant_state_checkpoint_value_unsafe"):
        project()


@pytest.mark.parametrize("secret_key", _SEMANTIC_SECRET_KEY_VARIANTS)
@pytest.mark.parametrize("boundary", ["recorded", "pending"])
def test_tool_arguments_fail_closed_on_semantic_secret_key_variants(
    secret_key: str,
    boundary: str,
) -> None:
    """Suffixes and provider prefixes must not bypass semantic key detection."""

    payload = {"outer": {secret_key: "secret"}}
    if boundary == "pending":
        project = lambda: assistant_turn_state_from_loop_state(  # noqa: E731
            _loop_state_with_pending_argument(payload)
        )
    else:
        state = AgentState.from_request(
            UserRequest(user_id="u", session_id="s", text="semantic key safety"),
            run_id="run-semantic-key-safety",
            trace_id="trace-semantic-key-safety",
        )
        state.add_tool_call(ProbeTool.name, {"payload": payload})
        project = lambda: assistant_turn_state_from_agent_state(state)  # noqa: E731

    with pytest.raises(ValueError, match="assistant_state_checkpoint_value_unsafe"):
        project()


def test_nested_client_secret_backup_fails_closed() -> None:
    """A nested suffixed client secret is rejected at every JSON depth."""

    with pytest.raises(ValueError, match="assistant_state_checkpoint_value_unsafe"):
        assistant_turn_state_from_loop_state(
            _loop_state_with_pending_argument(
                {"outer": [{"inner": {"client_secret_backup": "secret"}}]}
            )
        )


def test_checkpoint_argument_validator_allows_bounded_json_and_public_urls() -> None:
    """Normal Tool queries and stable refs remain valid checkpoint inputs."""

    payload = {
        "query": "summarize https://example.com/articles?q=agent " + "context " * 400,
        "filters": {"enabled": True, "count": 3, "scores": [1, 2.5, None]},
        "usage": {"token_count": 128, "token_budget": 4_096},
        "accessibility": "screen-reader",
        "output_ref": "artifact://safe-output-123",
    }
    persisted = assistant_turn_state_from_loop_state(
        _loop_state_with_pending_argument(payload)
    )

    encoded = persisted["pending_tool_calls"][0]["arguments"][0]["value_json"]
    assert json.loads(encoded) == payload


def test_checkpoint_argument_validator_preserves_empty_value_for_tool_repair() -> None:
    """Schema-invalid JSON still has to reach ActionValidator and the model repair loop."""

    persisted = assistant_turn_state_from_loop_state(
        _loop_state_with_pending_argument({"value": ""})
    )

    encoded = persisted["pending_tool_calls"][0]["arguments"][0]["value_json"]
    assert json.loads(encoded) == {"value": ""}


@pytest.mark.parametrize("boundary", ["recorded", "pending"])
def test_checkpoint_argument_validator_preserves_browser_session_reference(
    boundary: str,
) -> None:
    """Website Guidance resumes with its schema-bound opaque browser reference."""

    payload = {"browser_session_id": "opaque-browser-session-1", "action": "back"}
    if boundary == "pending":
        loop_state = _loop_state_with_pending_argument(None)
        loop_state["pending_tool_calls"] = [
            AssistantToolCall(
                tool_name="web_page_explore",
                tool_input=payload,
                provider_tool_call_id="provider-browser-ref",
            )
        ]
        persisted = assistant_turn_state_from_loop_state(loop_state)
        arguments = persisted["pending_tool_calls"][0]["arguments"]
    else:
        state = AgentState.from_request(
            UserRequest(user_id="u", session_id="s", text="website guidance"),
            run_id="run-browser-ref",
            trace_id="trace-browser-ref",
        )
        state.add_tool_call("web_page_explore", payload)
        persisted = assistant_turn_state_from_agent_state(state)
        arguments = persisted["run"]["tool_calls"][0]["arguments"]

    restored = {item["name"]: json.loads(item["value_json"]) for item in arguments}
    assert restored == payload


def test_checkpoint_sanitizer_does_not_inspect_free_form_user_text() -> None:
    """Credential education text is valid input; only persistence-risk fields are strict."""

    text = "Explain access_token=example and data:image/png;base64,AAAA safely."
    state = AgentState.from_request(
        UserRequest(user_id="u", session_id="s", text=text),
        run_id="run-user-text",
        trace_id="trace-user-text",
    )

    assert assistant_turn_state_from_agent_state(state)["request"]["text"] == text


def test_node_hydration_applies_persisted_trajectory_to_fresh_agent_state() -> None:
    """A fresh runtime state must not erase trajectory when a later node projects it."""

    original = AgentState.from_request(
        UserRequest(user_id="u", session_id="s", text="trajectory"),
        run_id="run-trajectory",
        trace_id="trace-trajectory",
    )
    record = original.add_tool_call(
        ProbeTool.name,
        {"value": "trajectory-value"},
        tool_call_id="trajectory-call",
    )
    original.complete_tool_call(
        record.tool_call_id,
        ProbeTool().run({"value": "trajectory-value"}),
    )
    persisted = assistant_turn_state_from_loop_state(
        {
            "state": original,
            "outputs_by_step": {},
            "current_step_index": 0,
            "assistant_output": None,
            "pending_tool_calls": [],
            "assistant_iterations": 2,
            "tool_calls_used": 1,
            "action_tool_calls_used": 1,
            "control_tool_calls_used": 0,
            "run_phase": RunPhase.ACT,
            "tool_observations": [],
        }
    )
    fresh = AgentState.from_request(
        original.request,
        run_id=original.run_id,
        trace_id=original.trace_id,
    )

    hydrated = assistant_loop_state_from_turn_state(persisted, runtime_state=fresh)
    reprojected = assistant_turn_state_from_loop_state(hydrated)

    assert reprojected["run"]["tool_calls"] == persisted["run"]["tool_calls"]
    assert reprojected["run"]["tool_results"] == persisted["run"]["tool_results"]


@pytest.mark.parametrize("mismatch", ["context", "capability", "catalog"])
def test_resume_fails_closed_when_runtime_refs_do_not_match_checkpoint(
    mismatch: str,
) -> None:
    """Resume must not silently substitute a changed context/capability snapshot."""

    saver = InMemorySaver()
    request = UserRequest(user_id="user-state", session_id="session-state", text="refs")
    runtime = _runtime(
        saver=saver,
        adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="not-used",
                )
            ]
        ),
    )
    prepared = _prepare(runtime, request, run_id="run-refs")
    if mismatch == "context":
        prepared.initial_state["context_refs"] = [
            {
                "kind": "context_section",
                "ref": "context-ref-missing",
                "source": "editable_file",
                "version": "v1",
                "status_code": None,
            }
        ]
    elif mismatch == "capability":
        prepared.initial_state["capability_refs"] = ["grant-missing"]
    else:
        prepared.initial_state["catalog"] = {
            "schema_version": "run_tool_catalog_v1",
            "available_tool_names": ["tool-not-registered"],
            "selection_reason_codes": [],
            "exclusion_reason_codes": [],
        }
    try:
        with pytest.raises(AssistantStateCompatibilityError) as exc_info:
            runtime.assistant_graph_app.graph.invoke(
                prepared.initial_state,
                config=prepared.identity.runnable_config(),
                context=prepared.runtime_context,
            )
        assert exc_info.value.code == "assistant_state_version_incompatible"
    finally:
        runtime.close()
