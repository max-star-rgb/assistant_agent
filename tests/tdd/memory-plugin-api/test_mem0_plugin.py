from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from time import sleep
from typing import Any

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.factory import create_long_term_memory_service
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.mem0.client import Mem0Client, UnavailableMem0Client
from assistant_agent.memory.mem0.models import (
    Mem0CompletedTurn,
    Mem0Identity,
    Mem0IngestionResult,
    Mem0MemoryChange,
)
from assistant_agent.memory.models import LongTermMemory
from assistant_agent.memory.service import LongTermMemoryService
from assistant_agent.memory.plugins.builtin.mem0 import (
    Mem0MemoryPlugin,
    default_memory_plugin_factories,
)
from assistant_agent.memory.plugins.assembly import MemoryPluginAssemblyError
from assistant_agent.memory.plugins.contracts import (
    CompletedMemoryTurn,
    MemoryBudgetHint,
    MemoryContextRequest,
    MemoryIdentity,
    MemoryMessage,
    MemoryPluginBuildContext,
    MemoryPluginExecutionPolicy,
    MemorySessionCloseRequest,
    MemorySessionOpenRequest,
    MemoryTurnIngestionRequest,
    NeverCancelledMemoryToken,
)
from assistant_agent.memory.plugins.host import (
    MemoryPluginHost,
    bind_memory_plugin_identity,
)
from assistant_agent.memory.plugins.media import ManagedMemoryMediaStore
from assistant_agent.memory.plugins.registry import (
    MemoryPluginRegistrationRecord,
    MemoryPluginRegistry,
)
from assistant_agent.memory.plugins.session_store import MemoryPluginSessionStore
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


class RecordingMem0Client:
    configured = True

    def __init__(
        self,
        *,
        memories: list[LongTermMemory],
        ingestion_result: Mem0IngestionResult | None = None,
    ) -> None:
        self.memories = memories
        self.ingestion_result = ingestion_result or Mem0IngestionResult(
            accepted=True
        )
        self.recall_identities: list[Any] = []
        self.ingested_turns: list[Any] = []

    def recall_long_term_memory(self, identity: Any) -> list[LongTermMemory]:
        self.recall_identities.append(identity)
        return list(self.memories)

    def ingest_completed_turn(self, turn: Any) -> Mem0IngestionResult:
        self.ingested_turns.append(turn)
        return self.ingestion_result


class ExplodingIngestionMem0Client(RecordingMem0Client):
    def ingest_completed_turn(self, turn: Any) -> Mem0IngestionResult:
        self.ingested_turns.append(turn)
        raise RuntimeError("provider-secret-sentinel")


class SlowIngestionMem0Client(RecordingMem0Client):
    def ingest_completed_turn(self, turn: Any) -> Mem0IngestionResult:
        self.ingested_turns.append(turn)
        sleep(0.05)
        return self.ingestion_result


class BlockingIngestionMem0Client(RecordingMem0Client):
    def __init__(self) -> None:
        super().__init__(memories=[])
        self.started = Event()
        self.release = Event()

    def ingest_completed_turn(self, turn: Any) -> Mem0IngestionResult:
        self.ingested_turns.append(turn)
        self.started.set()
        if not self.release.wait(1.0):
            raise TimeoutError("blocking-ingestion-timeout")
        return self.ingestion_result


class RejectingSecretResolver:
    def resolve(self, reference: str) -> str:
        raise KeyError(reference)


def _open_request() -> MemorySessionOpenRequest:
    return MemorySessionOpenRequest(
        memory_session_id="memory-session-sentinel",
        identity=MemoryIdentity(
            user_id="usr_3430bcf0b42b125b6c83834213a8e0aa",
            agent_id="agt_76635ea05f8a084b1c6c867bc39c9ae2",
            session_id="run_0eedf0a7ad0273960e77d5fe6105f879",
        ),
        opened_at=NOW,
        entry_profile="default",
        deadline=NOW + timedelta(seconds=5),
        cancellation=NeverCancelledMemoryToken(),
    )


