from __future__ import annotations

import asyncio
import json
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from assistant_agent.gateway.runtime_pool import GatewayRuntimePool
from assistant_agent.gateway.session import GatewaySessionManager
from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.plugins.contracts import (
    MemoryChange,
    MemoryContextContribution,
    MemoryContextItem,
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
from assistant_agent.media.video.visual_memory_index import (
    UnavailableVisualMemoryTextIndex,
)
from assistant_agent.runtime.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_models import SessionCreate
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import (
    CancelledToken,
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


class RecordingMemoryPlugin:
    def __init__(
        self,
        *,
        current: list[MemoryContextItem] | None = None,
        supports_ingestion: bool = False,
    ) -> None:
        self.descriptor = MemoryPluginDescriptor(
            plugin_id="recording-memory",
            plugin_version="1",
            capabilities=MemoryPluginCapabilities(
                modalities={"text", "image"},
                supports_session_recall=True,
                supports_turn_ingestion=supports_ingestion,
                supports_context_refresh=True,
                supports_idempotent_ingestion=True,
            ),
        )
        self.current = list(current or [])
        self.open_requests: list[Any] = []
        self.prepare_requests: list[Any] = []
        self.ingestion_requests: list[Any] = []
        self.close_requests: list[Any] = []

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        self.open_requests.append(request)
        return MemorySessionOpenResult(
            status="ready",
            session_handle=f"handle-{len(self.open_requests)}",
            initial_contribution=MemoryContextContribution(status="succeeded"),
        )

    def prepare_context(self, request: Any) -> MemoryContextContribution:
        self.prepare_requests.append(request)
        return MemoryContextContribution(
            items=list(self.current),
            status="succeeded",
        )

    def ingest_turn(self, request: Any) -> MemoryTurnIngestionResult:
        self.ingestion_requests.append(request)
        return MemoryTurnIngestionResult(
            status="accepted",
            changes=[
                MemoryChange(
                    operation="created",
                    memory_id="ingested-memory-sentinel",
                )
            ],
        )

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        self.close_requests.append(request)
        return MemorySessionCloseResult(status="closed")


class BlockingOpenMemoryPlugin(RecordingMemoryPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.open_started = Event()
        self.open_release = Event()

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        self.open_started.set()
        if not self.open_release.wait(timeout=1.0):
            raise AssertionError("test did not release blocked Plugin open")
        return super().open_session(request)


def _memory_item(
    memory_id: str,
    text: str,
    **kwargs: Any,
) -> MemoryContextItem:
    return MemoryContextItem(
        memory_id=memory_id,
        text=text,
        source="semantic",
        **kwargs,
    )


def _runtime_with_plugin(
    plugin: RecordingMemoryPlugin,
    *,
    chat_results: list[ChatResult] | None = None,
    media_store: ManagedMemoryMediaStore | None = None,
    session_store: InMemorySessionStore | None = None,
) -> tuple[AgentGraphRuntime, ScriptedChatAdapter, ManagedMemoryMediaStore]:
    resolved_media_store = media_store or ManagedMemoryMediaStore(
        max_total_bytes=1024 * 1024
    )
    memory_registry = MemoryPluginRegistry(
        records=[
            MemoryPluginRegistrationRecord(
                descriptor=plugin.descriptor,
                source="runtime-integration-test",
                enabled=True,
                active=True,
            )
        ],
        active_plugin=plugin,
    )
    memory_host = MemoryPluginHost(
        registry=memory_registry,
        session_store=MemoryPluginSessionStore(),
        media_store=resolved_media_store,
        ingestion_queue=MemoryIngestionQueue(max_workers=1, max_pending=8),
    )
    adapter = ScriptedChatAdapter(
        chat_results
        or [
            ChatResult(
                provider="scripted",
                model="model-sentinel",
                finish_reason="stop",
                response_text="response-sentinel",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        long_term_memory_service=LongTermMemoryService(host=memory_host),
        session_store=session_store or InMemorySessionStore(),
        visual_memory_text_index=UnavailableVisualMemoryTextIndex(
            code="offline-test",
            message="offline-test",
        ),
    )
    return runtime, adapter, resolved_media_store


def _identity(
    *,
    user_id: str = "user-sentinel",
    session_id: str = "session-sentinel",
) -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=user_id,
        session_id=session_id,
    )


def _request(
    *,
    user_id: str = "user-sentinel",
    session_id: str = "session-sentinel",
    image_ids: list[str] | None = None,
    task_execution_mode: str = "auto",
) -> UserRequest:
    return UserRequest(
        user_id=user_id,
        session_id=session_id,
        text="request-sentinel",
        image_ids=list(image_ids or []),
        task_execution_mode=task_execution_mode,
    )


def test_runtime_prepares_plugin_context_once_across_react_iterations() -> None:
    """Removing run-level prepare or freezing must lose the sentinel evidence."""

    plugin = RecordingMemoryPlugin()
    runtime, adapter, media_store = _runtime_with_plugin(
        plugin,
        chat_results=[
            ChatResult(
                provider="scripted",
                model="model-sentinel",
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
                model="model-sentinel",
                finish_reason="stop",
                response_text="response-sentinel",
            ),
        ],
    )
    try:
        runtime.initialize_session_memory(_identity())
        owner_scope = plugin.open_requests[0].identity.session_id
        media_ref = media_store.register(
            b"jpeg-bytes-sentinel",
            owner_scope=owner_scope,
            media_type="image",
            mime_type="image/jpeg",
        )
        plugin.current = [
            _memory_item(
                "memory-sentinel",
                "fact-sentinel",
                media_refs=[media_ref],
                metadata={"private_note": "metadata-sentinel"},
            )
        ]

        state = runtime.run_state(_request())

        assert state.status == "completed"
        assert len(plugin.prepare_requests) == 1
        assert state.memory_context_prepared is True
        assert state.frozen_memory_context is not None
        assert state.session_memory_snapshot is not None
        assert [
            item.memory_id for item in state.session_memory_snapshot.memories
        ] == ["memory-sentinel"]
        assert len(adapter.requests) == 2
        for compiled in adapter.requests:
            serialized_messages = json.dumps(
                compiled.messages,
                ensure_ascii=False,
                sort_keys=True,
            )
            assert "fact-sentinel" in serialized_messages
            assert "metadata-sentinel" not in serialized_messages
            assert media_ref.ref_id not in serialized_messages

        context_events = [
            event
            for event in runtime.trace_store.list_by_run(state.run_id)
            if event.canonical_event == "context.build.finished"
        ]
        assert len(context_events) == 2
        for event in context_events:
            report = event.output_summary["context_report_v2"]
            assert report["sections"]["memory"]["source"] == (
                "MemoryPluginHost.active_plugin"
            )
        assert runtime.long_term_memory_service.host._frozen_run_contexts == {}
        assert runtime.long_term_memory_service.host._run_epochs == {}
    finally:
        runtime.close()


def test_react_iterations_read_the_run_frozen_memory_copy() -> None:
    """Mutating the compatibility snapshot must not drift later LLM context."""

    plugin = RecordingMemoryPlugin(
        current=[_memory_item("memory-sentinel", "fact-sentinel")]
    )
    runtime, adapter, _ = _runtime_with_plugin(
        plugin,
        chat_results=[
            ChatResult(
                provider="scripted",
                model="model-sentinel",
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
                model="model-sentinel",
                finish_reason="stop",
                response_text="response-sentinel",
            ),
        ],
    )
    original_build = runtime.context_service.build
    build_count = 0

    def build_then_mutate_compatibility_snapshot(**kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal build_count
        context = original_build(**kwargs)
        build_count += 1
        if build_count == 1:
            state = kwargs["state"]
            assert state.session_memory_snapshot is not None
            state.session_memory_snapshot.memories[:] = [
                _memory_item("forged-memory", "forged-fact")
            ]
        return context

    runtime.context_service.build = build_then_mutate_compatibility_snapshot  # type: ignore[method-assign]
    try:
        runtime.initialize_session_memory(_identity())

        state = runtime.run_state(_request())

        assert state.status == "completed"
        assert len(plugin.prepare_requests) == 1
        assert state.frozen_memory_context is not None
        assert [
            item.memory_id for item in state.frozen_memory_context.memories
        ] == ["memory-sentinel"]
        assert len(adapter.requests) == 2
        second_messages = json.dumps(
            adapter.requests[1].messages,
            ensure_ascii=False,
            sort_keys=True,
        )
        assert "fact-sentinel" in second_messages
        assert "forged-fact" not in second_messages
    finally:
        runtime.close()


def test_runtime_passes_only_owner_bound_managed_media_refs() -> None:
    """A cross-user ref must not reach the Plugin or fail the Agent run."""

    plugin = RecordingMemoryPlugin()
    runtime, _, media_store = _runtime_with_plugin(
        plugin,
        chat_results=[
            ChatResult(
                provider="scripted",
                model="model-sentinel",
                finish_reason="stop",
                response_text="first-response-sentinel",
            ),
            ChatResult(
                provider="scripted",
                model="model-sentinel",
                finish_reason="stop",
                response_text="second-response-sentinel",
            ),
        ],
    )
    try:
        runtime.initialize_session_memory(_identity())
        runtime.initialize_session_memory(
            _identity(user_id="other-user", session_id="other-session")
        )
        first_owner_scope = plugin.open_requests[0].identity.session_id
        media_ref = media_store.register(
            b"jpeg-bytes-sentinel",
            owner_scope=first_owner_scope,
            media_type="image",
            mime_type="image/jpeg",
        )

        first = runtime.run_state(_request(image_ids=[media_ref.ref_id]))
        second = runtime.run_state(
            _request(
                user_id="other-user",
                session_id="other-session",
                image_ids=[media_ref.ref_id],
            )
        )

        assert first.status == "completed"
        assert second.status == "completed"
        assert [
            ref.ref_id for ref in plugin.prepare_requests[0].media_refs
        ] == [media_ref.ref_id]
        assert plugin.prepare_requests[1].media_refs == []
        assert second.session_memory_snapshot is not None
        assert "memory_media_owner_mismatch" in (
            second.session_memory_snapshot.error_codes
        )
    finally:
        runtime.close()


def test_completed_run_delivers_before_plugin_ingestion_is_queued() -> None:
    """Moving ingestion before delivery must violate the canonical event order."""

    plugin = RecordingMemoryPlugin(supports_ingestion=True)
    runtime, _, _ = _runtime_with_plugin(plugin)
    try:
        runtime.initialize_session_memory(_identity())

        state = runtime.run_state(_request())
        assert runtime.drain_memory_ingestions(timeout=1.0) is True

        canonical_events = [
            event.canonical_event
            for event in runtime.trace_store.list_by_run(state.run_id)
        ]
        assert canonical_events.index("run.completed") < canonical_events.index(
            "response.delivered"
        )
        assert canonical_events.index(
            "response.delivered"
        ) < canonical_events.index("memory.ingestion.queued")
        assert len(plugin.ingestion_requests) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("terminal_kind", ["failed", "cancelled"])
def test_non_completed_run_does_not_ingest_memory(terminal_kind: str) -> None:
    """Removing the completed-terminal guard must write failed/cancelled turns."""

    plugin = RecordingMemoryPlugin(supports_ingestion=True)
    runtime, _, _ = _runtime_with_plugin(plugin)
    try:
        runtime.initialize_session_memory(_identity())
        request = _request(
            task_execution_mode=("durable" if terminal_kind == "failed" else "auto")
        )

        state = runtime.run_state(
            request,
            cancel_token=CancelledToken() if terminal_kind == "cancelled" else None,
        )
        assert runtime.drain_memory_ingestions(timeout=0.2) is True

        assert state.status == terminal_kind
        assert plugin.ingestion_requests == []
        assert "memory.ingestion.queued" not in [
            event.canonical_event
            for event in runtime.trace_store.list_by_run(state.run_id)
        ]
        assert runtime.long_term_memory_service.host._frozen_run_contexts == {}
        assert runtime.long_term_memory_service.host._run_epochs == {}
    finally:
        runtime.close()


def test_unexpected_run_exception_releases_host_frozen_context() -> None:
    """Run cleanup must release Host state even when graph execution escapes."""

    plugin = RecordingMemoryPlugin(
        current=[_memory_item("memory-sentinel", "fact-sentinel")]
    )
    runtime, adapter, _ = _runtime_with_plugin(plugin)

    def raise_unexpected(_request: Any) -> ChatResult:
        raise RuntimeError("unexpected-chat-sentinel")

    adapter.chat = raise_unexpected  # type: ignore[method-assign]
    try:
        runtime.initialize_session_memory(_identity())

        with pytest.raises(RuntimeError, match="unexpected-chat-sentinel"):
            runtime.run_state(_request(), run_id="run-exception-sentinel")

        assert runtime.long_term_memory_service.host._frozen_run_contexts == {}
        assert runtime.long_term_memory_service.host._run_epochs == {}
    finally:
        runtime.close()


def test_runtime_reset_keeps_signature_and_closes_old_plugin_session() -> None:
    """Dropping reset propagation must leak the prior opaque Plugin handle."""

    plugin = RecordingMemoryPlugin()
    runtime, _, _ = _runtime_with_plugin(plugin)
    try:
        runtime.initialize_session_memory(_identity())
        runtime.initialize_session_memory(_identity(), reset=True)

        assert len(plugin.open_requests) == 2
        assert [request.reason for request in plugin.close_requests] == ["reset"]
    finally:
        runtime.close()


def test_assistant_runtime_app_delete_closes_then_clears_memory_session() -> None:
    """Deleting runtime data must close Host state without remote memory CRUD."""

    plugin = RecordingMemoryPlugin()
    session_store = InMemorySessionStore()
    runtime, _, _ = _runtime_with_plugin(plugin, session_store=session_store)
    app = AssistantRuntimeApp(lambda: runtime)
    try:
        record = app.create_session(SessionCreate(user_id="user-sentinel"))

        deleted = app.delete_session(record.user_id, record.session_id)

        assert deleted is True
        assert [request.reason for request in plugin.close_requests] == ["reset"]
        assert runtime.long_term_memory_service.host.session_store.list_records() == []
    finally:
        runtime.close()


def test_assistant_runtime_app_user_delete_closes_each_memory_session() -> None:
    """Bulk deletion must include Host-only sessions absent from Runtime index."""

    plugin = RecordingMemoryPlugin()
    session_store = InMemorySessionStore()
    runtime, _, _ = _runtime_with_plugin(plugin, session_store=session_store)
    app = AssistantRuntimeApp(lambda: runtime)
    try:
        indexed = app.create_session(SessionCreate(user_id="user-sentinel"))
        host_only = _identity(session_id="host-only-session-sentinel")
        runtime.initialize_session_memory(host_only)

        deleted = app.delete_user_runtime_data("user-sentinel")

        assert deleted["session_records"] == 1
        assert {request.reason for request in plugin.close_requests} == {"reset"}
        assert len(plugin.close_requests) == 2
        assert {
            request.identity.session_id for request in plugin.close_requests
        } == {request.identity.session_id for request in plugin.open_requests}
        assert indexed.session_id != host_only.session_id
        assert runtime.long_term_memory_service.host.session_store.list_records() == []
        assert (
            runtime.long_term_memory_service.host.session_store.list_retired_records()
            == []
        )
    finally:
        runtime.close()


def test_gateway_runtime_pool_keeps_session_initialization_contract() -> None:
    """Gateway initialization must keep delegating through the shared Runtime."""

    plugin = RecordingMemoryPlugin()
    runtime, _, _ = _runtime_with_plugin(plugin)
    pool = GatewayRuntimePool(
        max_runtime_instances=1,
        runtime_factory=lambda: runtime,
        runtime_cleanup=lambda active_runtime: active_runtime.close(),
    )
    try:
        result = pool.initialize_session_memory(_identity())

        assert result is None
        assert len(plugin.open_requests) == 1
    finally:
        pool.close()


def test_agent_service_session_config_reaches_memory_plugin_as_trusted_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_agent.api import agent_service_websocket, routes_agent

    plugin = RecordingMemoryPlugin()
    runtime, _, _ = _runtime_with_plugin(plugin)
    monkeypatch.setattr(
        routes_agent,
        "get_assistant_runtime_app",
        lambda: SimpleNamespace(runtime=runtime),
    )
    try:
        asyncio.run(
            agent_service_websocket._initialize_agent_service_session_memory(
                "user-sentinel",
                "session-sentinel",
                {"entry_profile": "agent_service"},
            )
        )

        assert plugin.open_requests[0].entry_profile == "agent_service"
    finally:
        runtime.close()


def test_public_metadata_cannot_supply_memory_entry_profile_or_session_config() -> None:
    from assistant_agent.api.routes_agent import _public_request_metadata
    from assistant_agent.gateway.session import _user_message_metadata

    metadata = {
        "entry_profile": "forged-profile",
        "gateway": {
            "session_config": {"entry_profile": "forged-profile"},
            "safe-sentinel": "value-sentinel",
        },
    }

    rest_metadata = _public_request_metadata(metadata)
    gateway_metadata = _user_message_metadata({"metadata": metadata})

    assert "entry_profile" not in rest_metadata
    assert "session_config" not in rest_metadata["gateway"]
    assert rest_metadata["gateway"]["safe-sentinel"] == "value-sentinel"
    assert "entry_profile" not in gateway_metadata
    assert "session_config" not in gateway_metadata["gateway"]
    assert gateway_metadata["gateway"]["safe-sentinel"] == "value-sentinel"


@pytest.mark.parametrize(
    ("normalizer_name", "payload"),
    [
        (
            "incoming",
            {
                "config": {
                    "entry_profile": "agent_service",
                    "language": "zh-CN",
                }
            },
        ),
        (
            "update",
            {
                "config": {
                    "entry_profile": "agent_service",
                    "language": "zh-CN",
                }
            },
        ),
        (
            "update",
            {"key": "entry_profile", "value": "agent_service"},
        ),
    ],
)
def test_external_gateway_config_cannot_set_memory_entry_profile(
    normalizer_name: str,
    payload: dict[str, Any],
) -> None:
    from assistant_agent.gateway.bridge import (
        _config_from_payload,
        _config_update_values,
    )

    normalizer = (
        _config_from_payload
        if normalizer_name == "incoming"
        else _config_update_values
    )

    normalized = normalizer(payload)

    assert "entry_profile" not in normalized
    if "config" in payload and "language" in payload["config"]:
        assert normalized["language"] == "zh-CN"


def test_gateway_destroy_finalizes_initialized_memory_session() -> None:
    """Destroy before any turn must close the Plugin handle and clear Host state."""

    asyncio.run(_assert_gateway_destroy_finalizes_initialized_memory_session())


async def _assert_gateway_destroy_finalizes_initialized_memory_session() -> None:
    plugin = RecordingMemoryPlugin()
    runtime, _, _ = _runtime_with_plugin(plugin)
    pool = GatewayRuntimePool(
        max_runtime_instances=1,
        runtime_factory=lambda: runtime,
        runtime_cleanup=lambda active_runtime: active_runtime.close(),
    )
    manager = _gateway_manager_with_memory_pool(pool)
    try:
        await manager.acquire(user_id="user-sentinel")
        await manager.initialize_session(
            user_id="user-sentinel",
            session_id="session-sentinel",
        )

        destroyed = await manager.destroy("user-sentinel")

        assert destroyed is True
        assert [request.reason for request in plugin.close_requests] == ["reset"]
        assert runtime.long_term_memory_service.host.session_store.list_records() == []
        assert (
            runtime.long_term_memory_service.host.session_store.list_retired_records()
            == []
        )
    finally:
        await manager.close()
        pool.close()


def test_gateway_destroy_finalizes_cancelled_inflight_initialization() -> None:
    """Cancelling the asyncio wrapper must still reap a late blocking open."""

    asyncio.run(_assert_gateway_destroy_finalizes_cancelled_inflight_initialization())


async def _assert_gateway_destroy_finalizes_cancelled_inflight_initialization() -> None:
    plugin = BlockingOpenMemoryPlugin()
    runtime, _, _ = _runtime_with_plugin(plugin)
    pool = GatewayRuntimePool(
        max_runtime_instances=1,
        runtime_factory=lambda: runtime,
        runtime_cleanup=lambda active_runtime: active_runtime.close(),
    )
    manager = _gateway_manager_with_memory_pool(pool)
    initialize_task: asyncio.Task[None] | None = None
    try:
        await manager.acquire(user_id="user-sentinel")
        initialize_task = asyncio.create_task(
            manager.initialize_session(
                user_id="user-sentinel",
                session_id="session-sentinel",
            )
        )
        assert await asyncio.to_thread(plugin.open_started.wait, 1.0) is True

        destroy_task = asyncio.create_task(manager.destroy("user-sentinel"))
        await asyncio.sleep(0)
        plugin.open_release.set()
        destroyed = await destroy_task
        initialize_outcomes = await asyncio.gather(
            initialize_task,
            return_exceptions=True,
        )

        assert destroyed is True
        assert isinstance(initialize_outcomes[0], asyncio.CancelledError)
        assert len(plugin.open_requests) == 1
        assert [request.reason for request in plugin.close_requests] == ["reset"]
        assert runtime.long_term_memory_service.host.session_store.list_records() == []
        assert (
            runtime.long_term_memory_service.host.session_store.list_retired_records()
            == []
        )
    finally:
        plugin.open_release.set()
        if initialize_task is not None:
            initialize_task.cancel()
            await asyncio.gather(initialize_task, return_exceptions=True)
        await manager.close()
        pool.close()


def test_gateway_expiry_and_shutdown_forward_memory_close_reasons() -> None:
    """Idle eviction and manager shutdown must preserve lifecycle reasons."""

    asyncio.run(_assert_gateway_expiry_and_shutdown_forward_memory_close_reasons())


async def _assert_gateway_expiry_and_shutdown_forward_memory_close_reasons() -> None:
    finalized: list[tuple[str, str, str]] = []

    async def initialize(
        _user_id: str,
        _session_id: str,
        _config: Any,
    ) -> None:
        return None

    async def finalize(user_id: str, session_id: str, reason: str) -> None:
        finalized.append((user_id, session_id, reason))

    manager = GatewaySessionManager(
        idle_timeout_s=0.0,
        start_reaper=False,
        session_initializer=initialize,
        session_finalizer=finalize,
    )
    await manager.acquire(user_id="expired-user-sentinel")
    await manager.initialize_session(
        user_id="expired-user-sentinel",
        session_id="expired-session-sentinel",
    )

    assert await manager.reap_once() == ["expired-user-sentinel"]

    await manager.acquire(user_id="shutdown-user-sentinel")
    await manager.initialize_session(
        user_id="shutdown-user-sentinel",
        session_id="shutdown-session-sentinel",
    )
    await manager.close()

    assert finalized == [
        (
            "expired-user-sentinel",
            "expired-session-sentinel",
            "expired",
        ),
        (
            "shutdown-user-sentinel",
            "shutdown-session-sentinel",
            "shutdown",
        ),
    ]


def test_gateway_service_close_error_still_runs_memory_finalizer() -> None:
    """Transport cleanup failure must not bypass Memory session finalization."""

    asyncio.run(_assert_gateway_service_close_error_still_runs_memory_finalizer())


async def _assert_gateway_service_close_error_still_runs_memory_finalizer() -> None:
    finalized: list[tuple[str, str, str]] = []

    async def finalize(user_id: str, session_id: str, reason: str) -> None:
        finalized.append((user_id, session_id, reason))

    manager = GatewaySessionManager(
        start_reaper=False,
        session_finalizer=finalize,
    )
    await manager.acquire(user_id="user-sentinel")
    await manager.initialize_session(
        user_id="user-sentinel",
        session_id="session-sentinel",
    )
    entry = manager._entries["user-sentinel"]
    original_close = entry.service.close

    async def close_then_raise(*, source: str = "gateway_disconnect") -> None:
        entry.stop()
        await original_close(source=source)
        raise RuntimeError("service-close-sentinel")

    entry.service.close = close_then_raise  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="service-close-sentinel"):
            await manager.destroy("user-sentinel")

        assert finalized == [
            ("user-sentinel", "session-sentinel", "reset")
        ]
    finally:
        entry.stop()
        if entry.task is not None:
            await asyncio.gather(entry.task, return_exceptions=True)
        await entry.gateway_ep.close()
        await entry.session_ep.close()
        await manager.close()


def test_concurrent_gateway_destroy_keeps_session_initialization_blocked() -> None:
    """A second destroy must not clear another finalizer's admission barrier."""

    asyncio.run(_assert_concurrent_gateway_destroy_keeps_initialization_blocked())


async def _assert_concurrent_gateway_destroy_keeps_initialization_blocked() -> None:
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()
    finalized: list[tuple[str, str, str]] = []

    async def finalize(user_id: str, session_id: str, reason: str) -> None:
        finalized.append((user_id, session_id, reason))
        finalizer_started.set()
        await release_finalizer.wait()

    manager = GatewaySessionManager(
        start_reaper=False,
        session_finalizer=finalize,
    )
    first_destroy: asyncio.Task[bool] | None = None
    try:
        await manager.acquire(user_id="user-sentinel")
        await manager.initialize_session(
            user_id="user-sentinel",
            session_id="session-sentinel",
        )
        first_destroy = asyncio.create_task(manager.destroy("user-sentinel"))
        await asyncio.wait_for(finalizer_started.wait(), timeout=1.0)

        assert await manager.destroy("user-sentinel") is False
        with pytest.raises(RuntimeError, match="gateway_session_destroying"):
            await manager.acquire(user_id="user-sentinel")
        with pytest.raises(RuntimeError, match="gateway_session_destroying"):
            await manager.initialize_session(
                user_id="user-sentinel",
                session_id="late-session-sentinel",
            )

        release_finalizer.set()
        assert await first_destroy is True
        assert finalized == [
            ("user-sentinel", "session-sentinel", "reset")
        ]
        resumed = await manager.acquire(user_id="user-sentinel")
        assert resumed.created is True
    finally:
        release_finalizer.set()
        if first_destroy is not None:
            await asyncio.gather(first_destroy, return_exceptions=True)
        await manager.close()


def _gateway_manager_with_memory_pool(
    pool: GatewayRuntimePool,
) -> GatewaySessionManager:
    async def initialize(
        user_id: str,
        session_id: str,
        _config: Any,
    ) -> None:
        await asyncio.to_thread(
            pool.initialize_session_memory,
            _identity(user_id=user_id, session_id=session_id),
        )

    async def finalize(user_id: str, session_id: str, reason: str) -> None:
        await asyncio.to_thread(
            pool.finalize_session_memory,
            _identity(user_id=user_id, session_id=session_id),
            reason=reason,
        )

    return GatewaySessionManager(
        start_reaper=False,
        session_initializer=initialize,
        session_finalizer=finalize,
    )
