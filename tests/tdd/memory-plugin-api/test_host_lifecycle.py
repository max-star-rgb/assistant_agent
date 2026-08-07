from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from time import sleep
from typing import Any

import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.plugins.contracts import (
    ManagedMediaRef,
    MemoryContextContribution,
    MemoryContextItem,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    MemoryPluginExecutionPolicy,
    MemorySessionCloseResult,
    MemorySessionOpenResult,
    MemoryTurnIngestionResult,
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
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


class RecordingMemoryPlugin:
    def __init__(
        self,
        *,
        baseline: list[MemoryContextItem] | None = None,
        current: list[MemoryContextItem] | None = None,
        supports_context_refresh: bool = True,
        prepare_result: MemoryContextContribution | None = None,
    ) -> None:
        self.descriptor = _descriptor(supports_context_refresh=supports_context_refresh)
        self.baseline = list(baseline or [])
        self.current = list(current or [])
        self.prepare_result = prepare_result
        self.open_calls = 0
        self.prepare_calls = 0
        self.open_requests: list[Any] = []
        self.prepare_requests: list[Any] = []

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        self.open_calls += 1
        self.open_requests.append(request)
        return MemorySessionOpenResult(
            status="ready",
            session_handle="handle-sentinel",
            initial_contribution=MemoryContextContribution(
                items=self.baseline,
                status="succeeded",
            ),
        )

    def prepare_context(self, request: Any) -> MemoryContextContribution:
        self.prepare_calls += 1
        self.prepare_requests.append(request)
        if self.prepare_result is not None:
            return self.prepare_result
        return MemoryContextContribution(items=self.current, status="succeeded")

    def ingest_turn(self, request: Any) -> MemoryTurnIngestionResult:
        return MemoryTurnIngestionResult(status="accepted")

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        return MemorySessionCloseResult(status="closed")


class LateMemoryPlugin(RecordingMemoryPlugin):
    def __init__(self, *, release: Event) -> None:
        super().__init__(
            baseline=[_item("baseline", "baseline")],
            current=[_item("late", "late")],
        )
        self.release = release
        self.started = Event()

    def prepare_context(self, request: Any) -> MemoryContextContribution:
        self.prepare_calls += 1
        self.prepare_requests.append(request)
        self.started.set()
        self.release.wait(1.0)
        return MemoryContextContribution(items=self.current, status="succeeded")


class CancellationAwareMemoryPlugin(RecordingMemoryPlugin):
    def __init__(self) -> None:
        super().__init__(baseline=[_item("baseline", "baseline")])
        self.started = Event()
        self.observed_cancellation = Event()

    def prepare_context(self, request: Any) -> MemoryContextContribution:
        self.prepare_calls += 1
        self.prepare_requests.append(request)
        self.started.set()
        for _ in range(200):
            if request.cancellation.is_cancelled():
                self.observed_cancellation.set()
                break
            sleep(0.001)
        return MemoryContextContribution(
            items=[_item("cancelled-late", "cancelled-late")],
            status="succeeded",
        )


class ExplodingMemoryPlugin(RecordingMemoryPlugin):
    def prepare_context(self, request: Any) -> MemoryContextContribution:
        self.prepare_calls += 1
        self.prepare_requests.append(request)
        raise RuntimeError("secret-provider-response-sentinel")


class MutableCancelToken:
    def __init__(self) -> None:
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeError("cancelled-sentinel")


def test_host_opens_session_and_prepares_memory_once_per_run() -> None:
    plugin = RecordingMemoryPlugin(
        baseline=[
            _item("baseline-only", "baseline-only"),
            _item("shared", "baseline"),
        ],
        current=[_item("shared", "current"), _item("turn", "turn")],
    )
    host, _ = _host(plugin)
    state = _state(run_id="run-sentinel")

    opened = host.open_session(identity=_identity(), state=state, trace_store=None)
    reopened = host.open_session(identity=_identity(), state=state, trace_store=None)
    first = host.prepare_context(state=state, trace_store=None, cancel_token=None)
    second = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert plugin.open_calls == 1
    assert plugin.prepare_calls == 1
    assert opened == reopened
    assert first == second
    assert [(item.memory_id, item.text) for item in first.memories] == [
        ("baseline-only", "baseline-only"),
        ("shared", "current"),
        ("turn", "turn"),
    ]
    assert state.memory_context_prepared is True
    assert plugin.open_requests[0].deadline == NOW + timedelta(seconds=5)
    assert plugin.prepare_requests[0].deadline == NOW + timedelta(seconds=5)


def test_plugin_identity_matches_legacy_mem0_hash_and_ignores_metadata() -> None:
    identity = _identity()
    legacy = bind_mem0_identity(identity, namespace="assistant-agent")
    bound = bind_memory_plugin_identity(identity, namespace="assistant-agent")
    plugin = RecordingMemoryPlugin()
    host, _ = _host(plugin)
    state = _state(
        metadata={
            "memory_identity": {
                "user_id": "forged-user",
                "agent_id": "forged-agent",
                "session_id": "forged-session",
            },
            "user_id": "forged-user",
            "agent_id": "forged-agent",
        }
    )

    host.open_session(identity=identity, state=state, trace_store=None)

    assert (bound.user_id, bound.agent_id, bound.session_id) == (
        legacy.user_id,
        legacy.agent_id,
        legacy.run_id,
    )
    assert plugin.open_requests[0].identity == bound


def test_session_store_isolates_sessions_and_clears_only_requested_scope() -> None:
    plugin = RecordingMemoryPlugin()
    store = MemoryPluginSessionStore()
    host, _ = _host(plugin, session_store=store)
    first = _identity(session_id="session-a")
    second = _identity(session_id="session-b")
    third = _identity(user_id="user-b", session_id="session-a")

    host.open_session(
        identity=first, state=_state(session_id="session-a"), trace_store=None
    )
    host.open_session(
        identity=second, state=_state(session_id="session-b"), trace_store=None
    )
    host.open_session(
        identity=third,
        state=_state(user_id="user-b", session_id="session-a"),
        trace_store=None,
    )

    assert plugin.open_calls == 3
    assert store.get(first) is not None
    assert store.get(second) is not None
    assert store.get(third) is not None
    assert store.clear_session(user_id="user-sentinel", session_id="session-a") == 1
    assert store.get(first) is None
    assert store.get(second) is not None
    assert store.get(third) is not None
    assert store.clear_user(user_id="user-sentinel") == 1
    assert store.get(second) is None
    assert store.get(third) is not None


def test_host_does_not_prepare_when_plugin_has_no_context_refresh() -> None:
    plugin = RecordingMemoryPlugin(
        baseline=[_item("baseline", "baseline")],
        current=[_item("turn", "turn")],
        supports_context_refresh=False,
    )
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert plugin.prepare_calls == 0
    assert [(item.memory_id, item.text) for item in snapshot.memories] == [
        ("baseline", "baseline")
    ]


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "item-count",
        "char-count",
        "absolute-path",
        "inline-media",
        "metadata-json",
    ],
    ids=[
        "item-count",
        "char-count",
        "absolute-path",
        "inline-media",
        "metadata-json",
    ],
)
def test_invalid_contribution_is_rejected_atomically(
    invalid_kind: str,
) -> None:
    invalid_result = _invalid_contribution(invalid_kind)
    plugin = RecordingMemoryPlugin(
        baseline=[_item("baseline", "baseline")],
        prepare_result=invalid_result,
    )
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            max_context_items=1,
            max_context_chars=64 if invalid_kind == "char-count" else 1024,
        ),
    )
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert [(item.memory_id, item.text) for item in snapshot.memories] == [
        ("baseline", "baseline")
    ]
    assert snapshot.error_codes == ["memory_plugin_invalid_result"]


