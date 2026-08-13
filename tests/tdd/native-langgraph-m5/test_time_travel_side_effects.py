from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import wraps

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.runtime.assistant_graph_app import GraphExecutionError
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_time_travel import (
    GraphForkRequest,
    GraphReplayRequest,
    TimeTravelEffectPolicy,
    fork_patch_preserves_pending_effects,
)
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.tool_operation_barrier import SQLiteToolOperationStore
from assistant_agent.runtime.tool_operation_barrier import (
    ToolOperationRequest,
    normalized_tool_input_digest,
    stable_assistant_thread_id,
    stable_operation_scope_id,
    tool_contract_digest,
    tool_execution_contract_digest,
)
from assistant_agent.tools.base import ToolContext
from tests.core.support import (
    ProbeInput,
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


def _async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class _AmbiguousWriteTool(ProbeTool):
    name = "time_travel_ambiguous_write"
    category = "write"

    def __init__(self) -> None:
        self.invocations = 0

    def _run(self, input: ProbeInput, context: ToolContext):
        self.invocations += 1
        raise RuntimeError("backend response was lost after the write boundary")


class _ReadTool(_AmbiguousWriteTool):
    name = "time_travel_read"
    category = "read"


class _SuccessfulWriteTool(_AmbiguousWriteTool):
    def _run(self, input: ProbeInput, context: ToolContext):
        self.invocations += 1
        from assistant_agent.tools.models import ToolResult

        return ToolResult(tool_name=self.name, success=True, data={"ok": True})


class _FailedWriteTool(_AmbiguousWriteTool):
    def _run(self, input: ProbeInput, context: ToolContext):
        self.invocations += 1
        from assistant_agent.tools.models import ToolResult

        return ToolResult(tool_name=self.name, success=False, error="known failure")


def _request(text: str = "time travel request") -> UserRequest:
    return UserRequest(
        user_id="user-time-travel-effects",
        session_id="session-time-travel-effects",
        text=text,
    )


def _prepare(runtime: AgentGraphRuntime, *, run_id: str, text: str = "time travel request"):
    return runtime._prepare_graph_run(  # noqa: SLF001 - native App boundary TDD.
        _request(text),
        event_sink=None,
        cancel_token=None,
        pre_terminal_state_hook=None,
        run_id=run_id,
    )


async def _selector_before_write(runtime: AgentGraphRuntime, identity):
    app = runtime.assistant_graph_app
    for summary in await app.alist_history(identity, limit=100):
        selector = GraphReplayRequest(
            selector={"history_ref": summary.history_ref}
        ).selector
        snapshot = await app._resolve_history_snapshot(identity, selector)  # noqa: SLF001
        if snapshot.values["continuation"] == "execute_tool":
            return selector
    raise AssertionError("No replay-safe checkpoint before the write was produced")


def test_assistant_only_replay_ignores_unrelated_unknown_operation(tmp_path) -> None:
    """Scanning a thread-wide ledger would incorrectly block this checkpoint."""

    tool = _AmbiguousWriteTool()
    registry = sealed_registry(tool)
    store = SQLiteToolOperationStore(tmp_path / "unrelated.sqlite3")
    reservation = store.reserve_and_mark_invoking(
        ToolOperationRequest(
            thread_id="assistant:unrelated-thread",
            operation_scope_id="unrelated-scope",
            profile="standard",
            tool_name=tool.name,
            input_digest="a" * 64,
        )
    )
    store.mark_outcome_unknown(
        reservation.operation_key,
        owner_token=reservation.owner_token or "",
    )

    decision = TimeTravelEffectPolicy(
        registry=registry,
        operation_store=store,
    ).classify(
        {"continuation": "assistant"},
        ("prepare_invocation",),
    )

    assert decision == "safe"


def test_missing_ledger_row_requires_a_stable_checkpoint_scope(tmp_path) -> None:
    """A never-started write is replayable only with its original stable scope."""

    tool = _AmbiguousWriteTool()
    registry = sealed_registry(tool)
    store = SQLiteToolOperationStore(tmp_path / "missing.sqlite3")
    thread_id = stable_assistant_thread_id(
        agent_id="agent-time-travel-effects",
        user_id="user-time-travel-effects",
        session_id="session-time-travel-effects",
    )
    base_state = {
        "continuation": "execute_tool",
        "profile": "standard",
        "turn_origin_id": "turn-origin",
        "assistant_iterations": 1,
        "request": {
            "user_id": "user-time-travel-effects",
            "session_id": "session-time-travel-effects",
        },
        "run": {"agent_id": "agent-time-travel-effects"},
        "catalog": {"registry_generation": registry.generation},
    }
    from assistant_agent.runtime.state import AgentState

    runtime_state = AgentState.from_request(
        _request(),
        run_id="run-policy",
        agent_id="agent-time-travel-effects",
    )
    policy = TimeTravelEffectPolicy(
        registry=registry,
        operation_store=store,
        runtime_state=runtime_state,
    )
    valid_scope = stable_operation_scope_id(
        thread_id=thread_id,
        turn_origin_id="turn-origin",
        assistant_iteration=1,
        call_ordinal=0,
        tool_name=tool.name,
        normalized_input_digest=normalized_tool_input_digest(
            {"value": "sentinel"}
        ),
    )

    valid = policy.classify(
        {
            **base_state,
            "pending_tool_calls": [
                {
                    "tool_name": tool.name,
                    "effect_category": "write",
                    "tool_contract_digest": tool_contract_digest(
                        registry.get_spec(tool.name)
                    ),
                    "execution_contract_digest": tool_execution_contract_digest(
                        tool,
                        registry.get_spec(tool.name),
                    ),
                    "bound_input_digest": normalized_tool_input_digest(
                        {"value": "sentinel"}
                    ),
                    "operation_scope_id": valid_scope,
                    "arguments": [
                        {"name": "value", "value_json": '"sentinel"'}
                    ],
                }
            ],
        },
        ("prepare_invocation",),
    )
    lost_scope = policy.classify(
        {
            **base_state,
            "pending_tool_calls": [
                {
                    "tool_name": tool.name,
                    "effect_category": "write",
                    "tool_contract_digest": tool_contract_digest(
                        registry.get_spec(tool.name)
                    ),
                    "execution_contract_digest": tool_execution_contract_digest(
                        tool,
                        registry.get_spec(tool.name),
                    ),
                    "bound_input_digest": normalized_tool_input_digest(
                        {"value": "sentinel"}
                    ),
                    "operation_scope_id": "legacy:lost-scope",
                    "arguments": [
                        {"name": "value", "value_json": '"sentinel"'}
                    ],
                }
            ],
        },
        ("prepare_invocation",),
    )

    assert valid == "barrier_required"
    assert lost_scope == "forbidden"


def test_shape_valid_scope_for_another_pending_input_is_forbidden(tmp_path) -> None:
    """A scope with valid syntax must still belong to the selected pending call."""

    tool = _AmbiguousWriteTool()
    registry = sealed_registry(tool)
    store = SQLiteToolOperationStore(tmp_path / "tampered-scope.sqlite3")
    thread_id = stable_assistant_thread_id(
        agent_id="agent-time-travel-effects",
        user_id="user-time-travel-effects",
        session_id="session-time-travel-effects",
    )
    wrong_scope = stable_operation_scope_id(
        thread_id=thread_id,
        turn_origin_id="turn-origin",
        assistant_iteration=1,
        call_ordinal=0,
        tool_name=tool.name,
        normalized_input_digest="b" * 64,
    )

    decision = TimeTravelEffectPolicy(
        registry=registry,
        operation_store=store,
    ).classify(
        {
            "continuation": "execute_tool",
            "profile": "standard",
            "turn_origin_id": "turn-origin",
            "assistant_iterations": 1,
            "request": {
                "user_id": "user-time-travel-effects",
                "session_id": "session-time-travel-effects",
            },
            "run": {"agent_id": "agent-time-travel-effects"},
            "catalog": {"registry_generation": registry.generation},
            "pending_tool_calls": [
                {
                    "tool_name": tool.name,
                    "effect_category": "write",
                    "tool_contract_digest": tool_contract_digest(
                        registry.get_spec(tool.name)
                    ),
                    "operation_scope_id": wrong_scope,
                    "arguments": [
                        {"name": "value", "value_json": '"sentinel"'}
                    ],
                }
            ],
        },
        ("prepare_invocation",),
    )

    assert decision == "forbidden"


def test_read_contract_with_a_historical_operation_row_is_forbidden(tmp_path) -> None:
    """A category downgrade cannot hide a historical side-effect ledger row."""

    tool = _ReadTool()
    registry = sealed_registry(tool)
    historical_registry = sealed_registry(_AmbiguousWriteTool())
    store = SQLiteToolOperationStore(tmp_path / "category-drift.sqlite3")
    thread_id = stable_assistant_thread_id(
        agent_id="agent-time-travel-effects",
        user_id="user-time-travel-effects",
        session_id="session-time-travel-effects",
    )
    arguments = {"value": "sentinel"}
    scope = stable_operation_scope_id(
        thread_id=thread_id,
        turn_origin_id="turn-origin",
        assistant_iteration=1,
        call_ordinal=0,
        tool_name=tool.name,
        normalized_input_digest=normalized_tool_input_digest(arguments),
    )
    store.reserve_and_mark_invoking(
        ToolOperationRequest(
            thread_id=thread_id,
            operation_scope_id=scope,
            profile="standard",
            tool_name=tool.name,
            input_digest=normalized_tool_input_digest(arguments),
        )
    )

    decision = TimeTravelEffectPolicy(
        registry=registry,
        operation_store=store,
    ).classify(
        {
            "continuation": "execute_tool",
            "profile": "standard",
            "turn_origin_id": "turn-origin",
            "assistant_iterations": 1,
            "request": {
                "user_id": "user-time-travel-effects",
                "session_id": "session-time-travel-effects",
            },
            "run": {"agent_id": "agent-time-travel-effects"},
            "catalog": {"registry_generation": registry.generation},
            "pending_tool_calls": [
                {
                    "tool_name": tool.name,
                    "effect_category": "write",
                    "tool_contract_digest": tool_contract_digest(
                        historical_registry.get_spec("time_travel_ambiguous_write")
                    ),
                    "operation_scope_id": scope,
                    "arguments": [
                        {"name": "value", "value_json": '"sentinel"'}
                    ],
                }
            ],
        },
        ("prepare_invocation",),
    )

    assert decision == "forbidden"


@pytest.mark.parametrize(
    ("tool_factory", "expected_status"),
    [(_SuccessfulWriteTool, "succeeded"), (_FailedWriteTool, "failed")],
)
def test_terminal_operation_rows_never_repeat_backend(
    tmp_path,
    tool_factory,
    expected_status,
) -> None:
    """Both terminal ledger states short-circuit the same logical operation."""

    from assistant_agent.runtime.state import AgentState
    from assistant_agent.runtime.tool_executor import ToolExecutor

    tool = tool_factory()
    executor = ToolExecutor(
        registry=sealed_registry(tool),
        operation_store=SQLiteToolOperationStore(tmp_path / f"{expected_status}.sqlite3"),
    )

    def state(run_id: str) -> AgentState:
        return AgentState.from_request(
            _request(),
            run_id=run_id,
            agent_id="agent-time-travel-effects",
        )

    operation = {
        "operation_scope_id": "toolop:" + "a" * 64,
        "operation_thread_id": "assistant:terminal-row",
        "operation_profile": "standard",
    }
    executor.run_tool(
        state("run-terminal-first"),
        "assistant_loop_1",
        tool.name,
        {"value": "sentinel"},
        **operation,
    )
    replayed = executor.run_tool(
        state("run-terminal-replay"),
        "assistant_loop_1",
        tool.name,
        {"value": "sentinel"},
        **operation,
    )

    assert replayed.error == "tool_operation_outcome_unknown"
    assert tool.invocations == 1


@_async_test
async def test_replay_and_fork_stop_before_unknown_write_outcome(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambiguous write must block before Provider, Tool, or fork mutation."""

    tool = _AmbiguousWriteTool()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-ambiguous-write",
                        name=tool.name,
                        arguments={"value": "sentinel"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="original terminal answer",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        tool_operation_store=SQLiteToolOperationStore(tmp_path / "operations.sqlite3"),
    )
    original = _prepare(runtime, run_id="run-effects-origin")
    update_calls = 0
    graph_type = type(runtime.assistant_graph_app.graph)
    original_update = graph_type.aupdate_state

    async def spy_update(graph, *args, **kwargs):
        nonlocal update_calls
        update_calls += 1
        return await original_update(graph, *args, **kwargs)

    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selector = await _selector_before_write(runtime, original.identity)
        baseline_provider_calls = len(adapter.requests)

        replay = _prepare(runtime, run_id="run-effects-replay")
        replay.state.trace_id = first.final_state["run"]["trace_id"]
        fork = _prepare(runtime, run_id="run-effects-fork")
        fork.state.trace_id = first.final_state["run"]["trace_id"]
        memory_calls = {"prepare_context": 0, "ingest_turn": 0}

        def unexpected_prepare(*args, **kwargs):
            memory_calls["prepare_context"] += 1
            raise AssertionError("replay/fork must not prepare new-turn memory")

        def unexpected_ingest(*args, **kwargs):
            memory_calls["ingest_turn"] += 1
            raise AssertionError("replay/fork must not ingest a derived turn")

        monkeypatch.setattr(
            runtime.long_term_memory_service,
            "prepare_context",
            unexpected_prepare,
        )
        monkeypatch.setattr(
            runtime.long_term_memory_service,
            "enqueue_completed_turn",
            unexpected_ingest,
        )
        with pytest.raises(GraphExecutionError) as replay_error:
            await runtime.assistant_graph_app.areplay(
                identity=replay.identity,
                context=replace(replay.runtime_context, invocation_kind="replay"),
                request=GraphReplayRequest(selector=selector),
            )

        monkeypatch.setattr(graph_type, "aupdate_state", spy_update)
        with pytest.raises(GraphExecutionError) as fork_error:
            await runtime.assistant_graph_app.afork(
                identity=fork.identity,
                context=fork.runtime_context,
                request=GraphForkRequest(
                    selector=selector,
                    patch={},
                ),
            )
    finally:
        runtime.close()

    assert replay_error.value.code == "graph_time_travel_effect_outcome_unknown"
    assert fork_error.value.code == "graph_time_travel_effect_outcome_unknown"
    assert tool.invocations == 1
    assert len(adapter.requests) == baseline_provider_calls
    assert update_calls == 0
    assert memory_calls == {"prepare_context": 0, "ingest_turn": 0}


@_async_test
async def test_fork_rejects_request_patch_while_a_write_is_pending(tmp_path) -> None:
    """Patched request bindings must not change a checkpointed operation input."""

    tool = _AmbiguousWriteTool()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-pending-write",
                        name=tool.name,
                        arguments={"value": "sentinel"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="terminal answer",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        tool_operation_store=SQLiteToolOperationStore(tmp_path / "fork-patch.sqlite3"),
    )
    original = _prepare(runtime, run_id="run-patch-origin")
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selector = await _selector_before_write(runtime, original.identity)
        fork = _prepare(runtime, run_id="run-patch-fork", text="changed request")
        fork.state.trace_id = first.final_state["run"]["trace_id"]
        with pytest.raises(GraphExecutionError) as captured:
            await runtime.assistant_graph_app.afork(
                identity=fork.identity,
                context=fork.runtime_context,
                request=GraphForkRequest(
                    selector=selector,
                    patch={"request_text": "changed request"},
                ),
            )
    finally:
        runtime.close()

    assert captured.value.code == "graph_time_travel_effect_forbidden"


def test_fork_rejects_patch_when_approval_checkpoint_keeps_pending_write() -> None:
    """Await-input must not bypass the pending-effect fork patch barrier."""

    historical = {
            "continuation": "await_input",
            "pending_tool_calls": [{"tool_name": "time_travel_ambiguous_write"}],
    }
    request = GraphForkRequest(
        selector={"history_ref": "ghr_" + "a" * 32},
        patch={"request_text": "changed while awaiting approval"},
    )

    assert fork_patch_preserves_pending_effects(historical, request.patch) is False
