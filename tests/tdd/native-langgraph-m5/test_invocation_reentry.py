from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.runtime.assistant_graph_state import (
    AssistantStateCompatibilityError,
    assistant_turn_state_from_agent_state,
    reenter_assistant_invocation,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.assistant_graph_app import (
    AssistantTurnGraphApp,
    GraphExecutionError,
    GraphExecutionIdentity,
)
from assistant_agent.runtime.assistant_interrupts import AssistantApproveResume
from assistant_agent.runtime.graph_invocation_claims import (
    GraphInvocationClaimCapacityExceeded,
    GraphInvocationClaimConflict,
    InMemoryGraphInvocationClaimStore,
    graph_invocation_owner_digest,
)
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.run_history import RunHistoryStore
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from tests.core.support import (
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


CLAIM = {
    "owner_digest": "owner-digest",
    "thread_id": "thread-claim",
    "run_id": "run-claim",
    "invocation_kind": "replay",
}


def _request(*, text: str = "run the probe") -> UserRequest:
    return UserRequest(
        user_id="user-reentry",
        session_id="session-reentry",
        text=text,
    )


def _runtime_state(*, run_id: str, trace_id: str = "trace-origin") -> AgentState:
    request = _request()
    request.metadata["_trusted_graph_profile"] = "standard"
    return AgentState.from_request(
        request,
        run_id=run_id,
        trace_id=trace_id,
    )


def test_claim_store_distinguishes_same_invocation_from_competing_branch() -> None:
    """Replacing the token value with a run-id set would admit competing branches."""

    store = InMemoryGraphInvocationClaimStore()

    assert store.claim(**CLAIM, invocation_token="token-a") == "claimed"
    assert store.claim(**CLAIM, invocation_token="token-a") == "same_invocation"
    with pytest.raises(GraphInvocationClaimConflict) as captured:
        store.claim(**CLAIM, invocation_token="token-b")

    assert captured.value.code == "graph_invocation_run_id_reused"


def test_claim_store_allows_only_one_atomic_native_start() -> None:
    """Two same-token callers admitted before native start must not both execute."""

    store = InMemoryGraphInvocationClaimStore()
    assert store.claim(**CLAIM, invocation_token="token-a") == "claimed"
    assert store.claim(**CLAIM, invocation_token="token-a") == "same_invocation"

    store.begin_native(
        owner_digest=CLAIM["owner_digest"],
        thread_id=CLAIM["thread_id"],
        run_id=CLAIM["run_id"],
        invocation_token="token-a",
    )
    with pytest.raises(GraphInvocationClaimConflict):
        store.begin_native(
            owner_digest=CLAIM["owner_digest"],
            thread_id=CLAIM["thread_id"],
            run_id=CLAIM["run_id"],
            invocation_token="token-a",
        )
    with pytest.raises(GraphInvocationClaimConflict):
        store.claim(**CLAIM, invocation_token="token-a")


def test_claim_store_fails_closed_at_capacity_until_owner_deletes_thread() -> None:
    """Silently evicting an old claim would admit reuse of a retained checkpoint."""

    store = InMemoryGraphInvocationClaimStore(max_entries=1)
    assert store.claim(**CLAIM, invocation_token="token-a") == "claimed"

    with pytest.raises(GraphInvocationClaimCapacityExceeded):
        store.claim(
            **{**CLAIM, "thread_id": "thread-other", "run_id": "run-other"},
            invocation_token="token-b",
        )

    assert store.delete_thread(
        owner_digest=CLAIM["owner_digest"],
        thread_id=CLAIM["thread_id"],
    ) == 1
    assert store.claim(**CLAIM, invocation_token="token-b") == "claimed"
    assert store.delete_thread(
        owner_digest=CLAIM["owner_digest"],
        thread_id=CLAIM["thread_id"],
    ) == 1
    assert (
        store.claim(
            **{**CLAIM, "thread_id": "thread-other", "run_id": "run-other"},
            invocation_token="token-b",
        )
        == "claimed"
    )


def test_no_saver_retains_claim_until_retention_owner_deletes_thread() -> None:
    """Terminal execution must not release a claim independently of retention."""

    store = InMemoryGraphInvocationClaimStore(max_entries=1)
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="sync-first",
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="sync-second",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        graph_invocation_claim_store=store,
    )

    try:
        first = runtime.run_state(_request(), run_id="run-sync-first")
        with pytest.raises(GraphExecutionError) as captured:
            runtime.run_state(_request(), run_id="run-sync-second")
        assert captured.value.code == "graph_invocation_claim_capacity_exceeded"

        identity = runtime._prepare_graph_run(  # noqa: SLF001 - retention TDD.
            _request(),
            event_sink=None,
            cancel_token=None,
            trace_context=None,
            export_trace_context=None,
            pre_terminal_state_hook=None,
            run_id="run-retention-owner",
        ).identity
        assert store.delete_thread(
            owner_digest=graph_invocation_owner_digest(
                agent_id=identity.agent_id,
                user_id="user-reentry",
                session_id="session-reentry",
            ),
            thread_id=identity.thread_id,
        ) == 1
        second = runtime.run_state(_request(), run_id="run-sync-second")
    finally:
        runtime.close()

    assert first.status == "completed"
    assert first.response is not None
    assert first.response.message == "sync-first"
    assert second.status == "completed"
    assert second.response is not None
    assert second.response.message == "sync-second"


@pytest.mark.parametrize("entrypoint", ["sync", "async", "astream"])
def test_no_saver_rejects_different_token_for_same_run_without_provider_reexecution(
    entrypoint: str,
) -> None:
    """Removing terminal retention would execute one run twice without history."""

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text=f"{entrypoint}-once",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )

    async def exercise_async() -> None:
        if entrypoint == "async":
            await runtime.arun_state(_request(), run_id="run-no-saver-reuse")
            with pytest.raises(GraphExecutionError) as captured:
                await runtime.arun_state(_request(), run_id="run-no-saver-reuse")
        else:
            prepared = runtime._prepare_graph_run(  # noqa: SLF001 - App boundary TDD.
                _request(),
                event_sink=None,
                cancel_token=None,
                trace_context=None,
                export_trace_context=None,
                pre_terminal_state_hook=None,
                run_id="run-no-saver-reuse",
            )
            async for _part in runtime.assistant_graph_app.astream(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            ):
                pass
            with pytest.raises(GraphExecutionError) as captured:
                async for _part in runtime.assistant_graph_app.astream(
                    prepared.initial_state,
                    identity=prepared.identity,
                    context=replace(
                        prepared.runtime_context,
                        invocation_token="competing-astream-token",
                    ),
                ):
                    pass
        assert captured.value.code == "graph_invocation_run_id_reused"

    try:
        if entrypoint == "sync":
            runtime.run_state(_request(), run_id="run-no-saver-reuse")
            with pytest.raises(GraphExecutionError) as captured:
                runtime.run_state(_request(), run_id="run-no-saver-reuse")
            assert captured.value.code == "graph_invocation_run_id_reused"
        else:
            asyncio.run(exercise_async())
    finally:
        runtime.close()

    assert len(adapter.requests) == 1