def _ingestion_request() -> MemoryTurnIngestionRequest:
    open_request = _open_request()
    return MemoryTurnIngestionRequest(
        memory_session_id=open_request.memory_session_id,
        identity=open_request.identity,
        turn=CompletedMemoryTurn(
            user_message=MemoryMessage(role="user", text="request-sentinel"),
            assistant_message=MemoryMessage(
                role="assistant",
                text="response-sentinel",
            ),
            occurred_at=NOW,
        ),
        idempotency_key="source-turn-sentinel",
        deadline=open_request.deadline,
        cancellation=NeverCancelledMemoryToken(),
    )


def _context_request() -> MemoryContextRequest:
    open_request = _open_request()
    return MemoryContextRequest(
        memory_session_id=open_request.memory_session_id,
        identity=open_request.identity,
        current_turn=MemoryMessage(role="user", text="request-sentinel"),
        context_budget_hint=MemoryBudgetHint(max_items=10, max_chars=1000),
        deadline=open_request.deadline,
        cancellation=NeverCancelledMemoryToken(),
    )


def _close_request() -> MemorySessionCloseRequest:
    open_request = _open_request()
    return MemorySessionCloseRequest(
        memory_session_id=open_request.memory_session_id,
        identity=open_request.identity,
        reason="normal",
        deadline=open_request.deadline,
        cancellation=NeverCancelledMemoryToken(),
    )


def _build_context(provider_mode: str) -> MemoryPluginBuildContext:
    media_store = ManagedMemoryMediaStore(max_total_bytes=1024)
    return MemoryPluginBuildContext(
        provider_mode=provider_mode,
        media_reader=media_store,
        artifact_writer=media_store,
        secret_resolver=RejectingSecretResolver(),
        clock=lambda: NOW,
    )


def _host_for_plugin(
    plugin: Mem0MemoryPlugin,
    *,
    execution_policy: MemoryPluginExecutionPolicy | None = None,
) -> MemoryPluginHost:
    registry = MemoryPluginRegistry(
        records=[
            MemoryPluginRegistrationRecord(
                descriptor=plugin.descriptor,
                source="test",
                enabled=True,
                active=True,
            )
        ],
        active_plugin=plugin,
    )
    return MemoryPluginHost(
        registry=registry,
        session_store=MemoryPluginSessionStore(),
        media_store=ManagedMemoryMediaStore(max_total_bytes=1024),
        ingestion_queue=MemoryIngestionQueue(max_workers=1, max_pending=2),
        execution_policy=execution_policy,
        clock=lambda: NOW,
    )


def _completed_state() -> AgentState:
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="request-sentinel",
            metadata={"conversation_turn_index": 1},
        ),
        run_id="runtime-run-sentinel",
        agent_id="agent-sentinel",
    )
    state.set_response(AgentResponse(message="response-sentinel"))
    return state


def test_mem0_plugin_open_session_maps_native_records() -> None:
    """Removing the Mem0-to-standard item mapping must break this test."""

    client = RecordingMem0Client(
        memories=[
            LongTermMemory(
                memory_id="memory-sentinel",
                text="fact-sentinel",
                created_at=NOW,
            )
        ]
    )
    plugin = Mem0MemoryPlugin(client=client)

    result = plugin.open_session(_open_request())

    assert result.status == "ready"
    assert result.initial_contribution is not None
    assert result.initial_contribution.items[0].memory_id == "memory-sentinel"
    assert result.initial_contribution.items[0].source == "long_term"
    assert client.recall_identities[0].model_dump() == {
        "user_id": "usr_3430bcf0b42b125b6c83834213a8e0aa",
        "agent_id": "agt_76635ea05f8a084b1c6c867bc39c9ae2",
        "run_id": "run_0eedf0a7ad0273960e77d5fe6105f879",
    }


