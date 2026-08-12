from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.assistant_interrupts import (
    AssistantApproveResume,
    AssistantInterruptRequest,
)
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.tool_operation_barrier import (
    OperationDigestConflict,
    SQLiteToolOperationStore,
    ToolOperationScopeRequired,
    ToolOperationRequest,
    normalized_tool_input_digest,
    stable_operation_scope_id,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.input_binding import RuntimeInputBinding
from assistant_agent.tools.models import ToolResult
from tests.core.support import (
    ProbeInput,
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


class _CountingWriteTool(ProbeTool):
    name = "write_probe_tool"
    category = "write"

    def __init__(self) -> None:
        self.invocations = 0

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        self.invocations += 1
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value, "private_body": "must-not-enter-ledger"},
            model_observation={"summary": "created-sentinel", "outcome": "success"},
            output_ref="artifact:sentinel",
        )


class _IdempotentWriteInput(BaseModel):
    value: str = Field(min_length=1)
    idempotency_key: str | None = None


class _IdempotentWriteTool(_CountingWriteTool):
    name = "idempotent_write_probe"
    input_schema = _IdempotentWriteInput
    output_schema = _IdempotentWriteInput
    runtime_input_bindings = (
        RuntimeInputBinding(field="idempotency_key", source="runtime_input"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.seen_keys: list[str | None] = []

    def _run(self, input: _IdempotentWriteInput, context: ToolContext) -> ToolResult:
        self.invocations += 1
        self.seen_keys.append(input.idempotency_key)
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"idempotency_key": input.idempotency_key},
            model_observation={"summary": "idempotent-created", "outcome": "success"},
        )


class _CrashingWriteTool(_CountingWriteTool):
    name = "crashing_write_probe"

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        self.invocations += 1
        raise RuntimeError("tool body crashed after an ambiguous boundary")


class _KnownFailureWriteTool(_CountingWriteTool):
    name = "known_failure_write_probe"

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        self.invocations += 1
        return ToolResult(
            tool_name=self.name,
            success=False,
            error="known_write_rejection",
            model_observation={"summary": "known-write-failed"},
        )


class _UnsafeLedgerProjectionTool(_CountingWriteTool):
    name = "unsafe_ledger_projection_probe"

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        self.invocations += 1
        return ToolResult(
            tool_name=self.name,
            success=True,
            model_observation={
                "summary": "credential=secret-value should-not-persist",
                "outcome": "success",
            },
            output_ref=(
                "https://storage.example.test/object?"
                "X-Amz-Credential=secret-value&X-Amz-Signature=signed-value"
            ),
        )


class _BackendCrash:
    def __init__(self) -> None:
        self.calls = 0
        self.reconcile_calls = 0

    def run(self, registry, tool_name, tool_input, context):
        self.calls += 1
        raise RuntimeError("backend response lost")

    def reconcile(self, *args, **kwargs):
        self.reconcile_calls += 1
        raise AssertionError("no reconciliation contract was declared")


class _InternalRetryBackend:
    def __init__(self) -> None:
        self.keys: list[str | None] = []

    def run(self, registry, tool_name, tool_input, context):
        payload = (
            tool_input.model_dump(mode="python")
            if isinstance(tool_input, BaseModel)
            else dict(tool_input)
        )
        self.keys.extend([payload.get("idempotency_key"), payload.get("idempotency_key")])
        return registry.run(tool_name, tool_input, context)


class _CommitCrashStore(SQLiteToolOperationStore):
    def commit_success(self, *args, **kwargs):
        raise RuntimeError("commit boundary lost")


def _state(*, run_id: str) -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="request-sentinel",
        ),
        run_id=run_id,
        agent_id="agent-sentinel",
    )


def _request(*, input_digest: str = "a" * 64) -> ToolOperationRequest:
    return ToolOperationRequest(
        thread_id="assistant:thread-sentinel",
        operation_scope_id="scope-sentinel",
        profile="standard",
        tool_name="write_probe_tool",
        input_digest=input_digest,
        business_idempotency_key="business-key-sentinel",
    )