def test_contribution_rejects_cross_owner_media_atomically() -> None:
    media_store = ManagedMemoryMediaStore(max_total_bytes=1024)
    ref = media_store.register(
        owner_scope="other-owner",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"jpeg-sentinel",
    )
    plugin = RecordingMemoryPlugin(
        baseline=[_item("baseline", "baseline")],
        current=[
            _item("safe", "safe"),
            _item("unsafe-media", "unsafe-media", media_refs=[ref]),
        ],
    )
    host, _ = _host(plugin, media_store=media_store)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == ["memory_plugin_invalid_result"]


def test_host_passes_and_accepts_only_current_owner_managed_media() -> None:
    plugin = RecordingMemoryPlugin()
    host, media_store = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)
    owner_scope = plugin.open_requests[0].identity.session_id
    ref = media_store.register(
        owner_scope=owner_scope,
        media_type="image",
        mime_type="image/jpeg",
        payload=b"jpeg-sentinel",
    )
    state.request.image_ids = [ref.ref_id]
    plugin.current = [_item("visual", "visual", media_refs=[ref])]

    snapshot = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert plugin.prepare_requests[0].media_refs == [ref]
    assert snapshot.memories[0].media_refs == [ref]


def test_prepare_timeout_discards_late_result_and_freezes_baseline() -> None:
    release = Event()
    plugin = LateMemoryPlugin(release=release)
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            prepare_context_timeout_seconds=0.01
        ),
    )
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    first = host.prepare_context(state=state, trace_store=None, cancel_token=None)
    release.set()
    assert plugin.started.wait(0.2)
    sleep(0.03)
    second = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert [item.memory_id for item in first.memories] == ["baseline"]
    assert first.error_codes == ["memory_plugin_timeout"]
    assert second == first