def test_mem0_plugin_identity_matches_legacy_binding() -> None:
    """Changing the historical Mem0 hash namespace must break this test."""

    runtime_identity = RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )

    assert bind_memory_plugin_identity(
        runtime_identity,
        namespace="assistant-agent",
    ).model_dump() == {
        "user_id": "usr_3430bcf0b42b125b6c83834213a8e0aa",
        "agent_id": "agt_76635ea05f8a084b1c6c867bc39c9ae2",
        "session_id": "run_0eedf0a7ad0273960e77d5fe6105f879",
        "tenant_id": None,
        "project_id": None,
    }


def test_mem0_plugin_ingestion_maps_native_changes_and_private_turn() -> None:
    """Bypassing the private Mem0 turn or event mapping must break this test."""

    client = RecordingMem0Client(
        memories=[],
        ingestion_result=Mem0IngestionResult(
            accepted=True,
            memory_ids=["memory-add", "memory-update", "memory-delete"],
            changes=[
                Mem0MemoryChange(
                    memory_id="memory-add",
                    memory="created-sentinel",
                    event="ADD",
                ),
                Mem0MemoryChange(
                    memory_id="memory-update",
                    memory="updated-sentinel",
                    event="UPDATE",
                ),
                Mem0MemoryChange(
                    memory_id="memory-delete",
                    event="DELETE",
                ),
            ],
        ),
    )
    plugin = Mem0MemoryPlugin(client=client)

    request = _ingestion_request()
    result = plugin.ingest_turn(request)
    repeated = plugin.ingest_turn(
        request.model_copy(
            update={"deadline": request.deadline + timedelta(seconds=1)}
        )
    )

    assert result.status == "accepted"
    assert repeated == result
    assert len(client.ingested_turns) == 1
    assert [change.model_dump() for change in result.changes] == [
        {
            "operation": "created",
            "memory_id": "memory-add",
            "memory_type": "long_term",
        },
        {
            "operation": "updated",
            "memory_id": "memory-update",
            "memory_type": "long_term",
        },
        {
            "operation": "deleted",
            "memory_id": "memory-delete",
            "memory_type": "long_term",
        },
    ]
    native_turn = client.ingested_turns[0]
    assert native_turn.identity.model_dump() == {
        "user_id": "usr_3430bcf0b42b125b6c83834213a8e0aa",
        "agent_id": "agt_76635ea05f8a084b1c6c867bc39c9ae2",
        "run_id": "run_0eedf0a7ad0273960e77d5fe6105f879",
    }
    assert native_turn.user_text == "request-sentinel"
    assert native_turn.assistant_text == "response-sentinel"
    assert native_turn.source_turn == "source-turn-sentinel"


def test_mem0_client_recall_accepts_an_already_bound_identity() -> None:
    """Reintroducing Runtime identity binding inside Mem0Client must fail."""

    requests: list[Any] = []

    def transport(request: Any) -> dict[str, Any]:
        requests.append(request)
        return {
            "results": [
                {
                    "id": f"memory-{index}",
                    "memory": f"fact-{index}",
                    "created_at": "2026-08-07T12:00:00+00:00",
                }
                for index in range(7)
            ]
        }

    client = Mem0Client(
        base_url="http://memory.invalid",
        timeout_seconds=7.0,
        transport=transport,
    )
    identity = Mem0Identity(
        user_id="usr_3430bcf0b42b125b6c83834213a8e0aa",
        agent_id="agt_76635ea05f8a084b1c6c867bc39c9ae2",
        run_id="run_0eedf0a7ad0273960e77d5fe6105f879",
    )

    memories = client.recall_long_term_memory(identity)

    assert [memory.memory_id for memory in memories] == [
        "memory-0",
        "memory-1",
        "memory-2",
        "memory-3",
        "memory-4",
        "memory-5",
        "memory-6",
    ]
    assert requests[0].method == "GET"
    assert requests[0].path == "/memories"
    assert requests[0].query == {
        "user_id": "usr_3430bcf0b42b125b6c83834213a8e0aa",
        "agent_id": "agt_76635ea05f8a084b1c6c867bc39c9ae2",
    }
    assert requests[0].timeout_seconds == 7.0


