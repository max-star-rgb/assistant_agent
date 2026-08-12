from __future__ import annotations

from contextlib import contextmanager
import importlib
import asyncio
from typing import Any
from contextlib import nullcontext
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import tracing_context
from langsmith.run_trees import RunTree

from assistant_agent.observability.langsmith_config import LangSmithConfig
from assistant_agent.runtime.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
    ProviderProtocolResponse,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.runtime.assistant_graph_app import (
    AssistantTurnGraphApp,
    GraphExecutionIdentity,
)
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from assistant_agent.providers.provider_policy import ProviderExecutionPolicy, RetryPolicy
from tests.core.support import ProbeTool, ScriptedChatAdapter, offline_config, sealed_registry


def _native() -> Any:
    return importlib.import_module("assistant_agent.observability.langsmith_native")


def _enabled_config() -> LangSmithConfig:
    return LangSmithConfig(enabled=True, api_key="key-sentinel", project="project-sentinel")


def test_active_experiment_tree_is_inherited_without_opening_local_context(
    monkeypatch,
) -> None:
    """Opening a new root over an Experiment parent must break parent ownership."""

    native = _native()
    opened: list[dict[str, Any]] = []

    @contextmanager
    def unexpected_context(**kwargs: Any):
        opened.append(kwargs)
        yield

    external_client = _ClientProbe()
    current_parent = type("CurrentParent", (), {"client": external_client})()
    monkeypatch.setattr(native, "get_current_run_tree", lambda: current_parent)
    monkeypatch.setattr(native, "tracing_context", unexpected_context)

    with native.native_langsmith_tracing(
        _enabled_config(), metadata={"run_id": "run-sentinel"}, tags=["runtime"]
    ):
        pass

    assert opened == []
    assert external_client.flush_timeouts == []
    assert external_client.close_timeouts == []


class _ClientProbe:
    def __init__(self) -> None:
        self.flush_timeouts: list[float | None] = []
        self.close_timeouts: list[float | None] = []

    def flush(self, timeout: float | None = None) -> None:
        self.flush_timeouts.append(timeout)

    def close(self, timeout: float | None = None) -> None:
        self.close_timeouts.append(timeout)


def test_enabled_root_uses_scoped_context_and_closes_only_owned_client(
    monkeypatch,
) -> None:
    """Dropping scoped parameters or leaking the owned client must break lifecycle."""

    native = _native()
    client = _ClientProbe()
    opened: list[dict[str, Any]] = []

    @contextmanager
    def recording_context(**kwargs: Any):
        opened.append(kwargs)
        yield

    monkeypatch.setattr(native, "get_current_run_tree", lambda: None)
    monkeypatch.setattr(native, "create_langsmith_client", lambda config: client)
    monkeypatch.setattr(native, "tracing_context", recording_context)

    with native.native_langsmith_tracing(
        _enabled_config(),
        metadata={"run_id": "run-sentinel"},
        tags=["assistant_turn_graph"],
    ):
        pass

    assert opened == [
        {
            "project_name": "project-sentinel",
            "metadata": {"run_id": "run-sentinel"},
            "tags": ["assistant_turn_graph"],
            "enabled": True,
            "client": client,
        }
    ]
    assert client.flush_timeouts == [2.0]
    assert client.close_timeouts == [2.0]


def test_disabled_or_broken_daily_tracing_is_fail_open(monkeypatch) -> None:
    """An optional SDK/configuration failure must not suppress graph execution."""

    native = _native()
    client_calls: list[LangSmithConfig] = []
    monkeypatch.setattr(native, "get_current_run_tree", lambda: None)
    monkeypatch.setattr(
        native,
        "create_langsmith_client",
        lambda config: client_calls.append(config),
    )

    entered: list[str] = []
    with native.native_langsmith_tracing(
        LangSmithConfig(enabled=False), metadata={}, tags=[]
    ):
        entered.append("disabled")

    monkeypatch.setattr(
        native,
        "create_langsmith_client",
        lambda config: (_ for _ in ()).throw(RuntimeError("sdk-sentinel")),
    )
    with native.native_langsmith_tracing(
        _enabled_config(), metadata={}, tags=[]
    ):
        entered.append("broken")

    assert entered == ["disabled", "broken"]
    assert client_calls == []


def test_current_parent_lookup_failure_does_not_create_a_competing_root(
    monkeypatch,
) -> None:
    """Treating an unknown parent as absent must not create or activate a new tree."""

    native = _native()
    client = _ClientProbe()
    calls: list[str] = []

    def fail_lookup() -> Any:
        raise RuntimeError("lookup-sentinel")

    monkeypatch.setattr(native, "get_current_run_tree", fail_lookup)
    monkeypatch.setattr(
        native,
        "create_langsmith_client",
        lambda config: calls.append("client") or client,
    )
    monkeypatch.setattr(
        native,
        "tracing_context",
        lambda **kwargs: calls.append("context") or nullcontext(),
    )

    entered: list[bool] = []
    with native.native_langsmith_tracing(
        _enabled_config(), metadata={"run_id": "run-sentinel"}, tags=[]
    ):
        entered.append(native.native_tracing_active())

    assert entered == [False]
    assert calls == []
    assert client.flush_timeouts == []
    assert client.close_timeouts == []