def test_sqlite_reserve_is_atomic_and_only_one_concurrent_owner(tmp_path) -> None:
    store = SQLiteToolOperationStore(tmp_path / "operations.sqlite3")

    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(lambda _: store.reserve_and_mark_invoking(_request()), range(2)))

    owners = [item for item in reservations if item.disposition == "invoke"]
    blocked = [item for item in reservations if item.disposition == "in_progress"]
    assert len(owners) == 1
    assert len(blocked) == 1
    assert owners[0].owner_token
    assert blocked[0].owner_token is None
    assert store.load(owners[0].operation_key).status == "invoking"


def test_operation_scope_is_stable_for_logical_call_but_changes_across_turns() -> None:
    digest = normalized_tool_input_digest({"value": "sentinel"})
    arguments = {
        "thread_id": "assistant:thread-sentinel",
        "turn_origin_id": "turn-a",
        "assistant_iteration": 2,
        "call_ordinal": 1,
        "tool_name": "write_probe_tool",
        "normalized_input_digest": digest,
    }

    first = stable_operation_scope_id(**arguments)
    replay = stable_operation_scope_id(**arguments)
    next_turn = stable_operation_scope_id(
        **{**arguments, "turn_origin_id": "turn-b"}
    )

    assert first == replay
    assert first != next_turn


def test_same_operation_key_with_different_input_digest_fails_closed(tmp_path) -> None:
    store = SQLiteToolOperationStore(tmp_path / "operations.sqlite3")
    store.reserve_and_mark_invoking(_request())

    with pytest.raises(OperationDigestConflict):
        store.reserve_and_mark_invoking(_request(input_digest="b" * 64))


