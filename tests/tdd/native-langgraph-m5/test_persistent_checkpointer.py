from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime import checkpointer as checkpointers
from assistant_agent.runtime import graph_invocation_claims as claims
from assistant_agent.runtime import runtime_host as runtime_hosts
from assistant_agent.runtime import assistant_run_service
from assistant_agent.runtime.assistant_interrupts import (
    AssistantApproveResume,
    AssistantInterruptRequest,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_time_travel import (
    GraphForkRequest,
    GraphReplayRequest,
)
from assistant_agent.runtime.event_stream import AgentRunStream
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import ProbeTool, ScriptedChatAdapter, sealed_registry


def test_sqlite_backend_and_path_are_explicit_configuration() -> None:
    """Mapping sqlite to memory would silently disable cross-host recovery."""

    config = ProviderConfig.from_env(
        {
            "LANGGRAPH_CHECKPOINTER_BACKEND": "sqlite",
            "LANGGRAPH_CHECKPOINT_PATH": "/tmp/checkpoints.sqlite3",
        }
    )

    assert config.langgraph_checkpointer_backend == "sqlite"
    assert config.langgraph_checkpoint_path == "/tmp/checkpoints.sqlite3"
    with pytest.raises(ValueError, match="LANGGRAPH_CHECKPOINTER_BACKEND"):
        ProviderConfig.from_env({"LANGGRAPH_CHECKPOINTER_BACKEND": "typo"})


def test_sync_runtime_composition_rejects_sqlite_without_async_owner(tmp_path) -> None:
    """The pre-cutover sync root must not silently compile with no saver."""

    config = replace(
        ProviderConfig(),
        langgraph_checkpointer_backend="sqlite",
        langgraph_checkpoint_path=str(tmp_path / "sync-root.sqlite3"),
    )

    with pytest.raises(checkpointers.CheckpointerConfigurationError) as captured:
        assistant_run_service.create_runtime(config=config, load_env=False)

    assert captured.value.code == "langgraph_checkpointer_owner_required"
    assert not (tmp_path / "sync-root.sqlite3").exists()


def test_sqlite_claim_store_persists_unique_claim_and_cas_phase(tmp_path) -> None:
    """Reopening the business DB must not make a claimed run executable again."""

    path = tmp_path / "claims.sqlite3"
    first = claims.SQLiteGraphInvocationClaimStore(path)
    assert (
        first.claim(
            owner_digest="owner-a",
            thread_id="thread-a",
            run_id="run-a",
            invocation_kind="replay",
            invocation_token="token-a",
        )
        == "claimed"
    )
    first.close()

    second = claims.SQLiteGraphInvocationClaimStore(path)
    assert (
        second.claim(
            owner_digest="owner-a",
            thread_id="thread-a",
            run_id="run-a",
            invocation_kind="replay",
            invocation_token="token-a",
        )
        == "same_invocation"
    )
    second.begin_native(
        owner_digest="owner-a",
        thread_id="thread-a",
        run_id="run-a",
        invocation_token="token-a",
    )
    with pytest.raises(claims.GraphInvocationClaimConflict):
        second.begin_native(
            owner_digest="owner-a",
            thread_id="thread-a",
            run_id="run-a",
            invocation_token="token-a",
        )
    second.close()

    third = claims.SQLiteGraphInvocationClaimStore(path)
    third.mark_terminal(
        owner_digest="owner-a",
        thread_id="thread-a",
        run_id="run-a",
        invocation_token="token-a",
    )
    with pytest.raises(claims.GraphInvocationClaimConflict):
        third.claim(
            owner_digest="owner-a",
            thread_id="thread-a",
            run_id="run-a",
            invocation_kind="fork",
            invocation_token="token-a",
        )
    third.close()


def test_sqlite_claim_store_persists_thread_tombstone_across_connections(
    tmp_path,
) -> None:
    """A second host must not claim a thread frozen by retention deletion."""

    path = tmp_path / "claims.sqlite3"
    deletion_owner = claims.SQLiteGraphInvocationClaimStore(path)
    competing_host = claims.SQLiteGraphInvocationClaimStore(path)
    deletion_owner.begin_thread_delete(owner_digest="owner-a", thread_id="thread-a")

    with pytest.raises(claims.GraphInvocationClaimConflict):
        competing_host.claim(
            owner_digest="owner-a",
            thread_id="thread-a",
            run_id="run-during-delete",
            invocation_kind="invoke",
            invocation_token="token-during-delete",
        )

    assert (
        deletion_owner.finish_thread_delete(
            owner_digest="owner-a", thread_id="thread-a", commit=True
        )
        == 0
    )
    assert (
        competing_host.claim(
            owner_digest="owner-a",
            thread_id="thread-a",
            run_id="run-after-delete",
            invocation_kind="invoke",
            invocation_token="token-after-delete",
        )
        == "claimed"
    )
    competing_host.close()
    deletion_owner.close()


def test_async_owner_opens_official_sqlite_saver_and_business_claim_store(
    tmp_path,
) -> None:
    """A sqlite graph host must own both durable resources without conflating them."""

    async def exercise() -> None:
        config = replace(
            ProviderConfig(),
            langgraph_checkpointer_backend="sqlite",
            langgraph_checkpoint_path=str(tmp_path / "checkpoints.sqlite3"),
        )
        owner = checkpointers.AsyncCheckpointerOwner(config)
        with pytest.raises(checkpointers.CheckpointerConfigurationError) as unopened:
            _ = owner.checkpointer
        assert unopened.value.code == "langgraph_checkpointer_owner_not_open"

        async with owner:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            assert isinstance(owner.checkpointer, AsyncSqliteSaver)
            assert isinstance(
                owner.invocation_claim_store,
                claims.SQLiteGraphInvocationClaimStore,
            )
            assert owner.is_persistent is True
            assert owner.claim_path != owner.checkpoint_path
            assert owner.claim_path.name.endswith(".claims.sqlite3")

        with pytest.raises(checkpointers.CheckpointerConfigurationError) as closed:
            _ = owner.checkpointer
        assert closed.value.code == "langgraph_checkpointer_owner_not_open"

    asyncio.run(exercise())


def test_sqlite_owner_fails_closed_without_path_dependency_or_async_owner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfigured persistence must never degrade to process memory."""

    missing_path = replace(
        ProviderConfig(),
        langgraph_checkpointer_backend="sqlite",
        langgraph_checkpoint_path=None,
    )
    with pytest.raises(checkpointers.CheckpointerConfigurationError) as no_owner:
        checkpointers.create_checkpointer(missing_path)
    assert no_owner.value.code == "langgraph_checkpointer_owner_required"

    async def exercise() -> None:
        with pytest.raises(checkpointers.CheckpointerConfigurationError) as missing:
            async with checkpointers.AsyncCheckpointerOwner(missing_path):
                pass
        assert missing.value.code == "langgraph_checkpoint_path_required"

        relative = replace(
            missing_path,
            langgraph_checkpoint_path="relative/checkpoints.sqlite3",
        )
        with pytest.raises(checkpointers.CheckpointerConfigurationError) as invalid:
            async with checkpointers.AsyncCheckpointerOwner(relative):
                pass
        assert invalid.value.code == "langgraph_checkpoint_path_invalid"

        configured = replace(
            missing_path,
            langgraph_checkpoint_path=str(tmp_path / "checkpoints.sqlite3"),
        )

        def dependency_missing():
            raise ImportError("dependency-secret")

        monkeypatch.setattr(
            checkpointers,
            "_load_async_sqlite_saver",
            dependency_missing,
        )
        with pytest.raises(checkpointers.CheckpointerConfigurationError) as dependency:
            async with checkpointers.AsyncCheckpointerOwner(configured):
                pass
        assert dependency.value.code == "langgraph_sqlite_dependency_unavailable"
        assert "dependency-secret" not in str(dependency.value)

    asyncio.run(exercise())


def test_memory_owner_is_explicitly_nonpersistent() -> None:
    """InMemorySaver must not satisfy the cross-host durability capability."""

    async def exercise() -> None:
        async with checkpointers.AsyncCheckpointerOwner(
            replace(ProviderConfig(), langgraph_checkpointer_backend="memory")
        ) as owner:
            assert isinstance(owner.checkpointer, InMemorySaver)
            assert isinstance(
                owner.invocation_claim_store,
                claims.InMemoryGraphInvocationClaimStore,
            )
            assert owner.is_persistent is False
            assert owner.checkpoint_path is None
            assert owner.claim_path is None

    asyncio.run(exercise())


def test_async_runtime_host_closes_runtime_trace_then_graph_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the saver before Runtime consumers would race live graph calls."""

    lifecycle: list[str] = []

    class FakeGraphOwner:
        def __init__(self, config) -> None:
            self.checkpointer = "checkpointer-sentinel"
            self.invocation_claim_store = "claims-sentinel"

        async def open(self):
            lifecycle.append("graph_open")
            return self

        async def aclose(self) -> None:
            lifecycle.append("graph_close")

    class Runtime:
        trace_store = "runtime-trace-sentinel"

        def close(self) -> bool:
            lifecycle.append("runtime_close")
            return True

    class TraceStore:
        def close(self, *, timeout: float) -> bool:
            assert timeout > 0
            lifecycle.append("trace_close")
            return True

    def runtime_factory(*, checkpointer, graph_invocation_claim_store):
        assert checkpointer == "checkpointer-sentinel"
        assert graph_invocation_claim_store == "claims-sentinel"
        lifecycle.append("runtime_create")
        return Runtime()

    monkeypatch.setattr(
        runtime_hosts,
        "AsyncCheckpointerOwner",
        FakeGraphOwner,
    )

    async def exercise() -> None:
        host = await runtime_hosts.RuntimeHost.aopen(
            config=ProviderConfig(),
            runtime_factory=runtime_factory,
            owned_trace_store=TraceStore(),
        )
        with pytest.raises(RuntimeError, match="private"):
            _ = host.runtime
        with pytest.raises(RuntimeError, match="synchronous"):
            host.run_state("sync-request")
        with pytest.raises(RuntimeError, match="aclose"):
            host.close()
        assert await host.aclose(timeout=2.0) is True
        assert await host.aclose(timeout=2.0) is True

    asyncio.run(exercise())
    assert lifecycle == [
        "graph_open",
        "runtime_create",
        "runtime_close",
        "trace_close",
        "graph_close",
    ]


def test_async_runtime_host_retains_stream_lease_through_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning a stream object must not release its saver lease early."""

    result_waiting = asyncio.Event()
    release_result = asyncio.Event()
    lifecycle: list[str] = []

    class FakeGraphOwner:
        checkpointer = "checkpointer-sentinel"
        invocation_claim_store = "claims-sentinel"

        def __init__(self, config) -> None:
            pass

        async def open(self):
            return self

        async def aclose(self) -> None:
            lifecycle.append("graph_close")

    class Stream:
        async def result(self):
            result_waiting.set()
            await release_result.wait()
            return "stream-result"

    class Runtime:
        trace_store = None

        def astream_state(self, request, **kwargs):
            return Stream()

        def close(self) -> bool:
            lifecycle.append("runtime_close")
            return True

    monkeypatch.setattr(runtime_hosts, "AsyncCheckpointerOwner", FakeGraphOwner)

    async def exercise() -> None:
        host = await runtime_hosts.RuntimeHost.aopen(
            config=ProviderConfig(), runtime_factory=lambda **kwargs: Runtime()
        )
        stream = await host.astream_state("stream-request")
        await result_waiting.wait()
        closing = asyncio.create_task(host.aclose(timeout=2.0))
        await asyncio.sleep(0)
        assert not closing.done()
        release_result.set()
        assert await stream.result() == "stream-result"
        assert await closing is True

    asyncio.run(exercise())
    assert lifecycle == ["runtime_close", "graph_close"]


def test_cancelled_stream_result_consumer_cannot_release_host_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consumer cancellation must not cancel the producer completion future."""

    lifecycle: list[str] = []

    class FakeGraphOwner:
        checkpointer = "checkpointer-sentinel"
        invocation_claim_store = "claims-sentinel"

        def __init__(self, config) -> None:
            pass

        async def open(self):
            return self

        async def aclose(self) -> None:
            lifecycle.append("graph_close")

    class Runtime:
        trace_store = None

        def __init__(self) -> None:
            self.stream = AgentRunStream(loop=asyncio.get_running_loop())

        def astream_state(self, request, **kwargs):
            return self.stream

        def close(self) -> bool:
            lifecycle.append("runtime_close")
            return True

    monkeypatch.setattr(runtime_hosts, "AsyncCheckpointerOwner", FakeGraphOwner)

    async def exercise() -> None:
        runtime = Runtime()
        host = await runtime_hosts.RuntimeHost.aopen(
            config=ProviderConfig(), runtime_factory=lambda **kwargs: runtime
        )
        stream = await host.astream_state("stream-request")
        consumer = asyncio.create_task(stream.result())
        await asyncio.sleep(0)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

        closing = asyncio.create_task(host.aclose(timeout=2.0))
        await asyncio.sleep(0)
        assert not closing.done()
        runtime.stream.set_result("producer-terminal")
        assert await closing is True
        assert await stream.result() == "producer-terminal"

    asyncio.run(exercise())
    assert lifecycle == ["runtime_close", "graph_close"]


def test_async_runtime_host_waits_for_active_graph_invocation_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saver shutdown must wait until admitted async graph consumers finish."""

    lifecycle: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    class FakeGraphOwner:
        checkpointer = "checkpointer-sentinel"
        invocation_claim_store = "claims-sentinel"

        def __init__(self, config) -> None:
            pass

        async def open(self):
            return self

        async def aclose(self) -> None:
            lifecycle.append("graph_close")

    class Runtime:
        trace_store = None

        async def arun_state(self, request, **kwargs):
            lifecycle.append("run_started")
            started.set()
            await release.wait()
            lifecycle.append("run_finished")
            return "state-sentinel"

        def close(self) -> bool:
            lifecycle.append("runtime_close")
            return True

    monkeypatch.setattr(runtime_hosts, "AsyncCheckpointerOwner", FakeGraphOwner)

    async def exercise() -> None:
        host = await runtime_hosts.RuntimeHost.aopen(
            config=ProviderConfig(),
            runtime_factory=lambda **kwargs: Runtime(),
        )
        invocation = asyncio.create_task(host.arun_state("request-sentinel"))
        await started.wait()
        closing = asyncio.create_task(host.aclose(timeout=2.0))
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(RuntimeError, match="closing"):
            await host.arun_state("late-request")
        release.set()
        assert await invocation == "state-sentinel"
        assert await closing is True

    asyncio.run(exercise())
    assert lifecycle == [
        "run_started",
        "run_finished",
        "runtime_close",
        "graph_close",
    ]


def test_fresh_persistent_hosts_resume_history_replay_and_fork(tmp_path) -> None:
    """Sharing Python saver objects would not prove process-host durability."""

    checkpoint_path = tmp_path / "assistant-checkpoints.sqlite3"
    config = replace(
        ProviderConfig(),
        langgraph_checkpointer_backend="sqlite",
        langgraph_checkpoint_path=str(checkpoint_path),
    )
    tool = ProbeTool()
    request = UserRequest(
        user_id="persistent-user",
        session_id="persistent-session",
        text="persistent request",
    )

    def runtime_factory(adapter):
        def build(*, checkpointer, graph_invocation_claim_store):
            return AgentGraphRuntime(
                registry=sealed_registry(tool),
                config=config,
                chat_adapter=adapter,
                session_store=InMemorySessionStore(),
                checkpointer=checkpointer,
                graph_invocation_claim_store=graph_invocation_claim_store,
                allow_interrupt=True,
            )

        return build

    async def exercise() -> None:
        first_adapter = ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="persistent-action",
                            name=tool.name,
                            arguments={"value": "persistent-value"},
                        )
                    ],
                )
            ]
        )
        first = await runtime_hosts.RuntimeHost.aopen(
            config=config,
            runtime_factory=runtime_factory(first_adapter),
        )
        waiting = await first.arun_state(
            request,
            run_id="persistent-before",
            interrupt_request=AssistantInterruptRequest(
                kind="approval",
                prompt="approve persistent action",
                action_ref="persistent-action",
                allowed_resume_kinds=("approve", "reject"),
            ),
        )
        assert waiting.status == "waiting_user"
        assert await first.aclose() is True

        second_adapter = ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="persistent terminal",
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="persistent replay",
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="persistent fork",
                ),
            ]
        )
        second = await runtime_hosts.RuntimeHost.aopen(
            config=config,
            runtime_factory=runtime_factory(second_adapter),
        )
        resumed = await second.aresume_state(
            request,
            resume=AssistantApproveResume(action_ref="persistent-action"),
            run_id="persistent-after",
        )
        assert resumed.status == "completed"
        assert resumed.run_id == "persistent-after"
        assert resumed.response is not None
        assert resumed.response.message == "persistent terminal"

        assert await second.aclose() is True

        time_travel_request = UserRequest(
            user_id="persistent-time-travel-user",
            session_id="persistent-time-travel-session",
            text="persistent time travel origin",
        )
        time_travel_owner = RequestIdentity.for_user(
            user_id=time_travel_request.user_id,
            agent_id="agent.default",
            session_id=time_travel_request.session_id,
        )
        third = await runtime_hosts.RuntimeHost.aopen(
            config=config,
            runtime_factory=runtime_factory(
                ScriptedChatAdapter(
                    [
                        ChatResult(
                            provider="scripted",
                            model="scripted",
                            finish_reason="stop",
                            response_text="time travel origin",
                        )
                    ]
                )
            ),
        )
        origin = await third.arun_state(time_travel_request, run_id="persistent-origin")
        assert origin.status == "completed"
        assert await third.aclose() is True

        fourth = await runtime_hosts.RuntimeHost.aopen(
            config=config,
            runtime_factory=runtime_factory(
                ScriptedChatAdapter(
                    [
                        ChatResult(
                            provider="scripted",
                            model="scripted",
                            finish_reason="stop",
                            response_text="persistent replay",
                        ),
                        ChatResult(
                            provider="scripted",
                            model="scripted",
                            finish_reason="stop",
                            response_text="persistent fork",
                        ),
                    ]
                )
            ),
        )
        history = await fourth.alist_history(time_travel_owner, limit=100)
        assert history
        replay_selector = next(
            item
            for item in history
            if item.next_nodes == ("prepare_invocation",) and not item.has_interrupt
        )
        replayed = await fourth.areplay_state(
            time_travel_owner,
            GraphReplayRequest(selector={"history_ref": replay_selector.history_ref}),
            run_id="persistent-replay",
        )
        assert replayed.status == "completed"
        assert replayed.response is not None
        assert replayed.run_id == "persistent-replay"
        assert replayed.turn_provenance == "time_travel"
        forked = await fourth.afork_state(
            time_travel_owner,
            GraphForkRequest(
                selector={"history_ref": replay_selector.history_ref},
                patch={"request_text": "persistent fork request"},
            ),
            run_id="persistent-fork",
        )
        assert forked.status == "completed"
        assert forked.response is not None
        assert forked.run_id == "persistent-fork"
        assert forked.turn_provenance == "time_travel"
        assert await fourth.aclose() is True

    asyncio.run(exercise())


def test_fresh_persistent_host_rejects_claimed_run_id(tmp_path) -> None:
    """A public replay in a rebuilt host must reject the persisted run claim."""

    config = replace(
        ProviderConfig(),
        langgraph_checkpointer_backend="sqlite",
        langgraph_checkpoint_path=str(tmp_path / "claims-host.sqlite3"),
    )

    owner_identity = RequestIdentity.for_user(
        user_id="claim-user",
        agent_id="agent.default",
        session_id="claim-session",
    )

    def runtime_factory(adapter):
        def build(*, checkpointer, graph_invocation_claim_store):
            return AgentGraphRuntime(
                registry=sealed_registry(),
                config=config,
                chat_adapter=adapter,
                session_store=InMemorySessionStore(),
                checkpointer=checkpointer,
                graph_invocation_claim_store=graph_invocation_claim_store,
            )

        return build

    async def exercise() -> None:
        first = await runtime_hosts.RuntimeHost.aopen(
            config=config,
            runtime_factory=runtime_factory(
                ScriptedChatAdapter(
                    [
                        ChatResult(
                            provider="scripted",
                            model="scripted",
                            finish_reason="stop",
                            response_text="claim origin",
                        ),
                        ChatResult(
                            provider="scripted",
                            model="scripted",
                            finish_reason="stop",
                            response_text="first replay",
                        ),
                    ]
                )
            ),
        )
        await first.arun_state(
            UserRequest(
                user_id=owner_identity.user_id,
                session_id=owner_identity.session_id,
                text="claim origin",
            ),
            run_id="claim-origin",
        )
        history = await first.alist_history(owner_identity, limit=100)
        selector = None
        selector = next(
            (
                candidate
                for candidate in history
                if candidate.next_nodes == ("prepare_invocation",)
                and not candidate.has_interrupt
            ),
            None,
        )
        assert selector is not None
        replay_request = GraphReplayRequest(
            selector={"history_ref": selector.history_ref}
        )
        first_replay = await first.areplay_state(
            owner_identity,
            replay_request,
            run_id="persistent-claimed-run",
        )
        assert first_replay.status == "completed"
        await first.aclose()

        second_adapter = ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="must not run",
                )
            ]
        )
        second = await runtime_hosts.RuntimeHost.aopen(
            config=config,
            runtime_factory=runtime_factory(second_adapter),
        )
        with pytest.raises(GraphExecutionError) as captured:
            await second.areplay_state(
                owner_identity,
                replay_request,
                run_id="persistent-claimed-run",
            )
        assert captured.value.code == "graph_invocation_run_id_reused"
        assert second_adapter.requests == []
        await second.aclose()

    from assistant_agent.runtime.assistant_graph_app import GraphExecutionError

    asyncio.run(exercise())