def test_context_setup_and_teardown_fail_open_and_close_owned_client(monkeypatch) -> None:
    """A context manager failure must not leak its client or replace business output."""

    native = _native()
    clients = [_ClientProbe(), _ClientProbe()]

    class BrokenContext:
        def __init__(self, phase: str) -> None:
            self.phase = phase

        def __enter__(self) -> None:
            if self.phase == "enter":
                raise RuntimeError("enter-sentinel")

        def __exit__(self, *args: Any) -> None:
            if self.phase == "exit":
                raise RuntimeError("exit-sentinel")

    phases = iter(("enter", "exit"))
    monkeypatch.setattr(native, "get_current_run_tree", lambda: None)
    monkeypatch.setattr(native, "create_langsmith_client", lambda config: clients.pop(0))
    monkeypatch.setattr(native, "tracing_context", lambda **kwargs: BrokenContext(next(phases)))

    entered: list[str] = []
    first_client, second_client = clients
    with native.native_langsmith_tracing(_enabled_config(), metadata={}, tags=[]):
        entered.append("enter-failed")
    with native.native_langsmith_tracing(_enabled_config(), metadata={}, tags=[]):
        entered.append("exit-failed")

    assert entered == ["enter-failed", "exit-failed"]
    assert first_client.flush_timeouts == [2.0]
    assert first_client.close_timeouts == [2.0]
    assert second_client.flush_timeouts == [2.0]
    assert second_client.close_timeouts == [2.0]


def test_llm_projection_excludes_callbacks_raw_payload_reasoning_and_media() -> None:
    """Adding SDK callbacks, protocol envelopes, secrets, or inline media must stay hidden."""

    native = _native()
    request = ChatRequest(
        user_id="raw-user-sentinel",
        session_id="raw-session-sentinel",
        user_query="query-sentinel",
        messages=[
            {"role": "user", "content": "safe-message"},
            {"role": "user", "content": "authorization=credential-inline-secret"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "safe-part"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,media-secret"},
                    },
                ],
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "probe_tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        stream_callback=lambda *_: None,
        provider_request_callback=lambda *_: None,
    )
    result = ChatResult(
        response_text="safe-response",
        provider="provider-sentinel",
        model="model-sentinel",
        usage={"input_tokens": 3, "output_tokens": 2},
        reasoning_content="hidden-reasoning",
        protocol_response=ProviderProtocolResponse(
            transport_mode="sync",
            content="raw-sdk-envelope",
        ),
        errors=[
            ChatProviderError(
                code="safe-error-code",
                message="credential-secret",
            )
        ],
    )

    inputs = native.project_llm_inputs(
        request, provider="provider-sentinel", model="model-sentinel"
    )
    outputs = native.project_llm_outputs(result)
    serialized = repr((inputs, outputs))

    assert inputs == {
        "provider": "provider-sentinel",
        "model": "model-sentinel",
        "messages": [
            {"role": "user", "content": "safe-message"},
            {"role": "user", "content": "[redacted]"},
            {"role": "user", "content": [{"type": "text", "text": "safe-part"}]},
        ],
        "tools": request.tools,
        "tool_choice": None,
        "response_format": None,
        "temperature": 0.2,
        "max_tokens": 512,
    }
    assert outputs == {
        "provider": "provider-sentinel",
        "model": "model-sentinel",
        "response_text": "safe-response",
        "tool_calls": [],
        "finish_reason": None,
        "refusal": None,
        "usage": {"input_tokens": 3, "output_tokens": 2},
        "errors": [{"code": "safe-error-code", "recoverable": False}],
    }
    for forbidden in (
        "raw-user-sentinel",
        "raw-session-sentinel",
        "media-secret",
        "hidden-reasoning",
        "raw-sdk-envelope",
        "credential-secret",
        "credential-inline-secret",
        "provider_request_callback",
        "stream_callback",
    ):
        assert forbidden not in serialized