def test_rebuilt_store_marks_abandoned_invocation_unknown_and_never_replays_it(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    first = SQLiteToolOperationStore(path)
    reservation = first.reserve_and_mark_invoking(_request())
    assert reservation.disposition == "invoke"

    rebuilt = SQLiteToolOperationStore(path)
    assert rebuilt.load(reservation.operation_key).status == "invoking"
    rebuilt.recover_abandoned_invocations()
    record = rebuilt.load(reservation.operation_key)
    replay = rebuilt.reserve_and_mark_invoking(_request())

    assert record is not None
    assert record.status == "outcome_unknown"
    assert replay.disposition == "outcome_unknown"
    assert replay.owner_token is None


def test_second_live_store_does_not_invalidate_current_owner(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    first = SQLiteToolOperationStore(path)
    reservation = first.reserve_and_mark_invoking(_request())

    second = SQLiteToolOperationStore(path)
    first.commit_success(
        reservation.operation_key,
        owner_token=reservation.owner_token or "",
        result_summary="created-sentinel",
        output_ref=None,
        result_digest="c" * 64,
    )

    assert second.load(reservation.operation_key).status == "succeeded"


def test_committed_record_contains_only_safe_projection_not_result_body(tmp_path) -> None:
    store = SQLiteToolOperationStore(tmp_path / "operations.sqlite3")
    reservation = store.reserve_and_mark_invoking(_request())

    store.commit_success(
        reservation.operation_key,
        owner_token=reservation.owner_token or "",
        result_summary="created-sentinel",
        output_ref="artifact:sentinel",
        result_digest="c" * 64,
    )
    record = store.load(reservation.operation_key)

    assert record is not None
    assert record.status == "succeeded"
    assert record.result_summary == "created-sentinel"
    assert record.output_ref == "artifact:sentinel"
    assert record.result_digest == "c" * 64
    assert not hasattr(record, "result_body")


def test_read_bypasses_barrier_and_committed_write_without_readback_fails_closed(
    tmp_path,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = SQLiteToolOperationStore(path)
    read_tool = ProbeTool()
    write_tool = _CountingWriteTool()
    registry = sealed_registry(read_tool, write_tool)
    executor = ToolExecutor(registry=registry, operation_store=store)

    read_result = executor.run_tool(
        _state(run_id="run-read"),
        "step-read",
        read_tool.name,
        {"value": "read-sentinel"},
    )
    first = executor.run_tool(
        _state(run_id="run-write-first"),
        "step-write",
        write_tool.name,
        {"value": "write-sentinel"},
        operation_scope_id="scope-write-sentinel",
        operation_thread_id="assistant:thread-sentinel",
        operation_profile="standard",
    )
    replay = executor.run_tool(
        _state(run_id="run-write-replay"),
        "step-write",
        write_tool.name,
        {"value": "write-sentinel"},
        operation_scope_id="scope-write-sentinel",
        operation_thread_id="assistant:thread-sentinel",
        operation_profile="standard",
    )

    with sqlite3.connect(path) as connection:
        operation_count = connection.execute(
            "SELECT count(*) FROM tool_operations"
        ).fetchone()[0]
    assert read_result.success is True
    assert first.success is True
    assert replay.success is False
    assert replay.error == "tool_operation_outcome_unknown"
    assert replay.trace_summary == {
        "operation_key": replay.trace_summary["operation_key"],
        "operation_replayed": False,
        "outcome_unknown": True,
    }
    assert write_tool.invocations == 1
    assert operation_count == 1
    assert b"must-not-enter-ledger" not in path.read_bytes()


def test_write_executor_without_persisted_operation_scope_fails_before_backend(tmp_path) -> None:
    tool = _CountingWriteTool()
    executor = ToolExecutor(
        registry=sealed_registry(tool),
        operation_store=SQLiteToolOperationStore(tmp_path / "operations.sqlite3"),
    )

    with pytest.raises(ToolOperationScopeRequired):
        executor.run_tool(
            _state(run_id="resume-run-must-not-be-scope"),
            "step-write",
            tool.name,
            {"value": "write-sentinel"},
        )

    assert tool.invocations == 0


@pytest.mark.parametrize("provider_call_id", [None, "provider-call-sentinel"])
def test_graph_checkpoints_stable_operation_scope_before_tool_edge(
    tmp_path, provider_call_id: str | None
) -> None:
    tool = _CountingWriteTool()
    saver = InMemorySaver()
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
                            id=provider_call_id,
                            name=tool.name,
                            arguments={"value": "write-sentinel"},
                        )
                    ],
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        tool_operation_store=SQLiteToolOperationStore(tmp_path / "operations.sqlite3"),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - Graph API checkpoint TDD.
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="write request",
        ),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="turn-origin-sentinel",
    )
    try:
        checkpoint = runtime.assistant_graph_app.graph.invoke(
            prepared.initial_state,
            config=prepared.identity.runnable_config(),
            context=prepared.runtime_context,
            interrupt_after=["assistant"],
        )
    finally:
        runtime.close()

    pending = checkpoint["pending_tool_calls"][0]
    assert checkpoint["turn_origin_id"] == "turn-origin-sentinel"
    assert pending["operation_scope_id"].startswith("toolop:")
    assert len(pending["operation_scope_id"]) == 71
    assert pending["provider_call_id"] == (
        provider_call_id or f"local:{pending['operation_scope_id']}"
    )
    assert tool.invocations == 0


def test_backend_crash_becomes_unknown_and_is_not_automatically_replayed(tmp_path) -> None:
    store = SQLiteToolOperationStore(tmp_path / "operations.sqlite3")
    tool = _CountingWriteTool()
    backend = _BackendCrash()
    executor = ToolExecutor(
        registry=sealed_registry(tool),
        operation_store=store,
        execution_backend=backend,
    )
    operation = {
        "operation_scope_id": "scope-crash-sentinel",
        "operation_thread_id": "assistant:thread-sentinel",
        "operation_profile": "standard",
    }

    first = executor.run_tool(
        _state(run_id="run-crash-first"),
        "step-crash",
        tool.name,
        {"value": "write-sentinel"},
        **operation,
    )
    replay = executor.run_tool(
        _state(run_id="run-crash-replay"),
        "step-crash",
        tool.name,
        {"value": "write-sentinel"},
        **operation,
    )

    assert first.success is False
    assert first.error == "tool_operation_outcome_unknown"
    assert replay.error == "tool_operation_outcome_unknown"
    assert backend.calls == 1
    assert backend.reconcile_calls == 0
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM tool_operations"
        ).fetchone()[0] == "outcome_unknown"