def test_prepare_cancellation_is_cooperative_and_discards_result() -> None:
    plugin = CancellationAwareMemoryPlugin()
    host, _ = _host(plugin)
    state = _state()
    token = MutableCancelToken()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    def cancel_after_start() -> None:
        assert plugin.started.wait(0.2)
        token.cancelled = True

    canceller = Thread(target=cancel_after_start)
    canceller.start()
    snapshot = host.prepare_context(state=state, trace_store=None, cancel_token=token)
    canceller.join(0.2)

    assert plugin.observed_cancellation.wait(0.2)
    assert plugin.prepare_requests[0].cancellation.is_cancelled() is True
    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == ["memory_plugin_unavailable"]


def test_prepare_exception_degrades_to_stable_internal_issue() -> None:
    plugin = ExplodingMemoryPlugin(baseline=[_item("baseline", "baseline")])
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == ["memory_plugin_internal_error"]
    assert "secret-provider-response-sentinel" not in snapshot.model_dump_json()


def test_frozen_context_is_not_rewritten_by_plugin_or_caller_mutation() -> None:
    current = _item("turn", "turn")
    plugin = RecordingMemoryPlugin(current=[current])
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    first = host.prepare_context(state=state, trace_store=None, cancel_token=None)
    object.__setattr__(current, "text", "plugin-mutated")
    first.memories.append(_item("caller-mutated", "caller-mutated"))
    attached = host.attach_frozen_context(state=state)
    second = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert [(item.memory_id, item.text) for item in attached.memories] == [
        ("turn", "turn")
    ]
    assert second == attached


def _descriptor(*, supports_context_refresh: bool = True) -> MemoryPluginDescriptor:
    return MemoryPluginDescriptor(
        plugin_id="mem0",
        plugin_version="1.0.0",
        capabilities=MemoryPluginCapabilities(
            modalities={"text", "image"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=supports_context_refresh,
            supports_idempotent_ingestion=True,
        ),
    )


def _host(
    plugin: RecordingMemoryPlugin,
    *,
    session_store: MemoryPluginSessionStore | None = None,
    media_store: ManagedMemoryMediaStore | None = None,
    execution_policy: MemoryPluginExecutionPolicy | None = None,
) -> tuple[MemoryPluginHost, ManagedMemoryMediaStore]:
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
    resolved_media_store = media_store or ManagedMemoryMediaStore(
        max_total_bytes=1024 * 1024
    )
    return (
        MemoryPluginHost(
            registry=registry,
            session_store=session_store or MemoryPluginSessionStore(),
            media_store=resolved_media_store,
            execution_policy=execution_policy or MemoryPluginExecutionPolicy(),
            identity_namespace="assistant-agent",
            clock=lambda: NOW,
        ),
        resolved_media_store,
    )


def _identity(
    *,
    user_id: str = "user-sentinel",
    session_id: str = "session-sentinel",
) -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=user_id,
        agent_id="assistant-default",
        session_id=session_id,
    )


def _state(
    *,
    run_id: str = "run-sentinel",
    user_id: str = "user-sentinel",
    session_id: str = "session-sentinel",
    metadata: dict[str, Any] | None = None,
) -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id=user_id,
            session_id=session_id,
            text="request-sentinel",
            metadata=dict(metadata or {}),
        ),
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        agent_id="assistant-default",
    )


def _item(
    memory_id: str,
    text: str,
    *,
    media_refs: list[ManagedMediaRef] | None = None,
) -> MemoryContextItem:
    return MemoryContextItem(
        memory_id=memory_id,
        text=text,
        source="long_term",
        media_refs=list(media_refs or []),
    )


def _invalid_contribution(kind: str) -> MemoryContextContribution:
    if kind == "item-count":
        items = [_item("one", "one"), _item("two", "two")]
    elif kind == "char-count":
        items = [_item("too-long", "x" * 65)]
    elif kind == "absolute-path":
        items = [_item("unsafe-path", "/home/user/private-memory.txt")]
    elif kind == "inline-media":
        items = [_item("inline", "data:image/jpeg;base64," + "A" * 64)]
    else:
        items = [
            MemoryContextItem.model_construct(
                memory_id="bad-metadata",
                text="bad-metadata",
                source="long_term",
                relevance=None,
                occurred_at=None,
                created_at=None,
                media_refs=[],
                metadata={"payload": object()},
            )
        ]
    return MemoryContextContribution(items=items, status="succeeded")