def test_tool_projection_is_bounded_and_removes_secret_media_and_raw_payload() -> None:
    """Returning a rich ToolResult must not turn LangSmith into a payload dump."""

    native = _native()
    safe_input = native.project_tool_input(
        {
            "query": "safe-query",
            "api_key": "credential-secret",
            "image": "data:image/png;base64,media-secret",
            "nested": {"path": "/private/media.jpg", "count": 2},
        }
    )
    safe_output = native.project_tool_output(
        ToolResult(
            tool_name="probe_tool",
            success=True,
            data={
                "answer": "safe-answer",
                "unknown_semantic": "private-natural-language-body",
                "raw_provider_payload": "raw-payload-secret",
                "bytes": b"media-bytes-secret",
                "https://media.example/object?signature=key-secret": "value-one",
                "signature=signed-key-secret": "value-two",
            },
            output_ref="artifact://safe-ref",
            raw_data_ref="/private/raw.json",
        )
    )
    serialized = repr((safe_input, safe_output))

    assert safe_input == {
        "query": "safe-query",
        "nested": {"count": 2},
    }
    assert safe_output == {
        "tool_name": "probe_tool",
        "success": True,
        "data_field_count": 6,
        "output_ref_present": True,
        "error_code": None,
    }
    for forbidden in (
        "credential-secret",
        "media-secret",
        "/private/media.jpg",
        "raw-payload-secret",
        "private-natural-language-body",
        "media-bytes-secret",
        "/private/raw.json",
        "artifact://safe-ref",
        "key-secret",
        "signed-key-secret",
    ):
        assert forbidden not in serialized


def test_tool_output_projection_never_exports_reference_values() -> None:
    """Artifact, signed remote, and private path references must remain business-only."""

    native = _native()
    references = (
        "artifact://owner/private-ref",
        "https://media.example/object?X-Amz-Signature=signed-secret",
        "/private/media/result.bin",
    )

    projected = [
        native.project_tool_output(
            ToolResult(
                tool_name="probe_tool",
                success=True,
                output_ref=reference,
            )
        )
        for reference in references
    ]

    assert projected == [
        {
            "tool_name": "probe_tool",
            "success": True,
            "data_field_count": 0,
            "output_ref_present": True,
            "error_code": None,
        }
    ] * 3
    serialized = repr(projected)
    for reference in references:
        assert reference not in serialized


def test_tool_role_messages_parse_json_safely_without_hiding_plain_assistant_text() -> None:
    """Tool-result JSON must not bypass redaction through ChatRequest message content."""

    native = _native()
    unsafe_json = (
        '{"answer":"safe-answer","query":"private-query",'
        '"output_ref":"https://media.example/object?signature=signed-secret",'
        '"media":"data:image/png;base64,media-secret",'
        '"message":"C:\\\\Users\\\\private\\\\media.jpg",'
        '"summary":"relative/media/result.jpg",'
        '"unknown_text":"unknown-private-text",'
        '"count":3,'
        '"nested":{"signature":"nested-secret","count":2}}'
    )
    invalid_tool_content = "https://media.example/raw?token=invalid-secret"
    request = ChatRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        user_query="query-sentinel",
        messages=[
            {"role": "assistant", "content": "normal-assistant-text"},
            {
                "role": "tool",
                "tool_call_id": "call-one",
                "name": "probe_tool",
                "output_ref": "https://media.example/top?signature=top-secret",
                "unknown_field": "unknown-top-private",
                "content": unsafe_json,
            },
            {
                "role": "tool",
                "tool_call_id": "call-two",
                "content": invalid_tool_content,
            },
        ],
    )

    projected = native.project_llm_inputs(
        request,
        provider="provider-sentinel",
        model="model-sentinel",
    )["messages"]

    assert projected == [
        {"role": "assistant", "content": "normal-assistant-text"},
        {
            "role": "tool",
            "tool_call_id": "call-one",
            "name": "probe_tool",
            "content": {
                "answer": "safe-answer",
                "count": 3,
                "nested": {"count": 2},
            },
        },
        {
            "role": "tool",
            "tool_call_id": "call-two",
            "content": {
                "redacted": True,
                "content_chars": len(invalid_tool_content),
            },
        },
    ]
    serialized = repr(projected)
    for forbidden in (
        "private-query",
        "https://media.example",
        "signed-secret",
        "media-secret",
        "nested-secret",
        "invalid-secret",
        "C:\\Users",
        "relative/media",
        "unknown-private-text",
        "top-secret",
        "unknown-top-private",
    ):
        assert forbidden not in serialized