def test_tool_body_crash_is_unknown_even_through_registry_backend(tmp_path) -> None:
    store = SQLiteToolOperationStore(tmp_path / "operations.sqlite3")
    tool = _CrashingWriteTool()
    executor = ToolExecutor(registry=sealed_registry(tool), operation_store=store)
    operation = {
        "operation_scope_id": "scope-tool-crash",
        "operation_thread_id": "assistant:thread-sentinel",
        "operation_profile": "standard",
    }

    first = executor.run_tool(
        _state(run_id="run-tool-crash"),
        "step-tool-crash",
        tool.name,
        {"value": "write-sentinel"},
        **operation,
    )
    replay = executor.run_tool(
        _state(run_id="run-tool-crash-replay"),
        "step-tool-crash",
        tool.name,
        {"value": "write-sentinel"},
        **operation,
    )

    assert first.error == "tool_operation_outcome_unknown"
    assert replay.error == "tool_operation_outcome_unknown"
    assert tool.invocations == 1


def test_commit_crash_becomes_unknown_after_single_backend_invocation(tmp_path) -> None:
    store = _CommitCrashStore(tmp_path / "operations.sqlite3")
    tool = _CountingWriteTool()
    executor = ToolExecutor(registry=sealed_registry(tool), operation_store=store)
    operation = {
        "operation_scope_id": "scope-commit-crash",
        "operation_thread_id": "assistant:thread-sentinel",
        "operation_profile": "standard",
    }

    result = executor.run_tool(
        _state(run_id="run-commit-crash"),
        "step-commit-crash",
        tool.name,
        {"value": "write-sentinel"},
        **operation,
    )
    replay = executor.run_tool(
        _state(run_id="run-commit-crash-replay"),
        "step-commit-crash",
        tool.name,
        {"value": "write-sentinel"},
        **operation,
    )

    assert result.error == "tool_operation_outcome_unknown"
    assert replay.error == "tool_operation_outcome_unknown"
    assert tool.invocations == 1


def test_known_failure_is_committed_but_not_fabricated_on_replay(tmp_path) -> None:
    store = SQLiteToolOperationStore(tmp_path / "operations.sqlite3")
    tool = _KnownFailureWriteTool()
    executor = ToolExecutor(registry=sealed_registry(tool), operation_store=store)
    operation = {
        "operation_scope_id": "scope-known-failure",
        "operation_thread_id": "assistant:thread-sentinel",
        "operation_profile": "standard",
    }

    first = executor.run_tool(
        _state(run_id="run-known-failure"),
        "step-known-failure",
        tool.name,
        {"value": "write-sentinel"},
        **operation,
    )
    replay = executor.run_tool(
        _state(run_id="run-known-failure-replay"),
        "step-known-failure",
        tool.name,
        {"value": "write-sentinel"},
        **operation,
    )

    assert first.success is False
    assert replay.success is False
    assert replay.error == "tool_operation_outcome_unknown"
    assert replay.trace_summary["operation_replayed"] is False
    assert tool.invocations == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM tool_operations"
        ).fetchone()[0] == "failed"


def test_ledger_rejects_tool_summary_credentials_and_signed_output_refs(tmp_path) -> None:
    path = tmp_path / "operations.sqlite3"
    store = SQLiteToolOperationStore(path)
    tool = _UnsafeLedgerProjectionTool()

    result = ToolExecutor(
        registry=sealed_registry(tool), operation_store=store
    ).run_tool(
        _state(run_id="run-unsafe-projection"),
        "step-unsafe-projection",
        tool.name,
        {"value": "write-sentinel"},
        operation_scope_id="scope-unsafe-projection",
        operation_thread_id="assistant:thread-sentinel",
        operation_profile="standard",
    )
    with sqlite3.connect(path) as connection:
        summary, output_ref = connection.execute(
            "SELECT result_summary, output_ref FROM tool_operations"
        ).fetchone()

    assert result.success is True
    assert summary == "Tool operation succeeded."
    assert output_ref is None
    raw = path.read_bytes()
    assert b"secret-value" not in raw
    assert b"signed-value" not in raw