def test_sync_completed_checkpoint_reuse_maps_conflict_and_closes_lifecycle(
    tmp_path,
) -> None:
    """A sync completed no-op must not bypass claim or leave Runtime started."""

    history = RunHistoryStore(tmp_path / "sync-conflict.jsonl")
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="sync-checkpoint-first",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        run_history=history,
    )

    try:
        first = runtime.run_state(_request(), run_id="run-sync-checkpoint")
        with pytest.raises(GraphExecutionError) as captured:
            runtime.run_state(_request(), run_id="run-sync-checkpoint")
    finally:
        runtime.close()

    assert first.status == "completed"
    assert captured.value.code == "graph_invocation_run_id_reused"
    records = history.read_all()
    assert [record.status for record in records] == [
        "started",
        "completed",
        "started",
        "failed",
    ]


def test_sync_claim_capacity_error_is_mapped_and_closes_lifecycle(tmp_path) -> None:
    """Raw claim-store failures must not cross the sync App/Runtime boundary."""

    store = InMemoryGraphInvocationClaimStore(max_entries=1)
    store.claim(**CLAIM, invocation_token="occupied-token")
    history = RunHistoryStore(tmp_path / "sync-capacity.jsonl")
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
        run_history=history,
        graph_invocation_claim_store=store,
    )

    try:
        with pytest.raises(GraphExecutionError) as captured:
            runtime.run_state(_request(), run_id="run-sync-capacity")
    finally:
        runtime.close()

    assert captured.value.code == "graph_invocation_claim_capacity_exceeded"
    assert [record.status for record in history.read_all()] == ["started", "failed"]