def test_llm_and_tool_wrappers_create_actual_child_types_and_preserve_results(
    monkeypatch,
) -> None:
    """Renaming child runs or tracing a projection instead of the call must break the tree."""

    native = _native()
    recorded: list[dict[str, Any]] = []

    def fake_traceable(**options: Any):
        def decorate(function):
            def wrapped(*args: Any, **kwargs: Any):
                item = {
                    "name": options["name"],
                    "run_type": options["run_type"],
                    "inputs": options["process_inputs"](
                        {"args": args, "kwargs": kwargs}
                    ),
                }
                result = function(*args, **kwargs)
                item["outputs"] = options["process_outputs"](result)
                recorded.append(item)
                return result

            return wrapped

        return decorate

    monkeypatch.setattr(native, "traceable", fake_traceable)
    monkeypatch.setattr(native, "get_current_run_tree", lambda: object())
    request = ChatRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        user_query="query-sentinel",
        messages=[{"role": "user", "content": "safe-message"}],
    )
    llm_result = ChatResult(
        response_text="safe-response",
        provider="provider-sentinel",
        model="model-sentinel",
    )
    tool_result = ToolResult(
        tool_name="probe_tool",
        success=True,
        data={"answer": "safe-answer"},
    )
    calls: list[str] = []

    with native.native_langsmith_tracing(
        LangSmithConfig(enabled=False), metadata={}, tags=[]
    ):
        actual_llm = native.trace_llm_call(
            lambda: calls.append("llm") or llm_result,
            request=request,
            provider="provider-sentinel",
            model="model-sentinel",
        )
        actual_tool = native.trace_governed_tool_call(
            lambda: calls.append("tool") or tool_result,
            tool_name="probe_tool",
            safe_input={"query": "safe-query"},
        )

    assert actual_llm is llm_result
    assert actual_tool is tool_result
    assert calls == ["llm", "tool"]
    assert [(item["name"], item["run_type"]) for item in recorded] == [
        ("llm.chat", "llm"),
        ("probe_tool", "tool"),
    ]
    assert recorded[0]["inputs"] == native.project_llm_inputs(
        request, provider="provider-sentinel", model="model-sentinel"
    )
    assert recorded[0]["outputs"] == native.project_llm_outputs(llm_result)
    assert recorded[1]["inputs"] == {"tool_name": "probe_tool", "input": {"query": "safe-query"}}
    assert recorded[1]["outputs"] == native.project_tool_output(tool_result)


def test_tracing_failure_does_not_repeat_or_replace_business_call(monkeypatch) -> None:
    """An SDK post failure must return the one business result without a duplicate side effect."""

    native = _native()
    monkeypatch.setattr(native, "get_current_run_tree", lambda: object())

    def broken_traceable(**options: Any):
        def decorate(function):
            def wrapped(*args: Any, **kwargs: Any):
                function(*args, **kwargs)
                raise RuntimeError("tracing-post-sentinel")

            return wrapped

        return decorate

    monkeypatch.setattr(native, "traceable", broken_traceable)
    result = ToolResult(tool_name="probe_tool", success=True)
    calls: list[str] = []
    with native.native_langsmith_tracing(
        LangSmithConfig(enabled=False), metadata={}, tags=[]
    ):
        actual = native.trace_governed_tool_call(
            lambda: calls.append("called") or result,
            tool_name="probe_tool",
            safe_input={},
        )

    assert actual is result
    assert calls == ["called"]