def test_mem0_client_ingestion_preserves_native_http_and_timeout_semantics() -> None:
    """Changing the native add payload, timeout, or event filter must fail."""

    requests: list[Any] = []

    def transport(request: Any) -> dict[str, Any]:
        requests.append(request)
        return {
            "results": [
                {
                    "id": "memory-add",
                    "memory": "created-sentinel",
                    "event": "add",
                },
                {
                    "id": "memory-update",
                    "memory": "updated-sentinel",
                    "event": "UPDATE",
                },
                {"id": "memory-delete", "event": "DELETE"},
                {"id": "unsupported", "memory": "ignored", "event": "MERGE"},
            ]
        }

    client = Mem0Client(
        base_url="http://memory.invalid/",
        timeout_seconds=7.0,
        api_key="api-key-sentinel",
        transport=transport,
    )
    turn = Mem0CompletedTurn(
        identity=Mem0Identity(
            user_id="usr_3430bcf0b42b125b6c83834213a8e0aa",
            agent_id="agt_76635ea05f8a084b1c6c867bc39c9ae2",
            run_id="run_0eedf0a7ad0273960e77d5fe6105f879",
        ),
        user_text="request-sentinel",
        assistant_text="response-sentinel",
        occurred_at=NOW,
        source_turn="source-turn-sentinel",
    )

    result = client.ingest_completed_turn(turn)

    assert result.accepted is True
    assert result.memory_ids == ["memory-add", "memory-update", "memory-delete"]
    assert [change.event for change in result.changes or []] == [
        "ADD",
        "UPDATE",
        "DELETE",
    ]
    request = requests[0]
    assert request.method == "POST"
    assert request.path == "/memories"
    assert request.timeout_seconds == 30.0
    assert request.headers == {"X-API-Key": "api-key-sentinel"}
    assert request.body == {
        "messages": [
            {"role": "user", "content": "request-sentinel"},
            {"role": "assistant", "content": "response-sentinel"},
        ],
        "user_id": "usr_3430bcf0b42b125b6c83834213a8e0aa",
        "agent_id": "agt_76635ea05f8a084b1c6c867bc39c9ae2",
        "run_id": "run_0eedf0a7ad0273960e77d5fe6105f879",
        "metadata": {
            "source": "runtime_turn_ingestion",
            "source_turn": "source-turn-sentinel",
            "occurred_at": "2026-08-07T12:00:00+00:00",
        },
    }
    assert "infer" not in request.body


def test_mem0_plugin_open_degrades_without_leaking_an_adapter_error() -> None:
    """Letting an unavailable Mem0 exception escape must break this test."""

    plugin = Mem0MemoryPlugin(client=UnavailableMem0Client())

    result = plugin.open_session(_open_request())

    assert result.status == "unavailable"
    assert result.initial_contribution is not None
    assert result.initial_contribution.status == "unavailable"
    assert result.initial_contribution.items == []
    assert [issue.code for issue in result.issues] == ["mem0_recall_failed"]


def test_mem0_plugin_ingestion_maps_native_failure_to_a_safe_issue() -> None:
    """Dropping Mem0's failed acceptance signal must break this test."""

    client = RecordingMem0Client(
        memories=[],
        ingestion_result=Mem0IngestionResult(
            accepted=False,
            errors=[{"code": "mem0_ingestion_failed"}],
        ),
    )
    plugin = Mem0MemoryPlugin(client=client)

    request = _ingestion_request()
    result = plugin.ingest_turn(request)
    repeated = plugin.ingest_turn(request)

    assert result.status == "failed"
    assert repeated == result
    assert len(client.ingested_turns) == 1
    assert result.changes == []
    assert [issue.code for issue in result.issues] == [
        "mem0_ingestion_failed"
    ]
    assert result.issues[0].recoverable is False


def test_mem0_plugin_ingestion_contains_an_adapter_exception() -> None:
    """Leaking a private adapter exception across the Plugin API must fail."""

    client = ExplodingIngestionMem0Client(memories=[])
    plugin = Mem0MemoryPlugin(client=client)

    request = _ingestion_request()
    result = plugin.ingest_turn(request)
    repeated = plugin.ingest_turn(request)

    assert result.status == "failed"
    assert repeated == result
    assert len(client.ingested_turns) == 1
    assert result.changes == []
    assert [issue.model_dump() for issue in result.issues] == [
        {
            "code": "mem0_ingestion_failed",
            "message": "mem0_ingestion_failed",
            "recoverable": False,
            "retry_after_seconds": None,
        }
    ]