def test_backend_internal_retries_share_runtime_bound_business_idempotency_key(
    tmp_path,
) -> None:
    store = SQLiteToolOperationStore(tmp_path / "operations.sqlite3")
    tool = _IdempotentWriteTool()
    backend = _InternalRetryBackend()
    executor = ToolExecutor(
        registry=sealed_registry(tool),
        operation_store=store,
        execution_backend=backend,
    )

    result = executor.run_tool(
        _state(run_id="run-idempotent"),
        "step-idempotent",
        tool.name,
        {"value": "write-sentinel"},
        operation_scope_id="scope-idempotent",
        operation_thread_id="assistant:thread-sentinel",
        operation_profile="standard",
    )
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT operation_key, business_idempotency_key FROM tool_operations"
        ).fetchone()

    assert result.success is True
    assert len(backend.keys) == 2
    assert backend.keys[0] == backend.keys[1]
    assert backend.keys[0] == row[0] == row[1]
    assert result.data == {"idempotency_key": row[0]}


def test_interrupt_rebuild_resume_matches_uninterrupted_write_trajectory(tmp_path) -> None:
    request = UserRequest(
        user_id="user-equivalence",
        session_id="session-equivalence",
        text="write request",
    )
    tool_call = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="tool_calls",
        tool_calls=[
            NativeToolCall(
                id="provider-equivalence",
                name=_IdempotentWriteTool.name,
                arguments={"value": "equivalent-write"},
            )
        ],
    )
    final_answer = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="stop",
        response_text="equivalent-final",
    )

    baseline_tool = _IdempotentWriteTool()
    baseline_adapter = ScriptedChatAdapter([tool_call, final_answer])
    baseline = AgentGraphRuntime(
        registry=sealed_registry(baseline_tool),
        config=offline_config(),
        chat_adapter=baseline_adapter,
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        tool_operation_store=SQLiteToolOperationStore(
            tmp_path / "baseline-operations.sqlite3"
        ),
    )
    baseline_prepared = baseline._prepare_graph_run(  # noqa: SLF001
        request.model_copy(deep=True),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="turn-equivalence",
    )
    try:
        baseline_result = asyncio.run(
            baseline.assistant_graph_app.arun(
                baseline_prepared.initial_state,
                identity=baseline_prepared.identity,
                context=baseline_prepared.runtime_context,
            )
        )
    finally:
        baseline.close()

    saver = InMemorySaver()
    resumed_path = tmp_path / "resumed-operations.sqlite3"
    first_tool = _IdempotentWriteTool()
    first_adapter = ScriptedChatAdapter([tool_call])
    first = AgentGraphRuntime(
        registry=sealed_registry(first_tool),
        config=offline_config(),
        chat_adapter=first_adapter,
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        tool_operation_store=SQLiteToolOperationStore(resumed_path),
    )
    interrupted_prepared = first._prepare_graph_run(  # noqa: SLF001
        request.model_copy(deep=True),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="turn-equivalence",
        interrupt_request=AssistantInterruptRequest(
            kind="approval",
            prompt="Approve the pending write?",
            action_ref="provider-equivalence",
            allowed_resume_kinds=("approve", "reject"),
        ),
    )
    try:
        interrupted = asyncio.run(
            first.assistant_graph_app.arun(
                interrupted_prepared.initial_state,
                identity=interrupted_prepared.identity,
                context=interrupted_prepared.runtime_context,
            )
        )
    finally:
        first.close()

    rebuilt_tool = _IdempotentWriteTool()
    rebuilt_adapter = ScriptedChatAdapter([final_answer])
    rebuilt = AgentGraphRuntime(
        registry=sealed_registry(rebuilt_tool),
        config=offline_config(),
        chat_adapter=rebuilt_adapter,
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        tool_operation_store=SQLiteToolOperationStore(resumed_path),
    )
    resumed_prepared = rebuilt._prepare_graph_run(  # noqa: SLF001
        request.model_copy(deep=True),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="resume-equivalence",
    )
    try:
        resumed_result = asyncio.run(
            rebuilt.assistant_graph_app.aresume(
                identity=resumed_prepared.identity,
                context=resumed_prepared.runtime_context,
                resume=AssistantApproveResume(action_ref="provider-equivalence"),
            )
        )
    finally:
        rebuilt.close()

    assert interrupted.status == "interrupted"
    assert first_tool.invocations == 0
    assert baseline_tool.invocations == rebuilt_tool.invocations == 1
    assert baseline_tool.seen_keys == rebuilt_tool.seen_keys
    assert baseline_adapter.requests[0].model_dump(mode="json") == (
        first_adapter.requests[0].model_dump(mode="json")
    )
    assert baseline_adapter.requests[1].model_dump(mode="json") == (
        rebuilt_adapter.requests[0].model_dump(mode="json")
    )
    baseline_state = baseline_result.final_state
    resumed_state = resumed_result.final_state
    assert baseline_state["turn_origin_id"] == resumed_state["turn_origin_id"]
    assert baseline_state["run"]["tool_calls"][0]["tool_name"] == (
        resumed_state["run"]["tool_calls"][0]["tool_name"]
    )
    assert baseline_state["run"]["tool_calls"][0]["arguments"] == (
        resumed_state["run"]["tool_calls"][0]["arguments"]
    )
    assert baseline_state["run"]["tool_results"] == resumed_state["run"]["tool_results"]
    assert baseline_state["tool_observations"] == resumed_state["tool_observations"]
    assert baseline_state["final_response"] == resumed_state["final_response"]


