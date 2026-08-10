from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.plugins.contracts import (
    MemoryChange,
    MemoryContextContribution,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    MemorySessionCloseResult,
    MemorySessionOpenResult,
    MemoryTurnIngestionResult,
)
from assistant_agent.memory.plugins.host import MemoryPluginHost
from assistant_agent.memory.plugins.media import ManagedMemoryMediaStore
from assistant_agent.memory.plugins.registry import (
    MemoryPluginRegistrationRecord,
    MemoryPluginRegistry,
)
from assistant_agent.memory.plugins.session_store import MemoryPluginSessionStore
from assistant_agent.memory.service import LongTermMemoryService
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import (
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


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


class BlockingMemoryPlugin:
    descriptor = MemoryPluginDescriptor(
        plugin_id="probe-memory",
        plugin_version="1",
        capabilities=MemoryPluginCapabilities(
            modalities={"text"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=False,
            supports_idempotent_ingestion=True,
        ),
    )

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def open_session(self, request) -> MemorySessionOpenResult:
        return MemorySessionOpenResult(
            status="ready",
            initial_contribution=MemoryContextContribution(
                status="succeeded"
            ),
        )

    def prepare_context(self, request) -> MemoryContextContribution:
        return MemoryContextContribution(status="succeeded")

    def ingest_turn(self, request) -> MemoryTurnIngestionResult:
        self.started.set()
        if not self.release.wait(2.0):
            raise TimeoutError("ingestion-sentinel")
        return MemoryTurnIngestionResult(
            status="accepted",
            changes=[
                MemoryChange(
                    operation="created",
                    memory_id="memory-sentinel",
                )
            ],
        )

    def close_session(self, request) -> MemorySessionCloseResult:
        return MemorySessionCloseResult(status="closed")


def _memory_host(plugin: BlockingMemoryPlugin) -> MemoryPluginHost:
    registry = MemoryPluginRegistry(
        records=[
            MemoryPluginRegistrationRecord(
                descriptor=plugin.descriptor,
                source="core-probe",
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
        ingestion_queue=MemoryIngestionQueue(
            max_workers=1,
            max_pending=2,
        ),
    )


@pytest.mark.core_invariant("OBS-001")
def test_runtime_returns_before_background_ingestion_finishes() -> None:
    plugin = BlockingMemoryPlugin()
    memory_service = LongTermMemoryService(host=_memory_host(plugin))
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="model-sentinel",
                    finish_reason="stop",
                    response_text="response-sentinel",
                )
            ]
        ),
        long_term_memory_service=memory_service,
        session_store=InMemorySessionStore(),
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        runtime.initialize_session_memory(
            RequestIdentity.for_user(
                user_id="user-sentinel",
                session_id="session-sentinel",
            )
        )
        future = executor.submit(
            runtime.run_state,
            UserRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="request-sentinel",
            ),
        )
        state = future.result(timeout=1.0)

        assert state.status == "completed"
        assert state.request.metadata["memory_ingestion"]["status"] == "queued"
        assert plugin.started.wait(0.5) is True
        assert memory_service.ingestion_queue.pending_count == 1
        canonical_events = [
            event.canonical_event
            for event in runtime.trace_store.list_by_run(state.run_id)
        ]
        assert canonical_events.index(
            "run.completed"
        ) < canonical_events.index("response.delivered")
        assert canonical_events.index(
            "response.delivered"
        ) < canonical_events.index("memory.ingestion.queued")
        assert "memory.ingestion.finished" not in canonical_events

        plugin.release.set()
        assert runtime.drain_memory_ingestions(timeout=1.0) is True
        ingestion = next(
            event
            for event in runtime.trace_store.list_by_run(state.run_id)
            if event.canonical_event == "memory.ingestion.finished"
        )
        assert ingestion.status == "succeeded"
        assert ingestion.attributes["memory_count"] == 1
    finally:
        plugin.release.set()
        runtime.close()
        executor.shutdown(wait=True)


@pytest.mark.core_invariant("DUR-001")
def test_ingestion_serializes_one_identity_and_parallelizes_others() -> None:
    queue = MemoryIngestionQueue(max_workers=2, max_pending=4)
    first_started = Event()
    first_release = Event()
    second_started = Event()
    other_started = Event()
    order: list[str] = []
    order_lock = Lock()

    def record(value: str) -> None:
        with order_lock:
            order.append(value)

    def first() -> None:
        record("first:start")
        first_started.set()
        first_release.wait(1.0)
        record("first:end")

    def second() -> None:
        record("second:start")
        second_started.set()

    def other() -> None:
        record("other:start")
        other_started.set()

    try:
        assert queue.submit(
            ordering_key=("identity-a",),
            callback=first,
        ).accepted
        assert first_started.wait(0.5)
        assert queue.submit(
            ordering_key=("identity-a",),
            callback=second,
        ).accepted
        assert queue.submit(
            ordering_key=("identity-b",),
            callback=other,
        ).accepted
        assert other_started.wait(0.5)
        assert not second_started.is_set()

        first_release.set()
        assert queue.drain(timeout=1.0)
        assert order.index("first:end") < order.index("second:start")
    finally:
        first_release.set()
        queue.close(timeout=1.0)


@pytest.mark.core_invariant("DUR-001")
def test_queue_close_drains_accepted_work() -> None:
    queue = MemoryIngestionQueue(max_workers=1, max_pending=1)
    ingestion_started = Event()
    ingestion_release = Event()
    ingestion_finished = Event()

    def ingest() -> None:
        ingestion_started.set()
        ingestion_release.wait(1.0)
        ingestion_finished.set()

    assert queue.submit(
        ordering_key=("identity-sentinel",),
        callback=ingest,
    ).accepted
    assert ingestion_started.wait(0.5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        closing = executor.submit(queue.close, timeout=1.0)
        assert not closing.done()
        ingestion_release.set()
        assert closing.result(timeout=1.0) is True

    assert ingestion_finished.is_set()
    rejected = queue.submit(
        ordering_key=("identity-sentinel",),
        callback=lambda: None,
    )
    assert rejected.accepted is False
    assert rejected.reason == "memory_ingestion_queue_closed"