def test_runtime_preserves_explicit_falsey_invocation_claim_store() -> None:
    """Truthiness-based defaulting would silently replace an explicit store."""

    class FalseyClaimStore(InMemoryGraphInvocationClaimStore):
        def __bool__(self) -> bool:
            return False

    store = FalseyClaimStore()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
        graph_invocation_claim_store=store,
    )

    try:
        assert runtime.graph_invocation_claim_store is store
    finally:
        runtime.close()


def test_completed_native_noop_rejects_different_token_at_app_boundary() -> None:
    """A completed checkpoint may no-op before the in-graph gate can reject reuse."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="completed-once",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - native boundary TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-completed-noop",
    )

    async def exercise() -> None:
        await runtime.assistant_graph_app.arun(
            prepared.initial_state,
            identity=prepared.identity,
            context=prepared.runtime_context,
        )
        with pytest.raises(GraphExecutionError) as captured:
            await runtime.assistant_graph_app.arun(
                prepared.initial_state,
                identity=prepared.identity,
                context=replace(
                    prepared.runtime_context,
                    invocation_token="competing-completed-token",
                ),
            )
        assert captured.value.code == "graph_invocation_run_id_reused"

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()


def test_public_astream_rejects_completed_checkpoint_reuse_before_native_noop() -> None:
    """Public streaming must claim before a completed checkpoint can no-op."""

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="astream-completed-once",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - native boundary TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-completed-astream",
    )

    async def exercise() -> None:
        async for _part in runtime.assistant_graph_app.astream(
            prepared.initial_state,
            identity=prepared.identity,
            context=prepared.runtime_context,
        ):
            pass
        with pytest.raises(GraphExecutionError) as captured:
            async for _part in runtime.assistant_graph_app.astream(
                prepared.initial_state,
                identity=prepared.identity,
                context=replace(
                    prepared.runtime_context,
                    invocation_token="competing-completed-astream-token",
                ),
            ):
                pass
        assert captured.value.code == "graph_invocation_run_id_reused"

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()

    assert len(adapter.requests) == 1


def test_same_token_retry_remains_idempotent_when_tracing_fails_before_native_start(
    monkeypatch,
) -> None:
    """Retained claims must still admit the same pre-native invocation token."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="retry-succeeded",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - native boundary TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-tracing-retry",
    )
    original_tracing = runtime.assistant_graph_app._native_tracing  # noqa: SLF001

    @contextmanager
    def fail_before_enter():
        raise RuntimeError("tracing setup failed")
        yield  # pragma: no cover

    async def exercise() -> None:
        monkeypatch.setattr(
            runtime.assistant_graph_app,
            "_native_tracing",
            lambda _identity: fail_before_enter(),
        )
        with pytest.raises(RuntimeError, match="tracing setup failed"):
            await runtime.assistant_graph_app.arun(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            )
        monkeypatch.setattr(runtime.assistant_graph_app, "_native_tracing", original_tracing)
        result = await runtime.assistant_graph_app.arun(
            prepared.initial_state,
            identity=prepared.identity,
            context=prepared.runtime_context,
        )
        assert result.final_state["run"]["status"] == "completed"

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()