class _GraphContextProbe:
    def __init__(self, native: Any) -> None:
        self.native = native
        self.sync_active = False
        self.async_active: list[bool] = []
        self.sync_kwargs: dict[str, Any] = {}
        self.async_kwargs: dict[str, Any] = {}

    def invoke(self, input_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.sync_active = self.native.native_tracing_active()
        self.sync_kwargs = kwargs
        return input_state

    async def astream(self, input_state: dict[str, Any], **kwargs: Any):
        self.async_active.append(self.native.native_tracing_active())
        self.async_kwargs = kwargs
        yield {"type": "values", "ns": (), "data": input_state}
        self.async_active.append(self.native.native_tracing_active())


def test_graph_sync_and_async_execution_share_native_context(monkeypatch) -> None:
    """Leaving either graph entry outside context must orphan native node traces."""

    native = _native()
    client = type(
        "NoopClient",
        (),
        {"create_run": lambda self, **kwargs: None, "update_run": lambda self, **kwargs: None},
    )()
    parent = type(
        "CurrentParent",
        (),
        {"client": client, "project_name": "project-sentinel"},
    )()
    monkeypatch.setattr(native, "get_current_run_tree", lambda: parent)
    monkeypatch.setattr(
        native,
        "get_tracing_context",
        lambda: {
            "parent": parent,
            "project_name": "project-sentinel",
            "tags": [],
            "metadata": {},
            "client": client,
        },
    )
    probe = _GraphContextProbe(native)
    app = AssistantTurnGraphApp.from_compiled_graph(probe)
    identity = GraphExecutionIdentity.for_assistant_turn(
        agent_id="agent-sentinel",
        user_id="raw-user-sentinel",
        session_id="raw-session-sentinel",
        run_id="run-sentinel",
    )
    context = GraphRuntimeContext(
        tool_executor=ToolExecutor(registry=sealed_registry()),
        chat_adapter=ScriptedChatAdapter([]),
    )

    sync_result = app.invoke({"state": "sync"}, identity=identity, context=context)
    async_result = asyncio.run(
        app.arun({"state": "async"}, identity=identity, context=context)
    )

    assert sync_result == {"state": "sync"}
    assert async_result.final_state == {"state": "async"}
    assert probe.sync_active is True
    assert probe.async_active == [True, True]


def test_graph_metadata_is_hashed_and_never_tags_raw_user_or_session(monkeypatch) -> None:
    """Adding product identity to tags or metadata must fail the safe graph projection."""

    graph_module = importlib.import_module("assistant_agent.runtime.assistant_graph_app")
    captured: list[dict[str, Any]] = []

    def capture_context(config: Any, *, metadata: dict[str, Any], tags: list[str]):
        captured.append({"metadata": metadata, "tags": tags})
        return nullcontext()

    monkeypatch.setattr(graph_module, "native_langsmith_tracing", capture_context)
    probe = _GraphContextProbe(_native())
    app = AssistantTurnGraphApp.from_compiled_graph(probe)
    identity = GraphExecutionIdentity.for_assistant_turn(
        agent_id="agent-sentinel",
        user_id="raw-user-sentinel",
        session_id="raw-session-sentinel",
        run_id="run-sentinel",
    )
    context = GraphRuntimeContext(
        tool_executor=ToolExecutor(registry=sealed_registry()),
        chat_adapter=ScriptedChatAdapter([]),
    )

    app.invoke({"state": "sync"}, identity=identity, context=context)

    assert captured == [
        {
            "metadata": {
                "run_id": "run-sentinel",
                "thread_id": identity.thread_id,
                "agent_id": "agent-sentinel",
                "execution_engine": "assistant_turn_graph",
                "graph_profile": "standard",
            },
            "tags": ["assistant_turn_graph"],
        }
    ]
    assert "raw-user-sentinel" not in repr(captured)
    assert "raw-session-sentinel" not in repr(captured)
    assert probe.sync_kwargs["config"]["metadata"] == {
        "run_id": "run-sentinel",
        "thread_id": identity.thread_id,
        "agent_id": "agent-sentinel",
        "execution_engine": "assistant_turn_graph",
        "graph_profile": "standard",
    }
    assert probe.sync_kwargs["config"]["tags"] == ["assistant_turn_graph"]


def test_runtime_traces_real_llm_and_governed_backend_boundaries(monkeypatch) -> None:
    """Tracing only projections or skipping the backend attempt must omit native children."""

    native = _native()
    recorded: list[dict[str, Any]] = []

    def fake_traceable(**options: Any):
        def decorate(function):
            def wrapped(*args: Any, **kwargs: Any):
                result = function(*args, **kwargs)
                recorded.append(
                    {
                        "name": options["name"],
                        "run_type": options["run_type"],
                        "inputs": options["process_inputs"]({}),
                        "outputs": options["process_outputs"](result),
                    }
                )
                return result

            return wrapped

        return decorate

    monkeypatch.setattr(native, "traceable", fake_traceable)
    client = type(
        "NoopClient",
        (),
        {"create_run": lambda self, **kwargs: None, "update_run": lambda self, **kwargs: None},
    )()
    parent = type(
        "CurrentParent",
        (),
        {"client": client, "project_name": "project-sentinel"},
    )()
    monkeypatch.setattr(native, "get_current_run_tree", lambda: parent)
    monkeypatch.setattr(
        native,
        "get_tracing_context",
        lambda: {
            "parent": parent,
            "project_name": "project-sentinel",
            "tags": [],
            "metadata": {},
            "client": client,
        },
    )
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
                response_text="done-sentinel",
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
                text="request-sentinel",
            ),
            run_id="run-sentinel",
        )
    finally:
        runtime.close()

    assert state.status == "completed"
    assert [(item["name"], item["run_type"]) for item in recorded] == [
        ("llm.chat", "llm"),
        ("probe_tool", "tool"),
        ("llm.chat", "llm"),
    ]
    assert recorded[1]["inputs"] == {
        "tool_name": "probe_tool",
        "input": {"value": "value-sentinel"},
    }
    assert state.tool_calls[0].status == "succeeded"
    assert state.tool_results[0].data == {"value": "value-sentinel"}


def test_each_tool_retry_traces_only_backend_attempt_before_commit(monkeypatch) -> None:
    """Wrapping retry orchestration or commit must hide attempt count or alter state order."""

    native = _native()
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="request-sentinel",
        ),
        run_id="run-sentinel",
    )
    traced_statuses: list[str] = []

    def fake_traceable(**options: Any):
        def decorate(function):
            def wrapped(*args: Any, **kwargs: Any):
                result = function(*args, **kwargs)
                traced_statuses.append(state.tool_calls[-1].status)
                return result

            return wrapped

        return decorate

    class RetryBackend:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, registry: Any, tool_name: str, tool_input: Any, context: Any):
            self.calls += 1
            assert state.tool_calls[-1].status == "running"
            if self.calls == 1:
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    error="provider_timeout: retry-sentinel",
                )
            return ToolResult(
                tool_name=tool_name,
                success=True,
                data={"value": "value-sentinel"},
            )

    backend = RetryBackend()
    executor = ToolExecutor(
        registry=sealed_registry(),
        execution_backend=backend,
        execution_policy=ProviderExecutionPolicy(
            retry=RetryPolicy(max_retries=1, backoff_seconds=0.0)
        ),
    )
    monkeypatch.setattr(native, "traceable", fake_traceable)
    monkeypatch.setattr(native, "get_current_run_tree", lambda: object())

    with native.native_langsmith_tracing(
        LangSmithConfig(enabled=False), metadata={}, tags=[]
    ):
        result = executor.run_tool(
            state,
            step_id="step-sentinel",
            tool_name="probe_tool",
            tool_input={"value": "value-sentinel"},
        )

    assert result.success is True
    assert backend.calls == 2
    assert traced_statuses == ["running", "running"]
    assert state.tool_calls[0].status == "succeeded"
    assert len(state.tool_results) == 1


