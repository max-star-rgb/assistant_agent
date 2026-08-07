from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Lock, Thread
from time import sleep
from typing import Any

import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.memory.plugins.contracts import (
    ManagedMediaRef,
    MemoryContextContribution,
    MemoryContextItem,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    MemoryPluginExecutionPolicy,
    MemoryPluginIssue,
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
from assistant_agent.memory.plugins.session_store import (
    MemoryPluginSessionRecord,
    MemoryPluginSessionStore,
    runtime_memory_identity_key,
)
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


class NonFiniteOpenIssuePlugin(RecordingMemoryPlugin):
    def open_session(self, request: Any) -> MemorySessionOpenResult:
        self.open_calls += 1
        self.open_requests.append(request)
        issue = MemoryPluginIssue.model_construct(
            code="plugin.retry",
            message="plugin.retry",
            recoverable=True,
            retry_after_seconds=float("inf"),
        )
        return MemorySessionOpenResult(
            status="ready",
            session_handle="handle-sentinel",
            issues=[issue],
        )


class BlockingPrepareMemoryPlugin(RecordingMemoryPlugin):
    def __init__(self) -> None:
        super().__init__(baseline=[_item("baseline", "baseline")])
        self._calls_lock = Lock()
        self.first_started = Event()
        self.second_started = Event()
        self.release = Event()

    def prepare_context(self, request: Any) -> MemoryContextContribution:
        with self._calls_lock:
            self.prepare_calls += 1
            call_number = self.prepare_calls
            self.prepare_requests.append(request)
        if call_number == 1:
            self.first_started.set()
        elif call_number == 2:
            self.second_started.set()
        self.release.wait(1.0)
        return MemoryContextContribution(
            items=[_item("turn", "turn")],
            status="succeeded",
        )


class DescriptorFailsAfterSealPlugin:
    def __init__(self, descriptor: MemoryPluginDescriptor) -> None:
        self._descriptor = descriptor
        self.descriptor_reads = 0
        self.delegate = RecordingMemoryPlugin(
            baseline=[_item("baseline", "baseline")],
            current=[_item("turn", "turn")],
        )

    @property
    def descriptor(self) -> MemoryPluginDescriptor:
        self.descriptor_reads += 1
        if self.descriptor_reads > 1:
            raise RuntimeError("secret-descriptor-getter-sentinel")
        return self._descriptor

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        return self.delegate.open_session(request)

    def prepare_context(self, request: Any) -> MemoryContextContribution:
        return self.delegate.prepare_context(request)

    def ingest_turn(self, request: Any) -> MemoryTurnIngestionResult:
        return self.delegate.ingest_turn(request)

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        return self.delegate.close_session(request)


class BlockingReadMemoryMediaStore(ManagedMemoryMediaStore):
    def __init__(self) -> None:
        super().__init__(max_total_bytes=1024 * 1024)
        self.validation_started = Event()
        self.release_validation = Event()

    def read(self, ref: ManagedMediaRef, **kwargs: Any) -> bytes:
        self.validation_started.set()
        assert self.release_validation.wait(0.5)
        return super().read(ref, **kwargs)


class ExplodingAssemblyReportRegistry(MemoryPluginRegistry):
    @property
    def assembly_report(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("secret-assembly-report-sentinel")


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


def test_concurrent_prepare_uses_one_single_flight_and_one_frozen_result() -> None:
    plugin = BlockingPrepareMemoryPlugin()
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)
    start = Barrier(3)
    results: list[SessionMemorySnapshot] = []
    failures: list[BaseException] = []

    def prepare() -> None:
        try:
            start.wait()
            results.append(
                host.prepare_context(
                    state=state,
                    trace_store=None,
                    cancel_token=None,
                )
            )
        except BaseException as error:
            failures.append(error)

    threads = [Thread(target=prepare), Thread(target=prepare)]
    for thread in threads:
        thread.start()
    start.wait()
    assert plugin.first_started.wait(0.5)
    # The bounded wait observes whether a forbidden second Plugin call starts
    # while the first call is intentionally held by ``release``.
    plugin.second_started.wait(0.1)
    plugin.release.set()
    for thread in threads:
        thread.join(0.5)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert plugin.prepare_calls == 1
    assert len(results) == 2
    assert results[0] == results[1]
    assert [item.memory_id for item in results[0].memories] == [
        "baseline",
        "turn",
    ]


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


def test_host_uses_sealed_descriptor_without_runtime_plugin_getter() -> None:
    descriptor = _descriptor()
    plugin = DescriptorFailsAfterSealPlugin(descriptor)
    registry = MemoryPluginRegistry(
        records=[
            MemoryPluginRegistrationRecord(
                descriptor=descriptor,
                source="test",
                enabled=True,
                active=True,
            )
        ],
        active_plugin=plugin,
    )
    host = MemoryPluginHost(
        registry=registry,
        session_store=MemoryPluginSessionStore(),
        media_store=ManagedMemoryMediaStore(max_total_bytes=1024 * 1024),
        clock=lambda: NOW,
    )
    state = _state()

    opened = host.open_session(
        identity=_identity(),
        state=state,
        trace_store=None,
    )
    prepared = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert plugin.descriptor_reads == 1
    assert [item.memory_id for item in opened.memories] == ["baseline"]
    assert [item.memory_id for item in prepared.memories] == ["baseline", "turn"]


def test_host_snapshot_failure_degrades_without_public_exception_details() -> None:
    descriptor = _descriptor()
    plugin = RecordingMemoryPlugin()
    registry = ExplodingAssemblyReportRegistry(
        records=[
            MemoryPluginRegistrationRecord(
                descriptor=descriptor,
                source="test",
                enabled=True,
                active=True,
            )
        ],
        active_plugin=plugin,
    )
    host = MemoryPluginHost(registry=registry, clock=lambda: NOW)
    state = _state()

    opened = host.open_session(
        identity=_identity(),
        state=state,
        trace_store=None,
    )
    prepared = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert opened.error_codes == ["memory_plugin_internal_error"]
    assert prepared.error_codes == ["memory_plugin_internal_error"]
    assert "secret-assembly-report-sentinel" not in opened.model_dump_json()
    assert "secret-assembly-report-sentinel" not in prepared.model_dump_json()


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


@pytest.mark.parametrize("clear_kind", ["session", "user"])
def test_session_store_clear_invalidates_inflight_loader_and_waiters(
    clear_kind: str,
) -> None:
    store = MemoryPluginSessionStore()
    identity = _identity()
    first_started = Event()
    release_first = Event()
    waiter_ready = Event()
    loader_calls = 0
    loader_lock = Lock()
    results: list[MemoryPluginSessionRecord] = []
    failures: list[BaseException] = []

    def loader() -> MemoryPluginSessionRecord:
        nonlocal loader_calls
        with loader_lock:
            loader_calls += 1
            call_number = loader_calls
        if call_number == 1:
            first_started.set()
            assert release_first.wait(0.5)
        return _session_record(
            identity,
            memory_id="stale" if call_number == 1 else "fresh",
        )

    def resolve(*, waiter: bool = False) -> None:
        try:
            if waiter:
                waiter_ready.set()
            results.append(store.resolve(identity, loader=loader).record)
        except BaseException as error:
            failures.append(error)

    first = Thread(target=resolve)
    waiter = Thread(target=resolve, kwargs={"waiter": True})
    first.start()
    assert first_started.wait(0.5)
    waiter.start()
    assert waiter_ready.wait(0.5)
    if clear_kind == "session":
        store.clear_session(
            user_id=identity.user_id,
            session_id=identity.session_id or "",
        )
    else:
        store.clear_user(user_id=identity.user_id)
    release_first.set()
    first.join(0.5)
    waiter.join(0.5)

    assert failures == []
    assert not first.is_alive()
    assert not waiter.is_alive()
    assert loader_calls == 2
    assert len(results) == 2
    assert all(record.baseline.memories[0].memory_id == "fresh" for record in results)
    retained = store.get(identity)
    assert retained is not None
    assert retained.baseline.memories[0].memory_id == "fresh"


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


@pytest.mark.parametrize(
    ("stop_kind", "expected_code"),
    [
        ("cancel", "memory_plugin_unavailable"),
        ("deadline", "memory_plugin_timeout"),
    ],
)
def test_prepare_discards_result_when_stopped_during_validation(
    stop_kind: str,
    expected_code: str,
) -> None:
    media_store = BlockingReadMemoryMediaStore()
    plugin = RecordingMemoryPlugin(baseline=[_item("baseline", "baseline")])
    host, _ = _host(
        plugin,
        media_store=media_store,
        execution_policy=MemoryPluginExecutionPolicy(
            prepare_context_timeout_seconds=0.05
        ),
    )
    state = _state()
    token = MutableCancelToken()
    host.open_session(identity=_identity(), state=state, trace_store=None)
    owner_scope = plugin.open_requests[0].identity.session_id
    ref = media_store.register(
        owner_scope=owner_scope,
        media_type="image",
        mime_type="image/jpeg",
        payload=b"jpeg-sentinel",
    )
    plugin.current = [_item("late-current", "late-current", media_refs=[ref])]

    def stop_during_validation() -> None:
        assert media_store.validation_started.wait(0.2)
        if stop_kind == "cancel":
            token.cancelled = True
        else:
            sleep(0.06)
        media_store.release_validation.set()

    stopper = Thread(target=stop_during_validation)
    stopper.start()
    snapshot = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=token,
    )
    stopper.join(0.2)

    assert not stopper.is_alive()
    assert plugin.prepare_calls == 1
    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == [expected_code]