def test_same_token_cannot_reenter_after_native_execution_starts() -> None:
    """Treating every same-token claim as a retry would execute Provider twice."""

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="same-token-once",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - App boundary TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-same-token-native-started",
    )

    async def exercise() -> None:
        await runtime.assistant_graph_app.arun(
            prepared.initial_state,
            identity=prepared.identity,
            context=prepared.runtime_context,
        )
        with pytest.raises(GraphExecutionError) as captured:
            await runtime.assistant_graph_app.arun(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            )
        assert captured.value.code == "graph_invocation_run_id_reused"

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()

    assert len(adapter.requests) == 1


def test_sync_same_token_cannot_reenter_after_native_execution_starts() -> None:
    """The synchronous public App boundary must enforce the same phase gate."""

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="sync-same-token-once",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - App boundary TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-sync-same-token-native-started",
    )
    try:
        runtime.assistant_graph_app.invoke(
            prepared.initial_state,
            identity=prepared.identity,
            context=prepared.runtime_context,
        )
        with pytest.raises(GraphExecutionError) as captured:
            runtime.assistant_graph_app.invoke(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            )
    finally:
        runtime.close()

    assert captured.value.code == "graph_invocation_run_id_reused"
    assert len(adapter.requests) == 1


def test_concurrent_same_token_public_arun_admits_only_one_native_start() -> None:
    """Concurrent same-token callers must not both cross the native boundary."""

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="concurrent-same-token-once",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - App boundary TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-concurrent-same-token",
    )

    async def exercise() -> list[object]:
        return await asyncio.gather(
            *(
                runtime.assistant_graph_app.arun(
                    prepared.initial_state,
                    identity=prepared.identity,
                    context=prepared.runtime_context,
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )

    try:
        outcomes = asyncio.run(exercise())
    finally:
        runtime.close()

    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    errors = [item for item in outcomes if isinstance(item, GraphExecutionError)]
    assert len(errors) == 1
    assert errors[0].code == "graph_invocation_run_id_reused"
    assert len(adapter.requests) == 1


def test_astream_early_close_keeps_native_started_claim() -> None:
    """Closing a partially consumed public stream must not reopen its run."""

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="early-close-once",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - App boundary TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-astream-early-close",
    )

    async def exercise() -> None:
        stream = runtime.assistant_graph_app.astream(
            prepared.initial_state,
            identity=prepared.identity,
            context=prepared.runtime_context,
        )
        await anext(stream)
        await stream.aclose()
        with pytest.raises(GraphExecutionError) as captured:
            async for _part in runtime.assistant_graph_app.astream(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            ):
                pass
        assert captured.value.code == "graph_invocation_run_id_reused"

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()


def test_thread_delete_keeps_claim_when_checkpointer_delete_fails() -> None:
    """Claim release before checkpoint deletion would reopen retained history."""

    class FailingCheckpointer:
        async def adelete_thread(self, thread_id: str) -> None:
            del thread_id
            raise RuntimeError("checkpoint delete failed")

    graph = type("CompiledGraph", (), {"checkpointer": FailingCheckpointer()})()
    store = InMemoryGraphInvocationClaimStore()
    request = _request()
    owner_digest = graph_invocation_owner_digest(
        agent_id="assistant-agent",
        user_id=request.user_id,
        session_id=request.session_id,
    )
    identity = GraphExecutionIdentity.for_assistant_turn(
        agent_id="assistant-agent",
        user_id=request.user_id,
        session_id=request.session_id,
        run_id="run-delete-order",
    )
    store.claim(
        owner_digest=owner_digest,
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        invocation_kind="invoke",
        invocation_token="delete-order-token",
    )
    app = AssistantTurnGraphApp.from_compiled_graph(graph)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="checkpoint delete failed"):
            await app.adelete_thread(
                agent_id="assistant-agent",
                user_id=request.user_id,
                session_id=request.session_id,
                invocation_claim_store=store,
            )

    asyncio.run(exercise())
    with pytest.raises(GraphInvocationClaimConflict):
        store.claim(
            owner_digest=owner_digest,
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            invocation_kind="invoke",
            invocation_token="competing-delete-order-token",
        )