def test_native_graph_callback_hides_raw_state_at_chain_persistence_boundary(
    monkeypatch,
) -> None:
    """LangGraph graph/node callbacks must never persist raw runtime state."""

    native = _native()
    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []

    class RecordingClient:
        def create_run(self, **kwargs: Any) -> None:
            created.append(kwargs)

        def update_run(self, run_id: Any, **kwargs: Any) -> None:
            updated.append({"id": run_id, **kwargs})

    monkeypatch.setattr(
        native,
        "get_tracing_context",
        lambda: {
            "parent": None,
            "project_name": "project-sentinel",
            "tags": [],
            "metadata": {},
            "client": RecordingClient(),
        },
    )
    token = native._NATIVE_TRACE_ACTIVE.set(True)
    try:
        callbacks = native.native_graph_callbacks()
    finally:
        native._NATIVE_TRACE_ACTIVE.reset(token)

    assert len(callbacks) == 1
    tracer = callbacks[0]
    for name in ("AssistantTurnGraph", "assistant", "execute_tool", "compose_response"):
        run_id = uuid4()
        tracer.on_chain_start(
            {"name": name},
            {
                "request": {
                    "user_id": "RAW-USER",
                    "session_id": "RAW-SESSION",
                    "text": "RAW-TEXT",
                    "metadata": {
                        "authorization": "RAW-SECRET",
                        "raw_payload": "RAW-PAYLOAD",
                    },
                },
                "state": {"image_id": "RAW-IMAGE", "output_ref": "RAW-REF"},
            },
            name=name,
            run_id=run_id,
        )
        tracer.on_chain_end(
            {"state": {"response": "RAW-OUTPUT", "output_ref": "RAW-REF"}},
            run_id=run_id,
        )

    persisted = repr([created, updated])
    for forbidden in (
        "RAW-USER",
        "RAW-SESSION",
        "RAW-TEXT",
        "RAW-IMAGE",
        "RAW-SECRET",
        "RAW-PAYLOAD",
        "RAW-REF",
        "RAW-OUTPUT",
    ):
        assert forbidden not in persisted
    assert [item["name"] for item in created] == [
        "AssistantTurnGraph",
        "assistant",
        "execute_tool",
        "compose_response",
    ]
    assert all(item["inputs"] == {} for item in created)
    assert all(item["outputs"] in ({}, None) for item in updated)

    error_run_id = uuid4()
    tracer.on_chain_start(
        {"name": "assistant"},
        {"state": "RAW-ERROR-INPUT"},
        name="assistant",
        run_id=error_run_id,
    )
    tracer.on_chain_error(
        RuntimeError("RAW-GRAPH-ERROR-SECRET"),
        inputs={"state": "RAW-ERROR-STATE"},
        run_id=error_run_id,
    )
    error_update = updated[-1]
    assert error_update["error"] == "Graph execution failed."
    assert "RAW-" not in repr(error_update)


def test_real_graph_callback_inherits_experiment_root_without_duplicate_tree() -> None:
    """The explicit safe tracer must preserve one native parented graph tree."""

    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []

    class RecordingClient:
        def create_run(self, **kwargs: Any) -> None:
            created.append(kwargs)

        def update_run(self, run_id: Any, **kwargs: Any) -> None:
            updated.append({"id": run_id, **kwargs})

    builder = StateGraph(dict)
    builder.add_node("assistant", lambda state: {**state, "value": "safe-result"})
    builder.add_edge(START, "assistant")
    builder.add_edge("assistant", END)
    graph = builder.compile(name="AssistantTurnGraph")
    app = AssistantTurnGraphApp.from_compiled_graph(graph)
    client = RecordingClient()
    parent = RunTree(
        name="experiment-item-task",
        inputs={"dataset": "must-remain"},
        outputs={"actual": "must-remain"},
        ls_client=client,
        project_name="project-sentinel",
    )
    identity = GraphExecutionIdentity.for_assistant_turn(
        agent_id="agent-sentinel",
        user_id="raw-user-sentinel",
        session_id="raw-session-sentinel",
        run_id="run-sentinel",
    )

    with tracing_context(parent=parent, enabled=True, client=client):
        result = app.invoke(
            {"value": "RAW-GRAPH-INPUT"},
            identity=identity,
            context=GraphRuntimeContext(
                tool_executor=ToolExecutor(registry=sealed_registry()),
                chat_adapter=ScriptedChatAdapter([]),
            ),
        )

    assert result == {"value": "safe-result"}
    assert parent.inputs == {"dataset": "must-remain"}
    assert parent.outputs == {"actual": "must-remain"}
    assert [run["name"] for run in created] == ["AssistantTurnGraph", "assistant"]
    graph_run, node_run = created
    assert graph_run["parent_run_id"] == parent.id
    assert node_run["parent_run_id"] == graph_run["id"]
    assert graph_run["inputs"] == node_run["inputs"] == {}
    assert "RAW-GRAPH-INPUT" not in repr((created, updated))
    assert graph_run["extra"]["metadata"] == {
        "run_id": "run-sentinel",
        "thread_id": identity.thread_id,
        "agent_id": "agent-sentinel",
        "execution_engine": "assistant_turn_graph",
        "graph_profile": "standard",
        "ls_integration": "langgraph",
    }
    assert "assistant_turn_graph" in graph_run["tags"]