def test_prepare_exception_degrades_to_stable_internal_issue() -> None:
    plugin = ExplodingMemoryPlugin(baseline=[_item("baseline", "baseline")])
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == ["memory_plugin_internal_error"]
    assert "secret-provider-response-sentinel" not in snapshot.model_dump_json()


def test_oversized_current_text_degrades_once_without_validation_error() -> None:
    plugin = RecordingMemoryPlugin(baseline=[_item("baseline", "baseline")])
    host, _ = _host(plugin)
    state = _state(text="x" * 20_001)
    host.open_session(identity=_identity(), state=state, trace_store=None)

    first = host.prepare_context(state=state, trace_store=None, cancel_token=None)
    second = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert plugin.prepare_calls == 0
    assert state.memory_context_prepared is True
    assert first == second
    assert [item.memory_id for item in first.memories] == ["baseline"]
    assert first.error_codes == ["memory_plugin_internal_error"]


@pytest.mark.parametrize(
    "unsafe_kind",
    [
        "embedded-path",
        "memory-id-key",
        "text-assignment",
        "nested-metadata-key",
        "nested-auth-key",
        "nested-metadata-value",
    ],
)
def test_credential_and_embedded_path_are_rejected_atomically(
    unsafe_kind: str,
) -> None:
    plugin = RecordingMemoryPlugin(
        baseline=[_item("baseline", "baseline")],
        prepare_result=_unsafe_contribution(unsafe_kind),
    )
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == ["memory_plugin_invalid_result"]