def test_mem0_host_timeout_retry_reuses_the_terminal_outcome() -> None:
    """A Host timeout retry must not issue a second native write."""

    client = SlowIngestionMem0Client(
        memories=[],
        ingestion_result=Mem0IngestionResult(
            accepted=True,
            changes=[
                Mem0MemoryChange(
                    memory_id="memory-timeout-sentinel",
                    memory="timeout-content-sentinel",
                    event="ADD",
                )
            ],
        ),
    )
    host = _host_for_plugin(
        Mem0MemoryPlugin(client=client),
        execution_policy=MemoryPluginExecutionPolicy(
            ingest_turn_timeout_seconds=0.01
        ),
    )
    state = _completed_state()
    identity = RequestIdentity.for_user(
        user_id=state.user_id,
        agent_id=state.agent_id,
        session_id=state.session_id,
    )
    trace_store = InMemoryTraceStore()

    try:
        host.open_session(
            identity=identity,
            state=state,
            trace_store=trace_store,
        )
        assert host.schedule_ingestion(
            state=state,
            trace_store=trace_store,
        )
        assert host.drain(timeout=1.0)

        finished = next(
            event
            for event in trace_store.list_by_run(state.run_id)
            if event.canonical_event == "memory.ingestion.finished"
        )
        assert len(client.ingested_turns) == 1
        assert finished.status == "succeeded"
        assert finished.attributes["memory_plugin_retry_count"] == 1
    finally:
        assert host.close(timeout=1.0)


def test_mem0_plugin_singleflights_concurrent_duplicate_keys() -> None:
    """Concurrent duplicates must share the same native write and outcome."""

    client = BlockingIngestionMem0Client()
    plugin = Mem0MemoryPlugin(client=client)
    request = _ingestion_request()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(plugin.ingest_turn, request)
        try:
            assert client.started.wait(0.5)
            second = executor.submit(plugin.ingest_turn, request)
            sleep(0.02)
            assert len(client.ingested_turns) == 1
        finally:
            client.release.set()

        assert first.result(timeout=0.5).status == "accepted"
        assert second.result(timeout=0.5) == first.result(timeout=0.5)
        assert len(client.ingested_turns) == 1


def test_mem0_plugin_ledger_fails_closed_at_capacity_and_clears_on_close() -> None:
    """The bounded ledger must reject new writes until session cleanup."""

    client = RecordingMem0Client(memories=[])
    plugin = Mem0MemoryPlugin(client=client, max_ingestion_records=1)
    first_request = _ingestion_request()
    second_request = first_request.model_copy(
        update={"idempotency_key": "second-source-turn-sentinel"}
    )

    assert plugin.ingest_turn(first_request).status == "accepted"
    rejected = plugin.ingest_turn(second_request)
    repeated_rejection = plugin.ingest_turn(second_request)

    assert rejected.status == "failed"
    assert repeated_rejection.status == "failed"
    assert [issue.code for issue in rejected.issues] == [
        "mem0_ingestion_ledger_full"
    ]
    assert len(client.ingested_turns) == 1

    assert plugin.close_session(_close_request()).status == "closed"
    assert plugin.ingest_turn(second_request).status == "accepted"
    assert len(client.ingested_turns) == 2


def test_mem0_plugin_prepare_context_does_not_repeat_session_recall() -> None:
    """Adding per-turn Mem0 recall when refresh is disabled must fail."""

    client = RecordingMem0Client(memories=[])
    plugin = Mem0MemoryPlugin(client=client)

    result = plugin.prepare_context(_context_request())

    assert result.status == "succeeded"
    assert result.items == []
    assert client.recall_identities == []