def test_unsupported_safe_tracer_api_disables_remote_graph_without_blocking_run(
    monkeypatch,
) -> None:
    """A LangChain private-API drift must fail closed for trace, open for business."""

    native = _native()
    context_calls: list[dict[str, Any]] = []

    @contextmanager
    def recording_context(**kwargs: Any):
        context_calls.append(kwargs)
        yield

    monkeypatch.setattr(native, "_safe_tracer_api_supported", lambda: False)
    monkeypatch.setattr(native, "tracing_context", recording_context)
    token = native._NATIVE_TRACE_ACTIVE.set(True)
    try:
        with native.native_graph_trace_scope() as callbacks:
            assert native.native_tracing_active() is False
            assert callbacks == []
            business_result = "business-result"
    finally:
        native._NATIVE_TRACE_ACTIVE.reset(token)

    assert business_result == "business-result"
    assert context_calls == [{"enabled": False}]
    assert native._safe_tracer_api_supported() is False


def test_verified_langchain_safe_tracer_callback_api_is_supported() -> None:
    """The M1 callback projection guards the API signatures verified on 1.4.3."""

    assert _native()._safe_tracer_api_supported() is True


def test_safe_tracer_constructor_failure_disables_ambient_graph_trace(
    monkeypatch,
) -> None:
    """A callback construction failure must not hand raw state to auto tracing."""

    native = _native()
    remote_calls: list[dict[str, Any]] = []

    class RecordingClient:
        def create_run(self, **kwargs: Any) -> None:
            remote_calls.append(kwargs)

        def update_run(self, run_id: Any, **kwargs: Any) -> None:
            remote_calls.append({"id": run_id, **kwargs})

    class BrokenSafeTracer:
        def __init__(self, **_kwargs: Any) -> None:
            raise RuntimeError("safe-tracer-construction-failed")

    builder = StateGraph(dict)
    builder.add_node("assistant", lambda state: {**state, "value": "business-ok"})
    builder.add_edge(START, "assistant")
    builder.add_edge("assistant", END)
    app = AssistantTurnGraphApp.from_compiled_graph(
        builder.compile(name="AssistantTurnGraph")
    )
    client = RecordingClient()
    parent = RunTree(
        name="experiment-item-task",
        inputs={"dataset": "must-remain"},
        ls_client=client,
        project_name="project-sentinel",
    )
    monkeypatch.setattr(native, "SafeLangChainTracer", BrokenSafeTracer)
    monkeypatch.setattr(native, "_safe_tracer_api_supported", lambda: True)

    with tracing_context(parent=parent, enabled=True, client=client):
        result = app.invoke(
            {"value": "RAW-STATE"},
            identity=GraphExecutionIdentity.for_assistant_turn(
                agent_id="agent-sentinel",
                user_id="raw-user-sentinel",
                session_id="raw-session-sentinel",
                run_id="run-sentinel",
            ),
            context=GraphRuntimeContext(
                tool_executor=ToolExecutor(registry=sealed_registry()),
                chat_adapter=ScriptedChatAdapter([]),
            ),
        )

    assert result == {"value": "business-ok"}
    assert parent.inputs == {"dataset": "must-remain"}
    assert remote_calls == []