@pytest.mark.parametrize(
    "unsafe_kind",
    ["workspace-text-path", "metadata-key-path", "windows-unc-path"],
)
def test_arbitrary_absolute_paths_are_rejected_from_all_string_surfaces(
    unsafe_kind: str,
) -> None:
    plugin = RecordingMemoryPlugin(
        baseline=[_item("baseline", "baseline")],
        prepare_result=_absolute_path_contribution(unsafe_kind),
    )
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == ["memory_plugin_invalid_result"]


@pytest.mark.parametrize(
    "safe_text",
    [
        "https://example.com/workspace/reference",
        "https://example.com/search?q=/workspace/reference",
        "//example.com/workspace/reference",
        "docs/workspace/reference.md",
        "中文/English and/or",
    ],
)
def test_urls_and_relative_text_are_not_misclassified_as_absolute_paths(
    safe_text: str,
) -> None:
    plugin = RecordingMemoryPlugin(
        current=[
            MemoryContextItem(
                memory_id="safe",
                text=safe_text,
                source="long_term",
                metadata={safe_text: safe_text},
            )
        ]
    )
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert [item.memory_id for item in snapshot.memories] == ["safe"]
    assert snapshot.error_codes == []


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_raw_metadata_is_rejected_before_json_roundtrip(
    non_finite: float,
) -> None:
    plugin = RecordingMemoryPlugin(
        baseline=[_item("baseline", "baseline")],
        current=[
            MemoryContextItem(
                memory_id="non-finite",
                text="non-finite",
                source="long_term",
                metadata={"score": non_finite},
            )
        ],
    )
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == ["memory_plugin_invalid_result"]