def test_mem0_plugin_close_is_idempotent_and_has_no_native_side_effect() -> None:
    """Turning close into implicit Mem0 consolidation must fail this test."""

    plugin = Mem0MemoryPlugin(client=RecordingMem0Client(memories=[]))

    first = plugin.close_session(_close_request())
    second = plugin.close_session(_close_request())

    assert first.status == "closed"
    assert second.status == "closed"
    assert first.issues == []
    assert second.issues == []


def test_default_mem0_factory_stays_unavailable_in_mock_mode() -> None:
    """Constructing a network-capable client in mock mode must fail this test."""

    factories = default_memory_plugin_factories(
        ProviderConfig(
            provider_mode="mock",
            mem0_base_url="http://configured-but-offline.invalid",
            mem0_api_key="secret-sentinel",
        )
    )
    factory = factories[0]

    plugin = factory.build(_build_context("mock"), factory.config_model())

    assert len(factories) == 1
    assert plugin.descriptor.plugin_id == "mem0"
    assert plugin.descriptor.capabilities.modalities == {"text"}
    assert plugin.open_session(_open_request()).status == "unavailable"


def test_default_mem0_factory_builds_real_client_only_when_fully_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignoring explicit real mode plus base URL must break this test."""

    constructed: list[dict[str, Any]] = []

    class ConstructedMem0Client(RecordingMem0Client):
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)
            super().__init__(memories=[])

    monkeypatch.setattr(
        "assistant_agent.memory.plugins.builtin.mem0.Mem0Client",
        ConstructedMem0Client,
    )
    factory = default_memory_plugin_factories(
        ProviderConfig(
            provider_mode="real",
            chat_provider="qwen",
            qwen_api_key="chat-secret-sentinel",
            mem0_base_url="http://memory.invalid/",
            mem0_api_key="secret-sentinel",
            mem0_timeout_seconds=7.0,
        )
    )[0]

    plugin = factory.build(_build_context("real"), factory.config_model())

    assert plugin.open_session(_open_request()).status == "ready"
    assert constructed == [
        {
            "base_url": "http://memory.invalid/",
            "api_key": "secret-sentinel",
            "timeout_seconds": 7.0,
        }
    ]


def test_default_mem0_factory_requires_base_url_even_in_real_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real main Provider must not implicitly enable unconfigured Mem0."""

    def reject_real_client(**kwargs: Any) -> None:
        raise AssertionError("unconfigured-real-mem0-client-constructed")

    monkeypatch.setattr(
        "assistant_agent.memory.plugins.builtin.mem0.Mem0Client",
        reject_real_client,
    )
    factory = default_memory_plugin_factories(
        ProviderConfig(
            provider_mode="real",
            chat_provider="qwen",
            qwen_api_key="chat-secret-sentinel",
        )
    )[0]

    plugin = factory.build(_build_context("real"), factory.config_model())

    assert plugin.open_session(_open_request()).status == "unavailable"


def test_long_term_memory_service_facade_runs_the_host_lifecycle() -> None:
    """Restoring direct client/queue orchestration in the facade must fail."""

    client = RecordingMem0Client(
        memories=[
            LongTermMemory(
                memory_id="memory-sentinel",
                text="fact-sentinel",
                created_at=NOW,
            )
        ],
        ingestion_result=Mem0IngestionResult(
            accepted=True,
            changes=[
                Mem0MemoryChange(
                    memory_id="captured-sentinel",
                    memory="captured-sentinel",
                    event="ADD",
                )
            ],
        ),
    )
    host = _host_for_plugin(Mem0MemoryPlugin(client=client))
    service = LongTermMemoryService(host=host)
    state = _completed_state()
    identity = RequestIdentity.for_user(
        user_id=state.user_id,
        agent_id=state.agent_id,
        session_id=state.session_id,
    )

    snapshot = service.initialize_session(
        identity=identity,
        state=state,
        trace_store=None,
    )
    attached = service.attach_session_snapshot(state)
    prepared = service.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )
    queued = service.enqueue_completed_turn(state=state, trace_store=None)

    assert snapshot.plugin_id == "mem0"
    assert [item.memory_id for item in snapshot.memories] == [
        "memory-sentinel"
    ]
    assert attached == snapshot
    assert prepared == snapshot
    assert queued is True
    assert service.drain(timeout=1.0) is True
    assert len(client.ingested_turns) == 1
    assert service.clear_session(
        user_id=state.user_id,
        session_id=state.session_id,
    ) == 1
    assert service.close(timeout=1.0) is True