def test_graph_trace_uses_contextvar_fallback_when_public_disable_paths_fail(
    monkeypatch,
) -> None:
    """Broken public disable helpers must still run business code without a trace."""

    native = _native()
    remote_calls: list[dict[str, Any]] = []

    class RecordingClient:
        def create_run(self, **kwargs: Any) -> None:
            remote_calls.append(kwargs)

        def update_run(self, run_id: Any, **kwargs: Any) -> None:
            remote_calls.append({"id": run_id, **kwargs})

    class BrokenContext:
        def __enter__(self) -> None:
            raise RuntimeError("public-disable-failed")

    builder = StateGraph(dict)
    builder.add_node("assistant", lambda state: {**state, "value": "business-ok"})
    builder.add_edge(START, "assistant")
    builder.add_edge("assistant", END)
    app = AssistantTurnGraphApp.from_compiled_graph(
        builder.compile(name="AssistantTurnGraph")
    )
    client = RecordingClient()
    parent = RunTree(
        name="experiment-item-task",
        inputs={"dataset": "must-remain"},
        ls_client=client,
        project_name="project-sentinel",
    )
    monkeypatch.setattr(native, "native_graph_callbacks", lambda: [])
    monkeypatch.setattr(native, "tracing_context", lambda **_kwargs: BrokenContext())
    monkeypatch.setattr(
        native,
        "_set_tracing_context",
        lambda _context: (_ for _ in ()).throw(RuntimeError("private-disable-failed")),
    )

    with tracing_context(parent=parent, enabled=True, client=client):
        result = app.invoke(
            {"value": "RAW-STATE"},
            identity=GraphExecutionIdentity.for_assistant_turn(
                agent_id="agent-sentinel",
                user_id="raw-user-sentinel",
                session_id="raw-session-sentinel",
                run_id="run-sentinel",
            ),
            context=GraphRuntimeContext(
                tool_executor=ToolExecutor(registry=sealed_registry()),
                chat_adapter=ScriptedChatAdapter([]),
            ),
        )

    assert result == {"value": "business-ok"}
    assert parent.inputs == {"dataset": "must-remain"}
    assert remote_calls == []


def test_remote_projection_redacts_signed_urls_credentials_and_paths() -> None:
    """Remote LangSmith projection is stricter than optional local trace content."""

    native = _native()
    signed_url = (
        "https://media.example/private?X-Amz-Credential=credential-secret"
        "&X-Amz-Signature=signature-secret"
    )
    ordinary_url = "https://docs.example/article?q=langgraph"
    media_url = "https://cdn.example/private/frame.png"
    relative_media_path = "generated/private-media.jpg"
    embedded_references = (
        "download artifact://owner/artifact-secret now; "
        "open file://private/file-secret.pdf then "
        "decode data:text/plain,data-secret; "
        "see generated/embedded-media-secret.jpg or "
        r"C:\private\windows-media-secret.png"
    )
    credential_urls = (
        "https://user:userinfo-secret@example.com/article "
        "https://example.com/object?sig=query-secret "
        "https://example.com/object?X-Amz-Expires=amz-secret"
    )
    request = ChatRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        user_query="query-sentinel",
        messages=[
            {"role": "user", "content": "ordinary semantic text"},
            {"role": "user", "content": ordinary_url},
            {"role": "user", "content": signed_url},
            {"role": "user", "content": media_url},
            {"role": "user", "content": relative_media_path},
            {"role": "user", "content": "Bearer bearer-secret"},
            {"role": "user", "content": "cookie=session-secret"},
            {"role": "user", "content": "/private/media/file.png"},
            {"role": "user", "content": embedded_references},
            {"role": "user", "content": credential_urls},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "probe_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "safe": {"type": "string"},
                            signed_url: {"default": signed_url},
                        },
                    },
                },
            }
        ],
    )
    outputs = native.project_llm_outputs(
        ChatResult(
            provider="provider-sentinel",
            model="model-sentinel",
            response_text=f"ordinary response {signed_url}",
        )
    )

    projected = native.project_llm_inputs(
        request,
        provider="provider-sentinel",
        model="model-sentinel",
    )
    serialized = repr((projected, outputs))

    assert "ordinary semantic text" in serialized
    assert "ordinary response" in serialized
    assert ordinary_url in serialized
    for forbidden in (
        "credential-secret",
        "signature-secret",
        "bearer-secret",
        "session-secret",
        "/private/media/file.png",
        "X-Amz-Credential",
        signed_url,
        media_url,
        relative_media_path,
        "artifact-secret",
        "file-secret",
        "data-secret",
        "embedded-media-secret",
        "windows-media-secret",
        "userinfo-secret",
        "query-secret",
        "amz-secret",
    ):
        assert forbidden not in serialized


def test_native_child_trace_records_safe_error_and_rethrows_business_error(
    monkeypatch,
) -> None:
    """Raw Provider/Tool exception text must not reach the remote child run."""

    native = _native()
    observed_errors: list[str] = []

    def fake_traceable(**_options: Any):
        def decorate(call):
            def wrapped():
                try:
                    return call()
                except Exception as exc:
                    observed_errors.append(str(exc))
                    raise

            return wrapped

        return decorate

    monkeypatch.setattr(native, "traceable", fake_traceable)
    token = native._NATIVE_TRACE_ACTIVE.set(True)
    business_error = RuntimeError("RAW-AUTHORIZATION-SECRET")
    try:
        with pytest.raises(RuntimeError) as captured:
            native.trace_governed_tool_call(
                lambda: (_ for _ in ()).throw(business_error),
                tool_name="probe_tool",
                safe_input={},
            )
    finally:
        native._NATIVE_TRACE_ACTIVE.reset(token)

    assert captured.value is business_error
    assert observed_errors == ["Governed tool call failed."]