@pytest.mark.parametrize(
    ("raw_field", "non_finite"),
    [
        ("item-relevance", float("nan")),
        ("item-relevance", float("inf")),
        ("item-relevance", float("-inf")),
        ("issue-retry", float("inf")),
    ],
)
def test_non_finite_anywhere_in_raw_result_is_rejected_before_roundtrip(
    raw_field: str,
    non_finite: float,
) -> None:
    if raw_field == "item-relevance":
        items = [
            MemoryContextItem.model_construct(
                memory_id="non-finite",
                text="non-finite",
                source="long_term",
                relevance=non_finite,
                occurred_at=None,
                created_at=None,
                media_refs=[],
                metadata={},
            )
        ]
        issues: list[MemoryPluginIssue] = []
    else:
        items = []
        issues = [
            MemoryPluginIssue.model_construct(
                code="plugin.retry",
                message="plugin.retry",
                recoverable=True,
                retry_after_seconds=non_finite,
            )
        ]
    plugin = RecordingMemoryPlugin(
        baseline=[_item("baseline", "baseline")],
        prepare_result=MemoryContextContribution(
            items=items,
            status="succeeded",
            issues=issues,
        ),
    )
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert [item.memory_id for item in snapshot.memories] == ["baseline"]
    assert snapshot.error_codes == ["memory_plugin_invalid_result"]


def test_non_finite_open_result_is_rejected_before_roundtrip() -> None:
    plugin = NonFiniteOpenIssuePlugin()
    host, _ = _host(plugin)

    snapshot = host.open_session(
        identity=_identity(),
        state=_state(),
        trace_store=None,
    )

    assert snapshot.memories == []
    assert snapshot.error_codes == ["memory_plugin_invalid_result"]


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


def test_state_frozen_copy_cannot_rewrite_host_authoritative_context() -> None:
    plugin = RecordingMemoryPlugin(current=[_item("turn", "turn")])
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)
    host.prepare_context(state=state, trace_store=None, cancel_token=None)
    assert state.frozen_memory_context is not None
    state.frozen_memory_context.memories.append(_item("forged", "forged"))
    assert state.session_memory_snapshot is not None
    state.session_memory_snapshot.memories.append(_item("forged-session", "forged"))

    attached = host.attach_frozen_context(state)

    assert attached is not None
    assert [item.memory_id for item in attached.memories] == ["turn"]


def test_run_memory_flags_are_excluded_from_state_serialization() -> None:
    state = _state()
    state.memory_context_prepared = True
    state.frozen_memory_context = SessionMemorySnapshot(
        memories=[_item("turn", "turn")]
    )

    dumped = state.model_dump(mode="python")

    assert "memory_context_prepared" not in dumped
    assert "frozen_memory_context" not in dumped


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
    text: str = "request-sentinel",
) -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id=user_id,
            session_id=session_id,
            text=text,
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


def _session_record(
    identity: RequestIdentity,
    *,
    memory_id: str,
) -> MemoryPluginSessionRecord:
    plugin_identity = bind_memory_plugin_identity(
        identity,
        namespace="assistant-agent",
    )
    return MemoryPluginSessionRecord(
        plugin_id="mem0",
        plugin_version="1.0.0",
        runtime_identity_key=runtime_memory_identity_key(identity),
        identity=plugin_identity,
        memory_session_id="memory-session-sentinel",
        session_handle="handle-sentinel",
        baseline=SessionMemorySnapshot(memories=[_item(memory_id, memory_id)]),
        status="ready",
    )


def _unsafe_contribution(kind: str) -> MemoryContextContribution:
    if kind == "embedded-path":
        item = _item("unsafe", "说明路径：/home/user/private-memory.txt")
    elif kind == "memory-id-key":
        item = _item("sk-secret-sentinel", "unsafe")
    elif kind == "text-assignment":
        item = _item("unsafe", "access_token=credential-sentinel")
    elif kind == "nested-metadata-key":
        item = MemoryContextItem(
            memory_id="unsafe",
            text="unsafe",
            source="long_term",
            metadata={"nested": {"service_access_token": "credential-sentinel"}},
        )
    elif kind == "nested-auth-key":
        item = MemoryContextItem(
            memory_id="unsafe",
            text="unsafe",
            source="long_term",
            metadata={"nested": {"service_auth": "credential-sentinel"}},
        )
    else:
        item = MemoryContextItem(
            memory_id="unsafe",
            text="unsafe",
            source="long_term",
            metadata={
                "nested": [{"value": "Authorization: Bearer credential-sentinel"}]
            },
        )
    return MemoryContextContribution(items=[item], status="succeeded")


def _absolute_path_contribution(kind: str) -> MemoryContextContribution:
    if kind == "workspace-text-path":
        item = _item("unsafe", "构建产物：/workspace/project/private.txt")
    elif kind == "metadata-key-path":
        item = MemoryContextItem(
            memory_id="unsafe",
            text="unsafe",
            source="long_term",
            metadata={"artifact /workspace/project/private.txt": "unsafe"},
        )
    else:
        item = _item("unsafe", r"share \\server\share\private.txt")
    return MemoryContextContribution(items=[item], status="succeeded")