def test_default_composition_root_seals_offline_mem0_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bypassing assembly or constructing real Mem0 in mock mode must fail."""

    def reject_real_client(**kwargs: Any) -> None:
        raise AssertionError("real-mem0-client-constructed")

    monkeypatch.setattr(
        "assistant_agent.memory.plugins.builtin.mem0.Mem0Client",
        reject_real_client,
    )
    service = create_long_term_memory_service(
        ProviderConfig(
            provider_mode="mock",
            mem0_base_url="http://must-not-connect.invalid",
            mem0_api_key="must-not-resolve-sentinel",
        )
    )
    state = _completed_state()
    identity = RequestIdentity.for_user(
        user_id=state.user_id,
        agent_id=state.agent_id,
        session_id=state.session_id,
    )

    try:
        snapshot = service.initialize_session(
            identity=identity,
            state=state,
            trace_store=None,
        )
        report = service.host.registry.assembly_report

        assert report.active_slot == "mem0"
        assert report.records[0].source == "builtin:mem0"
        assert snapshot.status == "unavailable"
        assert snapshot.error_codes == ["mem0_recall_failed"]
        assert service.enqueue_completed_turn(
            state=state,
            trace_store=None,
        ) is False
        assert state.request.metadata["memory_ingestion"] == {
            "status": "skipped",
            "reason": "memory_plugin_unavailable",
        }
        assert service.ingestion_queue.pending_count == 0
    finally:
        assert service.close(timeout=1.0) is True


def test_explicit_mem0_module_config_replaces_the_builtin_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registering builtin Mem0 twice for an explicit config must fail."""

    monkeypatch.setenv("MEM0_BASE_URL", "http://configured.invalid")
    monkeypatch.setenv("MEM0_API_KEY", "secret-sentinel")
    config_path = tmp_path / "memory-plugins.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": "mem0",
                "plugins": {
                    "mem0": {
                        "enabled": True,
                        "module": (
                            "assistant_agent.memory.plugins.builtin.mem0"
                        ),
                        "config": {
                            "base_url": "${MEM0_BASE_URL}",
                            "api_key": "${MEM0_API_KEY}",
                            "timeout_seconds": 9.0,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    service = create_long_term_memory_service(
        ProviderConfig(
            provider_mode="mock",
            memory_plugin_config_path=str(config_path),
        )
    )

    try:
        report = service.host.registry.assembly_report
        assert report.active_slot == "mem0"
        assert [record.source for record in report.records] == [
            "module:assistant_agent.memory.plugins.builtin.mem0"
        ]
    finally:
        assert service.close(timeout=1.0) is True


def test_explicit_mem0_config_rejects_plaintext_api_key(
    tmp_path: Path,
) -> None:
    """A persisted plaintext Mem0 credential must fail closed and stay redacted."""

    plaintext_secret = "plaintext-secret-sentinel"
    config_path = tmp_path / "memory-plugins-plaintext.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": "mem0",
                "plugins": {
                    "mem0": {
                        "enabled": True,
                        "module": (
                            "assistant_agent.memory.plugins.builtin.mem0"
                        ),
                        "config": {
                            "base_url": "http://memory.invalid",
                            "api_key": plaintext_secret,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MemoryPluginAssemblyError) as captured:
        create_long_term_memory_service(
            ProviderConfig(
                provider_mode="mock",
                memory_plugin_config_path=str(config_path),
            )
        )

    issue_codes = [issue.code for issue in captured.value.report.issues]
    assert issue_codes == ["memory_plugin_config_invalid"]
    assert plaintext_secret not in str(captured.value)
    assert plaintext_secret not in captured.value.report.model_dump_json()