def test_runtime_thread_delete_removes_checkpoint_then_releases_claims() -> None:
    """A successful host retention delete must open a genuinely new lifecycle."""

    saver = InMemorySaver()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="before-thread-delete",
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="after-thread-delete",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )
    request = _request()

    async def exercise() -> None:
        first = await runtime.arun_state(request, run_id="run-thread-lifecycle")
        assert first.status == "completed"

        deleted_claims = await runtime.adelete_assistant_thread(
            user_id=request.user_id,
            session_id=request.session_id,
        )
        assert deleted_claims == 1

        identity = GraphExecutionIdentity.for_assistant_turn(
            agent_id=runtime.agent_id,
            user_id=request.user_id,
            session_id=request.session_id,
            run_id="run-thread-lifecycle",
        )
        snapshot = await runtime.assistant_graph_app.aget_state(identity)
        assert not getattr(snapshot, "values", None)

        second = await runtime.arun_state(request, run_id="run-thread-lifecycle")
        assert second.status == "completed"
        assert second.response is not None
        assert second.response.message == "after-thread-delete"

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()

    assert len(adapter.requests) == 2


def test_thread_delete_freezes_claims_while_checkpointer_delete_is_pending() -> None:
    """A new run must not enter while retention deletion awaits its checkpointer."""

    delete_started = asyncio.Event()
    allow_delete = asyncio.Event()

    class BlockingCheckpointer:
        async def adelete_thread(self, thread_id: str) -> None:
            del thread_id
            delete_started.set()
            await allow_delete.wait()

    graph = type("CompiledGraph", (), {"checkpointer": BlockingCheckpointer()})()
    app = AssistantTurnGraphApp.from_compiled_graph(graph)
    store = InMemoryGraphInvocationClaimStore()
    request = _request()
    owner_digest = graph_invocation_owner_digest(
        agent_id="assistant-agent",
        user_id=request.user_id,
        session_id=request.session_id,
    )
    identity = GraphExecutionIdentity.for_assistant_turn(
        agent_id="assistant-agent",
        user_id=request.user_id,
        session_id=request.session_id,
        run_id="run-delete-concurrent",
    )
    store.claim(
        owner_digest=owner_digest,
        thread_id=identity.thread_id,
        run_id="run-before-delete",
        invocation_kind="invoke",
        invocation_token="before-delete-token",
    )

    async def exercise() -> int:
        deletion = asyncio.create_task(
            app.adelete_thread(
                agent_id="assistant-agent",
                user_id=request.user_id,
                session_id=request.session_id,
                invocation_claim_store=store,
            )
        )
        await delete_started.wait()
        with pytest.raises(GraphInvocationClaimConflict):
            store.claim(
                owner_digest=owner_digest,
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                invocation_kind="invoke",
                invocation_token="concurrent-delete-token",
            )
        allow_delete.set()
        return await deletion

    assert asyncio.run(exercise()) == 1
    assert (
        store.claim(
            owner_digest=owner_digest,
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            invocation_kind="invoke",
            invocation_token="new-lifecycle-token",
        )
        == "claimed"
    )