def test_crash_after_commit_before_graph_checkpoint_never_fabricates_or_reinvokes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant_agent.runtime import graph_runtime

    request = UserRequest(
        user_id="user-post-commit",
        session_id="session-post-commit",
        text="write request",
    )
    saver = InMemorySaver()
    path = tmp_path / "operations.sqlite3"
    first_tool = _CountingWriteTool()
    first = AgentGraphRuntime(
        registry=sealed_registry(first_tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="provider-post-commit",
                            name=first_tool.name,
                            arguments={"value": "write-sentinel"},
                        )
                    ],
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        tool_operation_store=SQLiteToolOperationStore(path),
    )
    prepared = first._prepare_graph_run(  # noqa: SLF001
        request.model_copy(deep=True),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="turn-post-commit",
    )
    original_project = graph_runtime.assistant_turn_state_from_loop_state
    crashed = False

    def crash_before_checkpoint(state, *, profile="standard"):
        nonlocal crashed
        projected = original_project(state, profile=profile)
        if projected["run"]["tool_results"] and not crashed:
            crashed = True
            raise RuntimeError("process lost before graph checkpoint")
        return projected

    monkeypatch.setattr(
        graph_runtime,
        "assistant_turn_state_from_loop_state",
        crash_before_checkpoint,
    )
    try:
        with pytest.raises(RuntimeError, match="before graph checkpoint"):
            first.assistant_graph_app.graph.invoke(
                prepared.initial_state,
                config=prepared.identity.runnable_config(),
                context=prepared.runtime_context,
            )
    finally:
        first.close()
        monkeypatch.setattr(
            graph_runtime,
            "assistant_turn_state_from_loop_state",
            original_project,
        )

    rebuilt_tool = _CountingWriteTool()
    rebuilt = AgentGraphRuntime(
        registry=sealed_registry(rebuilt_tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="unknown-final",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
        tool_operation_store=SQLiteToolOperationStore(path),
    )
    resumed = rebuilt._prepare_graph_run(  # noqa: SLF001
        request.model_copy(deep=True),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="turn-post-commit",
    )
    resumed.state.trace_id = prepared.state.trace_id
    try:
        final = rebuilt.assistant_graph_app.graph.invoke(
            None,
            config=prepared.identity.runnable_config(),
            context=resumed.runtime_context,
        )
    finally:
        rebuilt.close()

    assert first_tool.invocations == 1
    assert rebuilt_tool.invocations == 0
    assert final["tool_observations"][0]["status"] == "failed"
    assert final["tool_observations"][0]["error"]["code"] == (
        "tool_operation_outcome_unknown"
    )