def test_all_public_execution_apis_map_raw_claim_conflicts_at_app_boundary() -> None:
    """No public API may expose a store-specific claim exception."""

    class ConflictingStore(InMemoryGraphInvocationClaimStore):
        def claim(self, **kwargs):
            del kwargs
            raise GraphInvocationClaimConflict("raw store conflict")

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
        graph_invocation_claim_store=ConflictingStore(),
    )

    def prepared(run_id: str):
        return runtime._prepare_graph_run(  # noqa: SLF001 - public boundary TDD.
            _request(),
            event_sink=None,
            cancel_token=None,
            trace_context=None,
            export_trace_context=None,
            pre_terminal_state_hook=None,
            run_id=run_id,
        )

    invoke_run = prepared("run-public-invoke")
    with pytest.raises(GraphExecutionError) as invoke_error:
        runtime.assistant_graph_app.invoke(
            invoke_run.initial_state,
            identity=invoke_run.identity,
            context=invoke_run.runtime_context,
        )

    async def exercise_async() -> list[GraphExecutionError]:
        errors: list[GraphExecutionError] = []
        for api_name in ("astream", "arun", "aresume"):
            run = prepared(f"run-public-{api_name}")
            try:
                if api_name == "astream":
                    async for _part in runtime.assistant_graph_app.astream(
                        run.initial_state,
                        identity=run.identity,
                        context=run.runtime_context,
                    ):
                        pass
                elif api_name == "arun":
                    await runtime.assistant_graph_app.arun(
                        run.initial_state,
                        identity=run.identity,
                        context=run.runtime_context,
                    )
                else:
                    await runtime.assistant_graph_app.aresume(
                        identity=run.identity,
                        context=run.runtime_context,
                        resume=AssistantApproveResume(action_ref="action-ref"),
                    )
            except GraphExecutionError as exc:
                errors.append(exc)
        return errors

    try:
        async_errors = asyncio.run(exercise_async())
    finally:
        runtime.close()

    assert invoke_error.value.code == "graph_invocation_run_id_reused"
    assert [error.code for error in async_errors] == [
        "graph_invocation_run_id_reused",
        "graph_invocation_run_id_reused",
        "graph_invocation_run_id_reused",
    ]


def test_concurrent_apps_share_claim_store_and_conflict_closes_runtime_lifecycle(
    tmp_path,
) -> None:
    """Per-app stores could execute the same owner/thread/run concurrently."""

    store = InMemoryGraphInvocationClaimStore()
    histories = [RunHistoryStore(tmp_path / f"runs-{index}.jsonl") for index in range(2)]
    adapters = [
        ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text=f"winner-{index}",
                )
            ]
        )
        for index in range(2)
    ]
    runtimes = [
        AgentGraphRuntime(
            registry=sealed_registry(),
            config=offline_config(),
            chat_adapter=adapters[index],
            session_store=InMemorySessionStore(),
            checkpointer=InMemorySaver(),
            run_history=histories[index],
            graph_invocation_claim_store=store,
        )
        for index in range(2)
    ]

    async def exercise() -> list[object]:
        return await asyncio.gather(
            *(
                runtime.arun_state(_request(), run_id="run-shared-apps")
                for runtime in runtimes
            ),
            return_exceptions=True,
        )

    try:
        outcomes = asyncio.run(exercise())
    finally:
        for runtime in runtimes:
            runtime.close()

    completed = [item for item in outcomes if isinstance(item, AgentState)]
    conflicts = [item for item in outcomes if isinstance(item, GraphExecutionError)]
    assert len(completed) == 1
    assert completed[0].status == "completed"
    assert len(conflicts) == 1
    assert conflicts[0].code == "graph_invocation_run_id_reused"
    all_records = [record for history in histories for record in history.read_all()]
    assert [record.status for record in all_records].count("failed") == 1


def test_prepare_invocation_reenters_same_turn_with_new_run_id() -> None:
    """Keeping the checkpoint run id would bind Replay/Resume to an old invocation."""

    prepared_state = assistant_turn_state_from_agent_state(
        _runtime_state(run_id="run-original")
    )
    runtime_state = _runtime_state(run_id="run-replay-new")

    updated = reenter_assistant_invocation(
        prepared_state,
        runtime_state=runtime_state,
        invocation_kind="replay",
    )

    assert updated["turn_origin_id"] == prepared_state["turn_origin_id"]
    assert updated["invocation_run_id"] == "run-replay-new"
    assert updated["invocation_run_ids"] == ["run-original", "run-replay-new"]
    assert updated["invocation_kind"] == "replay"
    assert updated["run"]["run_id"] == "run-replay-new"
    assert updated["run"]["trace_id"] == prepared_state["run"]["trace_id"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state["request"].__setitem__("user_id", "other-owner"),
        lambda state: state["request"].__setitem__("text", "other-request"),
        lambda state: state.__setitem__("profile", "worker"),
        lambda state: state.__setitem__("state_schema_version", 999),
        lambda state: state["run"].__setitem__("trace_id", "other-trace"),
    ],
)
def test_reentry_fails_closed_on_checkpoint_runtime_mismatch(mutation) -> None:
    """Weak re-entry validation could run historical work for the wrong invocation owner."""

    persisted = assistant_turn_state_from_agent_state(
        _runtime_state(run_id="run-original")
    )
    mutated = deepcopy(persisted)
    mutation(mutated)

    with pytest.raises(AssistantStateCompatibilityError):
        reenter_assistant_invocation(
            mutated,
            runtime_state=_runtime_state(run_id="run-new"),
            invocation_kind="replay",
        )


def test_diagnostic_run_ids_do_not_replace_atomic_claim_store() -> None:
    """Historical diagnostics must not reject a same-token graph-node re-entry."""

    state = assistant_turn_state_from_agent_state(_runtime_state(run_id="run-loop"))
    first = reenter_assistant_invocation(
        state,
        runtime_state=_runtime_state(run_id="run-loop"),
        invocation_kind="invoke",
    )
    second = reenter_assistant_invocation(
        first,
        runtime_state=_runtime_state(run_id="run-loop"),
        invocation_kind="invoke",
    )

    assert second["invocation_run_ids"] == ["run-loop"]


def test_real_tool_stream_crosses_gate_before_every_semantic_node() -> None:
    """Any semantic edge bypassing the anchor/gate must change this real update order."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="provider-reentry",
                            name=ProbeTool.name,
                            arguments={"value": "gate-sentinel"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="done-reentry",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - native stream TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-loop",
    )

    async def exercise() -> tuple[list[str], dict[str, Any]]:
        result = await runtime.assistant_graph_app.arun(
            prepared.initial_state,
            identity=prepared.identity,
            context=prepared.runtime_context,
        )
        order: list[str] = []
        for part in result.parts:
            if part.type != "updates" or part.namespace or not isinstance(part.data, dict):
                continue
            order.extend(str(node) for node in part.data)
        return order, result.final_state

    try:
        order, final_state = asyncio.run(exercise())
    finally:
        runtime.close()

    assert order == [
        "prepare_invocation",
        "assistant",
        "time_travel_anchor",
        "prepare_invocation",
        "execute_tool",
        "time_travel_anchor",
        "prepare_invocation",
        "assistant",
        "time_travel_anchor",
        "prepare_invocation",
        "compose_response",
        "time_travel_anchor",
        "prepare_invocation",
    ]
    assert final_state["continuation"] == "end"
    assert final_state["invocation_run_ids"].count("run-loop") == 1
    assert final_state["run"]["status"] == "completed"


def test_graph_topology_routes_semantic_nodes_only_through_anchor_and_gate() -> None:
    """A direct semantic edge could execute Provider/Tool/compose before re-entry."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
    )
    try:
        drawable = runtime.assistant_graph_app.graph.get_graph()
    finally:
        runtime.close()

    edges = {(edge.source, edge.target) for edge in drawable.edges}
    semantic = {"assistant", "await_input", "execute_tool", "compose_response"}
    assert ("__start__", "prepare_invocation") in edges
    assert ("time_travel_anchor", "prepare_invocation") in edges
    assert all((node, "time_travel_anchor") in edges for node in semantic)
    assert not any(source in semantic and target in semantic for source, target in edges)
    assert not any(source in semantic and target == "__end__" for source, target in edges)
