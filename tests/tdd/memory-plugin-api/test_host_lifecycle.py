from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, Thread
from time import monotonic, sleep
from typing import Any

import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.memory.observability import (
    record_ingestion_finished,
    record_session_recall,
)
from assistant_agent.memory.plugins.contracts import (
    ManagedMediaRef,
    MemoryChange,
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
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.ids import VISUAL_REMINDER_MANAGE_TOOL_NAME
from assistant_agent.tools.models import ToolResult


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
        self.close_requests: list[Any] = []

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
        self.close_requests.append(request)
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


class DeepOpenResultPlugin(RecordingMemoryPlugin):
    def __init__(self, *, depth: int) -> None:
        super().__init__()
        metadata: dict[str, Any] = {"leaf": "safe"}
        for _ in range(depth):
            metadata = {"nested": metadata}
        item = MemoryContextItem.model_construct(
            memory_id="deep",
            text="deep",
            source="long_term",
            relevance=None,
            occurred_at=None,
            created_at=None,
            media_refs=[],
            metadata=metadata,
        )
        contribution = MemoryContextContribution.model_construct(
            items=[item],
            status="succeeded",
            issues=[],
        )
        self.open_result = MemorySessionOpenResult.model_construct(
            status="ready",
            session_handle="handle-sentinel",
            initial_contribution=contribution,
            issues=[],
        )

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        self.open_calls += 1
        self.open_requests.append(request)
        return self.open_result


@dataclass(frozen=True)
class DataclassScore:
    score: float


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


class BlockingOpenMemoryPlugin(RecordingMemoryPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.open_started = Event()
        self.open_release = Event()
        self.close_requests: list[Any] = []

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        self.open_calls += 1
        self.open_requests.append(request)
        self.open_started.set()
        assert self.open_release.wait(1.0)
        return MemorySessionOpenResult(
            status="ready",
            session_handle="late-handle-sentinel",
        )

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        self.close_requests.append(request)
        return MemorySessionCloseResult(status="closed")


class BlockingConcurrentOpenMemoryPlugin(RecordingMemoryPlugin):
    def __init__(self, *, expected_open_calls: int = 2) -> None:
        super().__init__()
        self._open_lock = Lock()
        self.expected_open_calls = expected_open_calls
        self.first_open_started = Event()
        self.second_open_started = Event()
        self.expected_opens_started = Event()
        self.open_release = Event()

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        with self._open_lock:
            self.open_calls += 1
            call_number = self.open_calls
            self.open_requests.append(request)
        if call_number == 1:
            self.first_open_started.set()
        elif call_number == 2:
            self.second_open_started.set()
        if call_number >= self.expected_open_calls:
            self.expected_opens_started.set()
        assert self.open_release.wait(1.0)
        return MemorySessionOpenResult(
            status="ready",
            session_handle=f"concurrent-handle-{call_number}",
        )


class RetryableLateCloseMemoryPlugin(BlockingOpenMemoryPlugin):
    def __init__(
        self,
        *,
        first_status: str = "partial",
        block_retry: bool = True,
    ) -> None:
        super().__init__()
        self.first_status = first_status
        self.block_retry = block_retry
        self._close_lock = Lock()
        self.retry_started = Event()
        self.retry_release = Event()

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        with self._close_lock:
            self.close_requests.append(request)
            call_number = len(self.close_requests)
        if call_number == 1:
            return MemorySessionCloseResult(
                status=self.first_status,
                issues=[
                    MemoryPluginIssue(
                        code="plugin.retry",
                        message="plugin.retry",
                        recoverable=True,
                    )
                ],
            )
        self.retry_started.set()
        if self.block_retry:
            assert self.retry_release.wait(1.0)
        return MemorySessionCloseResult(status="closed")


class TimingOutLateCloseMemoryPlugin(BlockingOpenMemoryPlugin):
    def __init__(
        self,
        *,
        first_close_status: str = "closed",
        block_retry: bool = False,
    ) -> None:
        super().__init__()
        self.first_close_status = first_close_status
        self.block_retry = block_retry
        self._close_lock = Lock()
        self.first_close_started = Event()
        self.first_close_release = Event()
        self.first_close_finished = Event()
        self.retry_started = Event()
        self.retry_release = Event()

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        with self._close_lock:
            self.close_requests.append(request)
            call_number = len(self.close_requests)
        if call_number == 1:
            self.first_close_started.set()
            try:
                assert self.first_close_release.wait(1.0)
            finally:
                self.first_close_finished.set()
            if self.first_close_status != "closed":
                return MemorySessionCloseResult(
                    status=self.first_close_status,
                    issues=[
                        MemoryPluginIssue(
                            code="plugin.retry",
                            message="plugin.retry",
                            recoverable=True,
                        )
                    ],
                )
        if call_number == 2:
            self.retry_started.set()
            if self.block_retry:
                assert self.retry_release.wait(1.0)
        return MemorySessionCloseResult(status="closed")


class SequencedBlockingOpenMemoryPlugin(RecordingMemoryPlugin):
    def __init__(self) -> None:
        super().__init__()
        self._open_lock = Lock()
        self.first_open_started = Event()
        self.first_open_release = Event()
        self.close_requests: list[Any] = []

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        with self._open_lock:
            self.open_calls += 1
            call_number = self.open_calls
            self.open_requests.append(request)
        if call_number == 1:
            self.first_open_started.set()
            assert self.first_open_release.wait(1.0)
        return MemorySessionOpenResult(
            status="ready",
            session_handle=f"sequenced-handle-{call_number}",
        )

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        self.close_requests.append(request)
        return MemorySessionCloseResult(status="closed")


class BlockingCachedResolveSessionStore(MemoryPluginSessionStore):
    def __init__(self) -> None:
        super().__init__()
        self._gate_lock = Lock()
        self._armed = False
        self.cached_resolved = Event()
        self.release_cached = Event()

    def arm(self) -> None:
        with self._gate_lock:
            self._armed = True
            self.cached_resolved.clear()
            self.release_cached.clear()

    def resolve(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        resolution = super().resolve(*args, **kwargs)
        with self._gate_lock:
            should_block = self._armed and resolution.status == "reused"
            if should_block:
                self._armed = False
        if should_block:
            self.cached_resolved.set()
            assert self.release_cached.wait(1.0)
        return resolution


class BlockingPublishedResolveSessionStore(MemoryPluginSessionStore):
    def __init__(self, *, max_entries: int = 1024) -> None:
        super().__init__(max_entries=max_entries)
        self._gate_lock = Lock()
        self._blocked_session_id: str | None = None
        self.published = Event()
        self.release = Event()

    def block_session(self, session_id: str) -> None:
        with self._gate_lock:
            self._blocked_session_id = session_id
            self.published.clear()
            self.release.clear()

    def resolve(self, identity: RequestIdentity, **kwargs: Any):  # type: ignore[no-untyped-def]
        resolution = super().resolve(identity, **kwargs)
        with self._gate_lock:
            should_block = self._blocked_session_id == identity.session_id
            if should_block:
                self._blocked_session_id = None
        if should_block:
            self.published.set()
            assert self.release.wait(1.0)
        return resolution


class BlockingBeforePublishSessionStore(MemoryPluginSessionStore):
    def __init__(self) -> None:
        super().__init__()
        self.before_publish_started = Event()
        self.before_publish_release = Event()

    def resolve(self, identity: RequestIdentity, **kwargs: Any):  # type: ignore[no-untyped-def]
        before_publish = kwargs["before_publish"]

        def blocked_before_publish(
            record: MemoryPluginSessionRecord,
        ) -> MemoryPluginSessionRecord:
            self.before_publish_started.set()
            assert self.before_publish_release.wait(1.0)
            return before_publish(record)

        kwargs["before_publish"] = blocked_before_publish
        return super().resolve(identity, **kwargs)


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


class ExplodingTraceStore:
    def append(self, event: Any) -> None:
        raise RuntimeError("trace-persistence-sentinel")


class MutableCancelToken:
    def __init__(self) -> None:
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeError("cancelled-sentinel")


class ScriptedIngestionMemoryPlugin(RecordingMemoryPlugin):
    def __init__(
        self,
        *,
        ingestion_results: list[MemoryTurnIngestionResult | Exception] | None = None,
        supports_idempotent_ingestion: bool = True,
        block_ingestion: bool = False,
    ) -> None:
        super().__init__()
        self.descriptor = _descriptor(
            supports_idempotent_ingestion=supports_idempotent_ingestion
        )
        self.ingestion_results = list(
            ingestion_results or [MemoryTurnIngestionResult(status="accepted")]
        )
        self.block_ingestion = block_ingestion
        self.ingestion_started = Event()
        self.ingestion_release = Event()
        self.close_started = Event()
        self.requests: list[Any] = []
        self.close_requests: list[Any] = []
        self._calls_lock = Lock()

    def ingest_turn(self, request: Any) -> MemoryTurnIngestionResult:
        with self._calls_lock:
            call_number = len(self.requests)
            self.requests.append(request)
        self.ingestion_started.set()
        if self.block_ingestion:
            assert self.ingestion_release.wait(1.0)
        result = self.ingestion_results[
            min(call_number, len(self.ingestion_results) - 1)
        ]
        if isinstance(result, Exception):
            raise result
        return result

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        with self._calls_lock:
            self.close_requests.append(request)
        self.close_started.set()
        return MemorySessionCloseResult(status="closed")


class UniqueHandleMemoryPlugin(ScriptedIngestionMemoryPlugin):
    def open_session(self, request: Any) -> MemorySessionOpenResult:
        with self._calls_lock:
            self.open_calls += 1
            call_number = self.open_calls
            self.open_requests.append(request)
        return MemorySessionOpenResult(
            status="ready",
            session_handle=f"unique-handle-{call_number}",
        )


class BlockingPrepareCloseMemoryPlugin(UniqueHandleMemoryPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.prepare_started = Event()
        self.prepare_release = Event()
        self.prepare_finished = Event()
        self.close_before_prepare_finished = Event()

    def prepare_context(self, request: Any) -> MemoryContextContribution:
        with self._calls_lock:
            self.prepare_calls += 1
            self.prepare_requests.append(request)
        self.prepare_started.set()
        try:
            assert self.prepare_release.wait(1.0)
            return MemoryContextContribution(status="succeeded")
        finally:
            self.prepare_finished.set()

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        if not self.prepare_finished.is_set():
            self.close_before_prepare_finished.set()
        return super().close_session(request)


class BlockingMaintenanceCloseMemoryPlugin(UniqueHandleMemoryPlugin):
    def __init__(self) -> None:
        super().__init__(
            supports_idempotent_ingestion=False,
            block_ingestion=True,
        )
        self.close_release = Event()
        self.close_finished = Event()

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        with self._calls_lock:
            self.close_requests.append(request)
        self.close_started.set()
        try:
            assert self.close_release.wait(1.0)
            return MemorySessionCloseResult(status="closed")
        finally:
            self.close_finished.set()


class ToggleFailingCloseMemoryPlugin(UniqueHandleMemoryPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.fail_close = True

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        with self._calls_lock:
            self.close_requests.append(request)
        if self.fail_close:
            return MemorySessionCloseResult(
                status="failed",
                issues=[
                    MemoryPluginIssue(
                        code="plugin.retry",
                        message="plugin.retry",
                        recoverable=True,
                    )
                ],
            )
        return MemorySessionCloseResult(status="closed")


class OversizedFirstCloseResultMemoryPlugin(UniqueHandleMemoryPlugin):
    def __init__(self, *, issue_count: int = 200_000) -> None:
        super().__init__()
        issue = MemoryPluginIssue(
            code="plugin.retry",
            message="plugin.retry",
            recoverable=True,
        )
        self.first_close_result = MemorySessionCloseResult.model_construct(
            status="closed",
            issues=[issue] * issue_count,
        )

    def close_session(self, request: Any) -> MemorySessionCloseResult:
        with self._calls_lock:
            self.close_requests.append(request)
            call_number = len(self.close_requests)
        if call_number == 1:
            return self.first_close_result
        return MemorySessionCloseResult(status="closed")


class BlockingCloseValidationHost(MemoryPluginHost):
    def __init__(self, **kwargs: Any) -> None:
        self.validation_started = Event()
        self.validation_release = Event()
        super().__init__(**kwargs)

    def _validated_close_result(
        self,
        raw: object,
        *,
        cancellation: Any | None = None,
        call_deadline: float | None = None,
    ) -> MemorySessionCloseResult | None:
        self.validation_started.set()
        assert self.validation_release.wait(1.0)
        return super()._validated_close_result(
            raw,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )


class PausingMaintenanceExitHost(MemoryPluginHost):
    def __init__(self, **kwargs: Any) -> None:
        self.maintenance_exiting = Event()
        self.maintenance_exit_release = Event()
        super().__init__(**kwargs)

    def _maintenance_reaper_loop(self) -> None:
        try:
            super()._maintenance_reaper_loop()
        finally:
            self.maintenance_exiting.set()
            assert self.maintenance_exit_release.wait(1.0)


class BlockingSessionDrainQueue(MemoryIngestionQueue):
    """Hold only per-session drain so Host.close can race with its owner."""

    def __init__(self) -> None:
        super().__init__()
        self.session_drain_started = Event()
        self.release_session_drain = Event()

    def drain(
        self,
        *,
        timeout: float | None = None,
        ordering_key: tuple[str, str, str] | None = None,
    ) -> bool:
        if ordering_key is not None:
            self.session_drain_started.set()
            assert self.release_session_drain.wait(1.0)
        return super().drain(timeout=timeout, ordering_key=ordering_key)


class BlockingHostCloseQueue(MemoryIngestionQueue):
    """Expose the queue-close point after Host shutdown owns lifecycle state."""

    def __init__(self) -> None:
        super().__init__()
        self.close_started = Event()
        self.release_close = Event()

    def close(self, *, timeout: float | None = None) -> bool:
        self.close_started.set()
        assert self.release_close.wait(1.0)
        return super().close(timeout=timeout)


class UnsafeIssueCodeMemoryPlugin(ScriptedIngestionMemoryPlugin):
    def __init__(self, *, unsafe_open: bool = True) -> None:
        super().__init__()
        self.unsafe_open = unsafe_open

    def open_session(self, request: Any) -> MemorySessionOpenResult:
        if not self.unsafe_open:
            return super().open_session(request)
        self.open_calls += 1
        self.open_requests.append(request)
        return MemorySessionOpenResult(
            status="ready",
            session_handle="handle-sentinel",
            issues=[
                MemoryPluginIssue(
                    code="authorization_secret_sentinel",
                    message="safe-open-issue",
                    recoverable=False,
                )
            ],
        )

    def prepare_context(self, request: Any) -> MemoryContextContribution:
        self.prepare_calls += 1
        self.prepare_requests.append(request)
        return MemoryContextContribution(
            status="partial",
            issues=[
                MemoryPluginIssue(
                    code="sk-secret123",
                    message="safe-prepare-issue",
                    recoverable=False,
                )
            ],
        )

    def ingest_turn(self, request: Any) -> MemoryTurnIngestionResult:
        with self._calls_lock:
            self.requests.append(request)
        return MemoryTurnIngestionResult(
            status="partial",
            issues=[
                MemoryPluginIssue(
                    code="authorization_secret_sentinel",
                    message="safe-ingestion-issue",
                    recoverable=False,
                )
            ],
        )


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


def test_open_timeout_covers_validation_and_never_publishes_large_baseline() -> None:
    repeated = _item("large", "large")
    plugin = RecordingMemoryPlugin(baseline=[repeated] * 20_000)
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            open_session_timeout_seconds=0.01,
            max_context_items=25_000,
        ),
    )

    identity = _identity()
    snapshot = host.open_session(
        identity=identity,
        state=_state(),
        trace_store=None,
    )

    assert plugin.open_calls == 1
    assert snapshot.memories == []
    assert snapshot.error_codes == ["memory_plugin_timeout"]
    assert host.session_store.get(identity) is None
    assert host.close(timeout=1.0) is True
    assert [request.session_handle for request in plugin.close_requests] == [
        "handle-sentinel"
    ]


def test_open_publish_deadline_keeps_the_allocated_handle_for_cleanup() -> None:
    plugin = UniqueHandleMemoryPlugin()
    store = BlockingBeforePublishSessionStore()
    host, _ = _host(
        plugin,
        session_store=store,
        execution_policy=MemoryPluginExecutionPolicy(
            open_session_timeout_seconds=0.01,
        ),
    )
    identity = _identity()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(
                host.open_session,
                identity=identity,
                state=_completed_state(run_id="publish-deadline-handle"),
                trace_store=None,
            )
            assert store.before_publish_started.wait(0.5)
            sleep(0.02)
            store.before_publish_release.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "degraded"
        assert opened.error_codes == ["memory_plugin_timeout"]
        assert host.close(timeout=1.0) is True
        assert [request.session_handle for request in plugin.close_requests] == [
            "unique-handle-1"
        ]
    finally:
        store.before_publish_release.set()
        host.close(timeout=1.0)


def test_deep_open_metadata_degrades_without_recursion_error_details() -> None:
    plugin = DeepOpenResultPlugin(depth=1_200)
    host, _ = _host(plugin)

    snapshot = host.open_session(
        identity=_identity(),
        state=_state(),
        trace_store=None,
    )

    assert snapshot.memories == []
    assert snapshot.error_codes == ["memory_plugin_invalid_result"]
    assert "RecursionError" not in snapshot.model_dump_json()


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
        "camel-api-key",
        "camel-access-token",
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
    [
        "posix-root",
        "workspace-text-path",
        "metadata-key-path",
        "windows-drive-root-forward",
        "windows-drive-root-backslash",
        "windows-unc-path",
    ],
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
        "输入/输出/错误",
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


def test_dataclass_hidden_non_finite_metadata_is_rejected_before_roundtrip() -> None:
    item = MemoryContextItem.model_construct(
        memory_id="dataclass-non-finite",
        text="dataclass-non-finite",
        source="long_term",
        relevance=None,
        occurred_at=None,
        created_at=None,
        media_refs=[],
        metadata={"payload": DataclassScore(score=float("inf"))},
    )
    contribution = MemoryContextContribution.model_construct(
        items=[item],
        status="succeeded",
        issues=[],
    )
    plugin = RecordingMemoryPlugin(
        baseline=[_item("baseline", "baseline")],
        prepare_result=contribution,
    )
    host, _ = _host(plugin)
    state = _state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    snapshot = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert [memory.memory_id for memory in snapshot.memories] == ["baseline"]
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


def test_host_schedules_an_immutable_standard_turn_with_stable_idempotency() -> None:
    plugin = ScriptedIngestionMemoryPlugin(block_ingestion=True)
    queue = MemoryIngestionQueue(max_workers=1, max_pending=4)
    host, media_store = _host(plugin, ingestion_queue=queue)
    state = _completed_state(run_id="run-sentinel", turn_index=2)
    identity = _identity()
    host.open_session(identity=identity, state=state, trace_store=None)
    record = host.session_store.get(identity)
    assert record is not None
    image_ref = media_store.register(
        b"jpeg-sentinel",
        owner_scope=record.identity.session_id,
        media_type="image",
        mime_type="image/jpeg",
    )
    state.request.image_ids = [image_ref.ref_id]
    state.tool_results = [
        ToolResult(
            tool_name="tool-sentinel",
            success=True,
            output_ref="artifact-sentinel",
        ),
        ToolResult(
            tool_name="failed-tool-sentinel",
            success=False,
            error="raw-error-sentinel",
            output_ref="/private/output-sentinel",
        ),
    ]

    try:
        assert host.schedule_ingestion(state=state, trace_store=None) is True
        assert plugin.ingestion_started.wait(0.5)
        request = plugin.requests[0]

        expected_key = hashlib.sha256(b"mem0run-sentinel2").hexdigest()
        assert request.idempotency_key == expected_key
        assert request.turn.user_message.role == "user"
        assert request.turn.user_message.text == "request-sentinel"
        assert request.turn.assistant_message.role == "assistant"
        assert request.turn.assistant_message.text == "response-sentinel"
        assert [evidence.model_dump() for evidence in request.turn.tool_evidence] == [
            {
                "tool_name": "tool-sentinel",
                "status": "succeeded",
                "output_ref": "artifact-sentinel",
            },
            {
                "tool_name": "failed-tool-sentinel",
                "status": "failed",
                "output_ref": None,
            },
        ]
        assert request.turn.media_refs == [image_ref]
        assert state.request.metadata["memory_ingestion"]["status"] == "queued"

        state.request.text = "mutated-request"
        assert state.response is not None
        state.response.message = "mutated-response"
        state.tool_results.clear()
        state.request.image_ids.clear()
        assert request.turn.user_message.text == "request-sentinel"
        assert request.turn.assistant_message.text == "response-sentinel"
        assert request.turn.media_refs == [image_ref]
    finally:
        plugin.ingestion_release.set()
        host.close(timeout=1.0)


def test_host_ingestion_uses_per_identity_ordering_and_global_pending_bound() -> None:
    class OrderingPlugin(ScriptedIngestionMemoryPlugin):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = Event()
            self.first_release = Event()
            self.second_started = Event()
            self.other_started = Event()
            self.other_release = Event()

        def ingest_turn(self, request: Any) -> MemoryTurnIngestionResult:
            with self._calls_lock:
                self.requests.append(request)
                call_number = len(self.requests)
            if request.identity.session_id == self.first_identity:
                if call_number == 1:
                    self.first_started.set()
                    assert self.first_release.wait(1.0)
                else:
                    self.second_started.set()
            else:
                self.other_started.set()
                assert self.other_release.wait(1.0)
            return MemoryTurnIngestionResult(status="accepted")

    plugin = OrderingPlugin()
    host, _ = _host(
        plugin,
        ingestion_queue=MemoryIngestionQueue(max_workers=2, max_pending=3),
    )
    first = _completed_state(run_id="run-a1", session_id="session-a")
    second = _completed_state(run_id="run-a2", session_id="session-a")
    other = _completed_state(run_id="run-b1", session_id="session-b")
    first_identity = _identity(session_id="session-a")
    other_identity = _identity(session_id="session-b")
    host.open_session(identity=first_identity, state=first, trace_store=None)
    host.open_session(identity=other_identity, state=other, trace_store=None)
    first_record = host.session_store.get(first_identity)
    assert first_record is not None
    plugin.first_identity = first_record.identity.session_id

    try:
        assert host.schedule_ingestion(state=first, trace_store=None)
        assert plugin.first_started.wait(0.5)
        assert host.schedule_ingestion(state=second, trace_store=None)
        assert host.schedule_ingestion(state=other, trace_store=None)
        assert plugin.other_started.wait(0.5)
        assert not plugin.second_started.is_set()

        overflow = _completed_state(run_id="run-b2", session_id="session-b")
        assert host.schedule_ingestion(state=overflow, trace_store=None) is False
        assert overflow.request.metadata["memory_ingestion"] == {
            "status": "failed",
            "error_code": "memory_ingestion_queue_full",
        }

        plugin.first_release.set()
        plugin.other_release.set()
        assert host.drain(timeout=1.0)
        assert plugin.second_started.is_set()
    finally:
        plugin.first_release.set()
        plugin.other_release.set()
        host.close(timeout=1.0)


def test_ingestion_without_idempotency_support_never_retries_failure() -> None:
    plugin = ScriptedIngestionMemoryPlugin(
        ingestion_results=[RuntimeError("remote-raw-error-sentinel")],
        supports_idempotent_ingestion=False,
    )
    host, _ = _host(plugin)
    state = _completed_state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    try:
        assert host.schedule_ingestion(state=state, trace_store=None)
        assert host.drain(timeout=1.0)
        assert len(plugin.requests) == 1
    finally:
        host.close(timeout=1.0)


def test_idempotent_ingestion_retries_one_recoverable_result_at_most_once() -> None:
    recoverable = MemoryTurnIngestionResult(
        status="partial",
        issues=[
            MemoryPluginIssue(
                code="plugin.retry",
                message="retry-sentinel",
                recoverable=True,
            )
        ],
    )
    plugin = ScriptedIngestionMemoryPlugin(
        ingestion_results=[recoverable, recoverable, recoverable],
        supports_idempotent_ingestion=True,
    )
    host, _ = _host(plugin)
    state = _completed_state()
    host.open_session(identity=_identity(), state=state, trace_store=None)

    try:
        assert host.schedule_ingestion(state=state, trace_store=None)
        assert host.drain(timeout=1.0)
        assert len(plugin.requests) == 2
        assert plugin.requests[0].idempotency_key == plugin.requests[1].idempotency_key
    finally:
        host.close(timeout=1.0)


def test_ingestion_skip_uses_only_structured_visual_reminder_results() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    host, _ = _host(plugin)
    state = _completed_state(text="please remember this ordinary sentinel")
    state.tool_results = [
        ToolResult(
            tool_name=VISUAL_REMINDER_MANAGE_TOOL_NAME,
            success=True,
        )
    ]
    host.open_session(identity=_identity(), state=state, trace_store=None)

    try:
        assert host.schedule_ingestion(state=state, trace_store=None) is False
        assert state.request.metadata["memory_ingestion"] == {
            "status": "skipped",
            "reason": "connection_scoped_visual_reminder",
        }
        assert plugin.requests == []
    finally:
        host.close(timeout=1.0)


def test_close_session_stops_new_ingestion_drains_and_calls_plugin_once() -> None:
    plugin = ScriptedIngestionMemoryPlugin(block_ingestion=True)
    host, _ = _host(plugin)
    identity = _identity()
    state = _completed_state()
    host.open_session(identity=identity, state=state, trace_store=None)
    host.prepare_context(state=state, trace_store=None, cancel_token=None)
    assert host.schedule_ingestion(state=state, trace_store=None)
    assert plugin.ingestion_started.wait(0.5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_close = executor.submit(
            host.close_session,
            identity=identity,
            reason="normal",
            timeout=1.0,
        )
        second_close = executor.submit(
            host.close_session,
            identity=identity,
            reason="normal",
            timeout=1.0,
        )
        sleep(0.02)
        rejected = _completed_state(run_id="run-after-close-start")
        assert host.schedule_ingestion(state=rejected, trace_store=None) is False
        assert not plugin.close_started.is_set()

        plugin.ingestion_release.set()
        first_result = first_close.result(timeout=1.0)
        second_result = second_close.result(timeout=1.0)

    repeated = host.close_session(
        identity=identity,
        reason="normal",
        timeout=1.0,
    )
    assert first_result == second_result == repeated
    assert first_result.status == "closed"
    assert len(plugin.close_requests) == 1
    assert plugin.close_requests[0].session_handle == "handle-sentinel"
    assert host.session_store.get(identity) is None
    assert host.attach_frozen_context(state) is None
    host.close(timeout=1.0)


def test_close_session_drain_timeout_is_retryable_without_closing_early() -> None:
    plugin = ScriptedIngestionMemoryPlugin(block_ingestion=True)
    host, _ = _host(plugin)
    identity = _identity()
    state = _completed_state()
    host.open_session(identity=identity, state=state, trace_store=None)
    assert host.schedule_ingestion(state=state, trace_store=None)
    assert plugin.ingestion_started.wait(0.5)

    try:
        first = host.close_session(identity=identity, timeout=0.01)

        assert first.status == "partial"
        assert [issue.code for issue in first.issues] == ["memory_plugin_timeout"]
        assert plugin.close_requests == []
        assert host.session_store.get(identity) is not None
        rejected = _completed_state(run_id="run-after-drain-timeout")
        assert host.schedule_ingestion(state=rejected, trace_store=None) is False

        plugin.ingestion_release.set()
        second = host.close_session(identity=identity, timeout=1.0)
        repeated = host.close_session(identity=identity, timeout=1.0)

        assert second.status == "closed"
        assert repeated == second
        assert len(plugin.close_requests) == 1
        assert host.session_store.get(identity) is None
    finally:
        plugin.ingestion_release.set()
        host.close(timeout=1.0)


def test_close_session_waits_for_inflight_open_and_closes_its_late_handle() -> None:
    plugin = BlockingOpenMemoryPlugin()
    host, _ = _host(plugin)
    identity = _identity()
    state = _completed_state()

    with ThreadPoolExecutor(max_workers=2) as executor:
        opening = executor.submit(
            host.open_session,
            identity=identity,
            state=state,
            trace_store=None,
        )
        assert plugin.open_started.wait(0.5)
        closing = executor.submit(
            host.close_session,
            identity=identity,
            reason="normal",
            timeout=1.0,
        )
        sleep(0.02)
        assert not closing.done()

        plugin.open_release.set()
        opening.result(timeout=1.0)
        close_result = closing.result(timeout=1.0)

    assert close_result.status == "closed"
    assert len(plugin.close_requests) == 1
    assert plugin.close_requests[0].session_handle == "late-handle-sentinel"
    assert host.session_store.get(identity) is None
    host.close(timeout=1.0)


def test_timed_out_open_retains_and_closes_its_late_allocated_handle() -> None:
    plugin = BlockingOpenMemoryPlugin()
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            open_session_timeout_seconds=0.01,
            close_session_timeout_seconds=0.2,
        ),
    )
    identity = _identity()

    try:
        opened = host.open_session(
            identity=identity,
            state=_completed_state(run_id="timed-out-open-late-handle"),
            trace_store=None,
        )
        assert plugin.open_started.wait(0.5)
        assert opened.status == "degraded"
        assert opened.error_codes == ["memory_plugin_timeout"]

        plugin.open_release.set()
        assert host.close(timeout=1.0) is True
        assert [request.session_handle for request in plugin.close_requests] == [
            "late-handle-sentinel"
        ]
    finally:
        plugin.open_release.set()
        host.close(timeout=1.0)


def test_close_session_timeout_invalidates_and_reaps_one_late_open() -> None:
    plugin = BlockingOpenMemoryPlugin()
    host, _ = _host(plugin)
    identity = _identity()
    state = _completed_state()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            opening = executor.submit(
                host.open_session,
                identity=identity,
                state=state,
                trace_store=None,
            )
            assert plugin.open_started.wait(0.5)

            first_close = host.close_session(identity=identity, timeout=0.01)
            assert first_close.status == "partial"
            assert [issue.code for issue in first_close.issues] == [
                "memory_plugin_timeout"
            ]
            assert plugin.close_requests == []
            assert host.session_store.get(identity) is None

            plugin.open_release.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "degraded"
        assert opened.error_codes == ["memory_plugin_unavailable"]
        assert plugin.open_calls == 1
        assert len(plugin.close_requests) == 1
        assert plugin.close_requests[0].session_handle == "late-handle-sentinel"
        assert host.session_store.get(identity) is None

        second_close = host.close_session(identity=identity, timeout=1.0)
        repeated = host.close_session(identity=identity, timeout=1.0)
        assert second_close.status == "closed"
        assert repeated == second_close
        assert len(plugin.close_requests) == 1
    finally:
        plugin.open_release.set()
        host.close(timeout=1.0)


def test_failed_late_open_reap_is_retained_for_one_concurrent_stable_retry() -> None:
    plugin = RetryableLateCloseMemoryPlugin()
    host, _ = _host(plugin)
    identity = _identity()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(
                host.open_session,
                identity=identity,
                state=_completed_state(),
                trace_store=None,
            )
            assert plugin.open_started.wait(0.5)
            first_close = host.close_session(identity=identity, timeout=0.01)
            assert first_close.status == "partial"

            plugin.open_release.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "degraded"
        assert len(plugin.close_requests) == 1
        rejected = _completed_state(run_id="run-after-failed-late-reap")
        assert host.schedule_ingestion(state=rejected, trace_store=None) is False

        retry_gate = Barrier(3)

        def retry_close() -> MemorySessionCloseResult:
            retry_gate.wait()
            return host.close_session(identity=identity, timeout=1.0)

        with ThreadPoolExecutor(max_workers=2) as executor:
            retries = [executor.submit(retry_close) for _ in range(2)]
            retry_gate.wait()
            assert plugin.retry_started.wait(0.5)
            sleep(0.02)
            assert len(plugin.close_requests) == 2
            assert sum(future.done() for future in retries) == 0
            plugin.retry_release.set()
            results = [future.result(timeout=1.0) for future in retries]

        assert [result.status for result in results] == ["closed", "closed"]
        assert len(plugin.close_requests) == 2
        first_request, retry_request = plugin.close_requests
        assert retry_request.memory_session_id == first_request.memory_session_id
        assert retry_request.session_handle == first_request.session_handle
        assert retry_request.identity == first_request.identity
        assert retry_request.reason == first_request.reason == "normal"
        assert host.session_store.get(identity) is None
        assert host.close(timeout=1.0) is True
    finally:
        plugin.open_release.set()
        plugin.retry_release.set()
        host.close(timeout=1.0)


def test_host_close_retries_a_failed_late_open_reap_before_success() -> None:
    plugin = RetryableLateCloseMemoryPlugin(
        first_status="failed",
        block_retry=False,
    )
    host, _ = _host(plugin)
    identity = _identity()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(
                host.open_session,
                identity=identity,
                state=_completed_state(),
                trace_store=None,
            )
            assert plugin.open_started.wait(0.5)
            assert host.close(timeout=0.01) is False
            plugin.open_release.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "degraded"
        assert len(plugin.close_requests) == 1
        assert host.close(timeout=1.0) is True
        assert len(plugin.close_requests) == 2
        first_request, retry_request = plugin.close_requests
        assert retry_request.memory_session_id == first_request.memory_session_id
        assert retry_request.session_handle == first_request.session_handle
        assert retry_request.identity == first_request.identity
        assert retry_request.reason == first_request.reason == "shutdown"
        assert host.close(timeout=1.0) is True
        assert len(plugin.close_requests) == 2
    finally:
        plugin.open_release.set()
        plugin.retry_release.set()
        host.close(timeout=1.0)


def test_host_close_does_not_overlap_retry_with_a_timed_out_late_reap() -> None:
    plugin = TimingOutLateCloseMemoryPlugin()
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            close_session_timeout_seconds=0.02,
        ),
    )
    identity = _identity()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(
                host.open_session,
                identity=identity,
                state=_completed_state(),
                trace_store=None,
            )
            assert plugin.open_started.wait(0.5)
            first_close = host.close_session(identity=identity, timeout=0.005)
            assert first_close.status == "partial"
            plugin.open_release.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "degraded"
        assert plugin.first_close_started.is_set()
        assert len(plugin.close_requests) == 1
        assert host.close(timeout=0.02) is False
        assert len(plugin.close_requests) == 1

        plugin.first_close_release.set()
        assert plugin.first_close_finished.wait(0.5)
        assert host.close(timeout=1.0) is True
        assert len(plugin.close_requests) == 1
        assert plugin.close_requests[0].reason == "normal"
    finally:
        plugin.open_release.set()
        plugin.first_close_release.set()
        host.close(timeout=1.0)


@pytest.mark.parametrize("clear_scope", ["session", "user"])
def test_clear_cannot_steal_a_stored_handle_from_a_timed_out_active_close(
    clear_scope: str,
) -> None:
    plugin = TimingOutLateCloseMemoryPlugin()
    plugin.open_release.set()
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            close_session_timeout_seconds=0.02,
        ),
    )
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )

    try:
        first_close = host.close_session(identity=identity, timeout=0.02)
        assert first_close.status == "failed"
        assert [issue.code for issue in first_close.issues] == ["memory_plugin_timeout"]
        assert plugin.first_close_started.is_set()
        assert len(plugin.close_requests) == 1

        if clear_scope == "session":
            cleared = host.clear_session(
                user_id=identity.user_id,
                session_id=identity.session_id or "",
            )
        else:
            cleared = host.clear_user(
                user_id=identity.user_id,
                agent_id=identity.agent_id,
            )
        assert cleared == 0
        assert host.session_store.get(identity) is not None

        plugin.first_close_release.set()
        assert plugin.first_close_finished.wait(0.5)
        harvested = host.close_session(identity=identity, timeout=1.0)
        assert harvested.status == "closed"
        assert len(plugin.close_requests) == 1
        assert host.session_store.get(identity) is None
        assert host.close(timeout=1.0) is True
    finally:
        plugin.first_close_release.set()
        host.close(timeout=1.0)


@pytest.mark.parametrize("late_status", ["partial", "failed"])
@pytest.mark.parametrize("clear_scope", ["session", "user"])
def test_clear_preserves_stored_handle_after_timed_out_close_finishes_nonclosed(
    late_status: str,
    clear_scope: str,
) -> None:
    plugin = TimingOutLateCloseMemoryPlugin(first_close_status=late_status)
    plugin.open_release.set()
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            close_session_timeout_seconds=0.02,
        ),
    )
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )

    try:
        first_close = host.close_session(identity=identity, timeout=0.02)
        assert first_close.status == "failed"
        assert [issue.code for issue in first_close.issues] == ["memory_plugin_timeout"]
        assert plugin.first_close_started.is_set()

        plugin.first_close_release.set()
        assert plugin.first_close_finished.wait(0.5)
        ordering_key = runtime_memory_identity_key(identity)
        with host._lifecycle_condition:
            active = host._active_session_closes[ordering_key]
            while not active.future.done():
                assert host._lifecycle_condition.wait(0.5)

        if clear_scope == "session":
            cleared = host.clear_session(
                user_id=identity.user_id,
                session_id=identity.session_id or "",
            )
        else:
            cleared = host.clear_user(
                user_id=identity.user_id,
                agent_id=identity.agent_id,
            )
        assert cleared == 0
        assert host.session_store.get(identity) is not None
        assert len(plugin.close_requests) == 1

        retried = host.close_session(identity=identity, timeout=1.0)
        assert retried.status == "closed"
        assert len(plugin.close_requests) == 2
        first_request, retry_request = plugin.close_requests
        assert retry_request.memory_session_id == first_request.memory_session_id
        assert retry_request.session_handle == first_request.session_handle
        assert retry_request.identity == first_request.identity
        assert retry_request.reason == first_request.reason == "normal"
        assert host.session_store.get(identity) is None
        assert host.close(timeout=1.0) is True
    finally:
        plugin.first_close_release.set()
        host.close(timeout=1.0)


@pytest.mark.parametrize("first_status", ["partial", "failed"])
def test_completed_synchronous_close_cannot_be_harvested_by_concurrent_retries(
    first_status: str,
) -> None:
    plugin = TimingOutLateCloseMemoryPlugin(
        first_close_status=first_status,
        block_retry=True,
    )
    plugin.open_release.set()
    host, _ = _host(plugin)
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )
    ordering_key = runtime_memory_identity_key(identity)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(
                host.close_session,
                identity=identity,
                timeout=1.0,
            )
            assert plugin.first_close_started.wait(0.5)

            with host._lifecycle_condition:
                while ordering_key not in host._active_session_closes:
                    assert host._lifecycle_condition.wait(0.5)
                active = host._active_session_closes[ordering_key]
                retries = [
                    executor.submit(
                        host.close_session,
                        identity=identity,
                        timeout=1.0,
                    )
                    for _ in range(2)
                ]
                plugin.first_close_release.set()
                raw_first = active.future.result(timeout=0.5)
                assert raw_first.status == first_status

                host._harvest_completed_active_closes_locked(ordering_key)
                assert host._active_session_closes[ordering_key] is active
                assert ordering_key in host._closing_sessions

            first_result = first.result(timeout=0.5)
            assert first_result.status == first_status
            assert plugin.retry_started.wait(0.5)
            assert len(plugin.close_requests) == 2
            assert sum(retry.done() for retry in retries) == 0

            plugin.retry_release.set()
            retry_results = [retry.result(timeout=1.0) for retry in retries]

        assert [result.status for result in retry_results] == ["closed", "closed"]
        assert len(plugin.close_requests) == 2
        first_request, retry_request = plugin.close_requests
        assert retry_request.memory_session_id == first_request.memory_session_id
        assert retry_request.session_handle == first_request.session_handle
        assert retry_request.identity == first_request.identity
        assert retry_request.reason == first_request.reason == "normal"
        assert host.session_store.get(identity) is None
        assert host.close(timeout=1.0) is True
    finally:
        plugin.first_close_release.set()
        plugin.retry_release.set()
        host.close(timeout=1.0)


def test_host_close_waits_for_late_open_reap_before_reporting_success() -> None:
    plugin = BlockingOpenMemoryPlugin()
    host, _ = _host(plugin)
    identity = _identity()
    state = _completed_state()

    with ThreadPoolExecutor(max_workers=2) as executor:
        opening = executor.submit(
            host.open_session,
            identity=identity,
            state=state,
            trace_store=None,
        )
        assert plugin.open_started.wait(0.5)
        closing = executor.submit(host.close, timeout=1.0)
        sleep(0.02)
        assert not closing.done()
        assert plugin.close_requests == []

        plugin.open_release.set()
        opened = opening.result(timeout=1.0)
        closed = closing.result(timeout=1.0)

    assert opened.status == "degraded"
    assert opened.error_codes == ["memory_plugin_unavailable"]
    assert host.session_store.get(identity) is None
    assert len(plugin.close_requests) == 1
    assert plugin.close_requests[0].session_handle == "late-handle-sentinel"
    assert closed is True
    assert host.close(timeout=1.0) is True


def test_host_close_timeout_can_retry_after_late_open_reap() -> None:
    plugin = BlockingOpenMemoryPlugin()
    host, _ = _host(plugin)
    identity = _identity()
    state = _completed_state()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(
                host.open_session,
                identity=identity,
                state=state,
                trace_store=None,
            )
            assert plugin.open_started.wait(0.5)

            assert host.close(timeout=0.01) is False
            assert plugin.close_requests == []
            plugin.open_release.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "degraded"
        assert opened.error_codes == ["memory_plugin_unavailable"]
        assert host.session_store.get(identity) is None
        assert len(plugin.close_requests) == 1
        assert host.close(timeout=1.0) is True
        assert host.close(timeout=1.0) is True
        assert len(plugin.close_requests) == 1
    finally:
        plugin.open_release.set()


def test_host_close_waits_for_active_session_close_owner_before_shutdown() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    queue = BlockingSessionDrainQueue()
    host, _ = _host(plugin, ingestion_queue=queue)
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        session_closing = executor.submit(
            host.close_session,
            identity=identity,
            reason="normal",
            timeout=1.0,
        )
        assert queue.session_drain_started.wait(0.5)
        host_closing = executor.submit(host.close, timeout=1.0)
        try:
            sleep(0.02)
            assert not host_closing.done()
            assert plugin.close_requests == []
        finally:
            queue.release_session_drain.set()

        session_result = session_closing.result(timeout=1.0)
        host_result = host_closing.result(timeout=1.0)

    assert session_result.status == "closed"
    assert host_result is True
    assert len(plugin.close_requests) == 1
    assert host.session_store.get(identity) is None
    assert host.close(timeout=1.0) is True


def test_host_close_timeout_preserves_active_session_close_for_retry() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    queue = BlockingSessionDrainQueue()
    host, _ = _host(plugin, ingestion_queue=queue)
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        session_closing = executor.submit(
            host.close_session,
            identity=identity,
            reason="normal",
            timeout=1.0,
        )
        assert queue.session_drain_started.wait(0.5)
        try:
            assert host.close(timeout=0.01) is False
            assert plugin.close_requests == []
            assert host.session_store.get(identity) is not None
        finally:
            queue.release_session_drain.set()
        session_result = session_closing.result(timeout=1.0)

    assert session_result.status == "closed"
    assert len(plugin.close_requests) == 1
    assert host.session_store.get(identity) is None
    assert host.close(timeout=1.0) is True
    assert host.close(timeout=1.0) is True
    assert len(plugin.close_requests) == 1


def test_host_close_closes_every_open_session_handle_before_success() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    host, _ = _host(plugin)
    first_identity = _identity(session_id="session-a")
    second_identity = _identity(session_id="session-b")
    host.open_session(
        identity=first_identity,
        state=_completed_state(session_id="session-a", run_id="run-a"),
        trace_store=None,
    )
    host.open_session(
        identity=second_identity,
        state=_completed_state(session_id="session-b", run_id="run-b"),
        trace_store=None,
    )

    assert host.close(timeout=1.0) is True

    assert len(plugin.close_requests) == 2
    assert {request.identity.session_id for request in plugin.close_requests} == {
        bind_memory_plugin_identity(
            first_identity,
            namespace="assistant-agent",
        ).session_id,
        bind_memory_plugin_identity(
            second_identity,
            namespace="assistant-agent",
        ).session_id,
    }
    assert all(request.reason == "shutdown" for request in plugin.close_requests)
    assert host.session_store.get(first_identity) is None
    assert host.session_store.get(second_identity) is None
    assert host.close(timeout=1.0) is True
    assert len(plugin.close_requests) == 2


def test_reset_transfers_replaced_handle_to_retryable_host_close_ownership() -> None:
    plugin = UniqueHandleMemoryPlugin()
    host, _ = _host(plugin)
    identity = _identity()

    first = host.open_session(
        identity=identity,
        state=_completed_state(run_id="run-before-reset"),
        trace_store=None,
    )
    second = host.open_session(
        identity=identity,
        state=_completed_state(run_id="run-after-reset"),
        trace_store=None,
        reset=True,
    )

    assert first.status == second.status == "ready"
    assert [request.session_handle for request in plugin.close_requests] == [
        "unique-handle-1"
    ]
    assert plugin.close_requests[0].reason == "reset"
    assert (
        host.clear_session(
            user_id=identity.user_id,
            session_id=identity.session_id or "",
        )
        == 1
    )
    assert len(plugin.close_requests) == 1
    assert host.close(timeout=1.0) is True
    assert [request.session_handle for request in plugin.close_requests] == [
        "unique-handle-1",
        "unique-handle-2",
    ]
    assert [request.reason for request in plugin.close_requests] == [
        "reset",
        "reset",
    ]


def test_capacity_eviction_transfers_every_handle_to_host_close_ownership() -> None:
    plugin = UniqueHandleMemoryPlugin()
    host, _ = _host(
        plugin,
        session_store=MemoryPluginSessionStore(max_entries=1),
    )
    first_identity = _identity(session_id="capacity-session-a")
    second_identity = _identity(session_id="capacity-session-b")

    host.open_session(
        identity=first_identity,
        state=_completed_state(
            session_id="capacity-session-a",
            run_id="capacity-run-a",
        ),
        trace_store=None,
    )
    host.open_session(
        identity=second_identity,
        state=_completed_state(
            session_id="capacity-session-b",
            run_id="capacity-run-b",
        ),
        trace_store=None,
    )

    assert host.session_store.get(first_identity) is None
    assert host.close(timeout=1.0) is True
    assert [request.session_handle for request in plugin.close_requests] == [
        "unique-handle-1",
        "unique-handle-2",
    ]
    assert [request.reason for request in plugin.close_requests] == [
        "expired",
        "shutdown",
    ]


def test_capacity_admission_reserves_a_slot_before_concurrent_plugin_open() -> None:
    plugin = BlockingConcurrentOpenMemoryPlugin()
    host, _ = _host(
        plugin,
        session_store=MemoryPluginSessionStore(max_entries=1),
    )
    first_session = "reserved-capacity-a"
    second_session = "reserved-capacity-b"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                host.open_session,
                identity=_identity(session_id=first_session),
                state=_completed_state(
                    session_id=first_session,
                    run_id="reserved-capacity-run-a",
                ),
                trace_store=None,
            )
            assert plugin.first_open_started.wait(0.5)
            second = executor.submit(
                host.open_session,
                identity=_identity(session_id=second_session),
                state=_completed_state(
                    session_id=second_session,
                    run_id="reserved-capacity-run-b",
                ),
                trace_store=None,
            )

            rejected = second.result(timeout=0.2)
            assert rejected.status == "degraded"
            assert rejected.error_codes == ["memory_plugin_unavailable"]
            assert not plugin.second_open_started.is_set()
            assert plugin.open_calls == 1

            plugin.open_release.set()
            assert first.result(timeout=1.0).status == "ready"

        assert host.close(timeout=1.0) is True
        assert [request.session_handle for request in plugin.close_requests] == [
            "concurrent-handle-1"
        ]
    finally:
        plugin.open_release.set()
        host.close(timeout=1.0)


def test_capacity_admission_never_dispatches_more_than_the_store_limit() -> None:
    max_entries = 3
    opener_count = 20
    plugin = BlockingConcurrentOpenMemoryPlugin(
        expected_open_calls=max_entries,
    )
    host, _ = _host(
        plugin,
        session_store=MemoryPluginSessionStore(max_entries=max_entries),
    )
    start = Barrier(opener_count)

    def open_one(index: int) -> SessionMemorySnapshot:
        start.wait()
        session_id = f"capacity-stress-{index}"
        return host.open_session(
            identity=_identity(session_id=session_id),
            state=_completed_state(
                session_id=session_id,
                run_id=f"capacity-stress-run-{index}",
            ),
            trace_store=None,
        )

    try:
        with ThreadPoolExecutor(max_workers=opener_count) as executor:
            openings = [
                executor.submit(open_one, index) for index in range(opener_count)
            ]
            assert plugin.expected_opens_started.wait(0.5)
            sleep(0.02)
            assert plugin.open_calls == max_entries
            plugin.open_release.set()
            snapshots = [opening.result(timeout=1.0) for opening in openings]

        assert sum(snapshot.status == "ready" for snapshot in snapshots) == max_entries
        assert sum(snapshot.status == "degraded" for snapshot in snapshots) == (
            opener_count - max_entries
        )
        assert host.close(timeout=1.0) is True
        assert len(plugin.close_requests) == max_entries
    finally:
        plugin.open_release.set()
        host.close(timeout=1.0)


def test_capacity_eviction_closes_displaced_handles_during_steady_state() -> None:
    plugin = UniqueHandleMemoryPlugin()
    host, _ = _host(
        plugin,
        session_store=MemoryPluginSessionStore(max_entries=1),
    )

    for index in range(5):
        session_id = f"steady-capacity-{index}"
        opened = host.open_session(
            identity=_identity(session_id=session_id),
            state=_completed_state(
                session_id=session_id,
                run_id=f"steady-capacity-run-{index}",
            ),
            trace_store=None,
        )
        assert opened.status == "ready"

    assert [request.session_handle for request in plugin.close_requests] == [
        "unique-handle-1",
        "unique-handle-2",
        "unique-handle-3",
        "unique-handle-4",
    ]
    assert all(request.reason == "expired" for request in plugin.close_requests)
    assert host.close(timeout=1.0) is True
    assert plugin.close_requests[-1].session_handle == "unique-handle-5"


def test_capacity_eviction_drains_active_ingestion_before_closing_handle() -> None:
    plugin = UniqueHandleMemoryPlugin(
        supports_idempotent_ingestion=False,
        block_ingestion=True,
    )
    host, _ = _host(
        plugin,
        session_store=MemoryPluginSessionStore(max_entries=1),
    )
    first_session = "ingesting-capacity-a"
    second_session = "ingesting-capacity-b"
    first_state = _completed_state(
        session_id=first_session,
        run_id="ingesting-capacity-run-a",
    )
    host.open_session(
        identity=_identity(session_id=first_session),
        state=first_state,
        trace_store=None,
    )
    assert host.schedule_ingestion(state=first_state, trace_store=None)
    assert plugin.ingestion_started.wait(0.5)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(
                host.open_session,
                identity=_identity(session_id=second_session),
                state=_completed_state(
                    session_id=second_session,
                    run_id="ingesting-capacity-run-b",
                ),
                trace_store=None,
            )
            sleep(0.05)
            assert not opening.done()
            assert plugin.close_requests == []
            plugin.ingestion_release.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "ready"
        assert [request.session_handle for request in plugin.close_requests] == [
            "unique-handle-1"
        ]
        assert host.close(timeout=1.0) is True
    finally:
        plugin.ingestion_release.set()
        host.close(timeout=1.0)


def test_capacity_eviction_waits_for_timed_out_prepare_before_closing_handle() -> None:
    plugin = BlockingPrepareCloseMemoryPlugin()
    host, _ = _host(
        plugin,
        session_store=MemoryPluginSessionStore(max_entries=1),
        execution_policy=MemoryPluginExecutionPolicy(
            prepare_context_timeout_seconds=0.01,
            close_session_timeout_seconds=0.5,
        ),
    )
    first_session = "preparing-capacity-a"
    second_session = "preparing-capacity-b"
    first_identity = _identity(session_id=first_session)
    first_state = _state(
        session_id=first_session,
        run_id="preparing-capacity-run-a",
    )
    host.open_session(
        identity=first_identity,
        state=first_state,
        trace_store=None,
    )
    assert (
        host.prepare_context(
            state=first_state,
            trace_store=None,
            cancel_token=None,
        ).status
        == "degraded"
    )
    assert plugin.prepare_started.wait(0.5)
    assert not plugin.prepare_finished.is_set()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(
                host.open_session,
                identity=_identity(session_id=second_session),
                state=_completed_state(
                    session_id=second_session,
                    run_id="preparing-capacity-run-b",
                ),
                trace_store=None,
            )
            sleep(0.05)
            assert not opening.done()
            assert plugin.close_requests == []
            assert not plugin.close_before_prepare_finished.is_set()

            plugin.prepare_release.set()
            assert plugin.prepare_finished.wait(0.5)
            opened = opening.result(timeout=1.0)

        assert opened.status == "ready"
        assert [request.session_handle for request in plugin.close_requests] == [
            "unique-handle-1"
        ]
        assert not plugin.close_before_prepare_finished.is_set()
    finally:
        plugin.prepare_release.set()
        host.close(timeout=1.0)


def test_reset_cleanup_recovers_after_timed_out_ingestion_finishes() -> None:
    plugin = UniqueHandleMemoryPlugin(
        supports_idempotent_ingestion=False,
        block_ingestion=True,
    )
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            ingest_turn_timeout_seconds=0.01,
            close_session_timeout_seconds=0.02,
        ),
    )
    identity = _identity()
    first_state = _completed_state(run_id="reset-ingestion-before")
    next_state = _completed_state(run_id="reset-ingestion-after")
    host.open_session(identity=identity, state=first_state, trace_store=None)
    assert host.schedule_ingestion(state=first_state, trace_store=None)
    assert plugin.ingestion_started.wait(0.5)

    try:
        reset_result = host.open_session(
            identity=identity,
            state=next_state,
            trace_store=None,
            reset=True,
        )

        assert reset_result.status == "degraded"
        assert plugin.close_requests == []
        assert not host.schedule_ingestion(state=next_state, trace_store=None)
        assert next_state.request.metadata["memory_ingestion"] == {
            "status": "failed",
            "error_code": "memory_plugin_session_closed",
        }

        plugin.ingestion_release.set()
        assert plugin.close_started.wait(0.5)
        deadline = monotonic() + 0.5
        session_key = runtime_memory_identity_key(identity)
        while session_key in host._session_admission_closed and monotonic() < deadline:
            sleep(0.001)

        assert session_key not in host._session_admission_closed
        assert [request.session_handle for request in plugin.close_requests] == [
            "unique-handle-1"
        ]
        assert (
            host.open_session(
                identity=identity,
                state=next_state,
                trace_store=None,
            ).status
            == "ready"
        )
        assert host.schedule_ingestion(state=next_state, trace_store=None)
    finally:
        plugin.ingestion_release.set()
        host.close(timeout=1.0)


def test_host_close_owns_a_running_maintenance_reap_before_success() -> None:
    plugin = BlockingMaintenanceCloseMemoryPlugin()
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            ingest_turn_timeout_seconds=0.01,
            close_session_timeout_seconds=0.02,
        ),
    )
    identity = _identity()
    first_state = _completed_state(run_id="maintenance-close-before")
    next_state = _completed_state(run_id="maintenance-close-after")
    host.open_session(identity=identity, state=first_state, trace_store=None)
    assert host.schedule_ingestion(state=first_state, trace_store=None)
    assert plugin.ingestion_started.wait(0.5)
    assert (
        host.open_session(
            identity=identity,
            state=next_state,
            trace_store=None,
            reset=True,
        ).status
        == "degraded"
    )

    try:
        plugin.ingestion_release.set()
        assert plugin.close_started.wait(0.5)
        assert not plugin.close_finished.is_set()

        assert host.close(timeout=0.02) is False
        assert len(plugin.close_requests) == 1
        assert not plugin.close_finished.is_set()

        plugin.close_release.set()
        assert plugin.close_finished.wait(0.5)
        assert host.close(timeout=1.0) is True
        assert [request.session_handle for request in plugin.close_requests] == [
            "unique-handle-1",
            "unique-handle-2",
        ]
        assert host._maintenance_thread is None
    finally:
        plugin.ingestion_release.set()
        plugin.close_release.set()
        host.close(timeout=1.0)


def test_late_reset_close_releases_only_the_old_handle_admission_barrier() -> None:
    plugin = BlockingMaintenanceCloseMemoryPlugin()
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            close_session_timeout_seconds=0.02,
        ),
    )
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(run_id="late-reset-close-before"),
        trace_store=None,
    )

    try:
        reset_result = host.open_session(
            identity=identity,
            state=_completed_state(run_id="late-reset-close-after"),
            trace_store=None,
            reset=True,
        )
        assert reset_result.status == "degraded"
        assert plugin.close_started.wait(0.5)

        plugin.close_release.set()
        assert plugin.close_finished.wait(0.5)
        deadline = monotonic() + 0.5
        session_key = runtime_memory_identity_key(identity)
        while (
            host._active_session_closes or session_key in host._session_admission_closed
        ) and monotonic() < deadline:
            sleep(0.001)

        assert host._pending_reaps == {}
        assert host._active_session_closes == {}
        assert session_key not in host._session_admission_closed
        assert (
            host.open_session(
                identity=identity,
                state=_completed_state(run_id="late-reset-close-reopened"),
                trace_store=None,
            ).status
            == "ready"
        )
    finally:
        plugin.close_release.set()
        plugin.ingestion_release.set()
        host.close(timeout=1.0)


def test_host_close_joins_maintenance_after_its_active_pointer_is_cleared() -> None:
    plugin = UniqueHandleMemoryPlugin(
        supports_idempotent_ingestion=False,
        block_ingestion=True,
    )
    host, _ = _host(
        plugin,
        host_type=PausingMaintenanceExitHost,
        execution_policy=MemoryPluginExecutionPolicy(
            ingest_turn_timeout_seconds=0.01,
            close_session_timeout_seconds=0.02,
        ),
    )
    assert isinstance(host, PausingMaintenanceExitHost)
    identity = _identity()
    first_state = _completed_state(run_id="maintenance-exit-before")
    next_state = _completed_state(run_id="maintenance-exit-after")
    host.open_session(identity=identity, state=first_state, trace_store=None)
    assert host.schedule_ingestion(state=first_state, trace_store=None)
    assert plugin.ingestion_started.wait(0.5)
    assert (
        host.open_session(
            identity=identity,
            state=next_state,
            trace_store=None,
            reset=True,
        ).status
        == "degraded"
    )

    try:
        plugin.ingestion_release.set()
        assert host.maintenance_exiting.wait(0.5)
        assert host._maintenance_thread is None

        assert host.close(timeout=0.02) is False

        host.maintenance_exit_release.set()
        assert host.close(timeout=1.0) is True
    finally:
        plugin.ingestion_release.set()
        host.maintenance_exit_release.set()
        host.close(timeout=1.0)


def test_close_waits_for_a_timed_out_prepare_future_before_closing_handle() -> None:
    plugin = BlockingPrepareCloseMemoryPlugin()
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            prepare_context_timeout_seconds=0.01,
            close_session_timeout_seconds=0.02,
        ),
    )
    identity = _identity()
    state = _state(run_id="timed-out-prepare-close")
    host.open_session(identity=identity, state=state, trace_store=None)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            preparing = executor.submit(
                host.prepare_context,
                state=state,
                trace_store=None,
                cancel_token=None,
            )
            assert plugin.prepare_started.wait(0.5)
            assert preparing.result(timeout=0.2).status == "degraded"
            assert not plugin.prepare_finished.is_set()

            first_close = host.close_session(identity=identity, timeout=0.02)
            assert first_close.status == "partial"
            assert plugin.close_requests == []
            assert not plugin.close_before_prepare_finished.is_set()

            plugin.prepare_release.set()
            assert plugin.prepare_finished.wait(0.5)

        assert host.close_session(identity=identity, timeout=1.0).status == "closed"
        assert len(plugin.close_requests) == 1
        assert not plugin.close_before_prepare_finished.is_set()
    finally:
        plugin.prepare_release.set()
        host.close(timeout=1.0)


def test_failed_eviction_reap_fail_closes_new_handles_at_the_capacity_bound() -> None:
    plugin = ToggleFailingCloseMemoryPlugin()
    host, _ = _host(
        plugin,
        session_store=MemoryPluginSessionStore(max_entries=1),
    )
    first_session = "failed-reap-capacity-a"
    second_session = "failed-reap-capacity-b"
    third_session = "failed-reap-capacity-c"

    try:
        assert (
            host.open_session(
                identity=_identity(session_id=first_session),
                state=_completed_state(
                    session_id=first_session,
                    run_id="failed-reap-run-a",
                ),
                trace_store=None,
            ).status
            == "ready"
        )
        assert (
            host.open_session(
                identity=_identity(session_id=second_session),
                state=_completed_state(
                    session_id=second_session,
                    run_id="failed-reap-run-b",
                ),
                trace_store=None,
            ).status
            == "ready"
        )

        rejected = host.open_session(
            identity=_identity(session_id=third_session),
            state=_completed_state(
                session_id=third_session,
                run_id="failed-reap-run-c",
            ),
            trace_store=None,
        )

        assert rejected.status == "degraded"
        assert rejected.error_codes == ["memory_plugin_unavailable"]
        assert plugin.open_calls == 2
        assert len(host._pending_reaps) == 1
    finally:
        plugin.fail_close = False
        assert host.close(timeout=1.0) is True


def test_shutdown_waits_for_published_eviction_ownership_before_success() -> None:
    plugin = UniqueHandleMemoryPlugin()
    store = BlockingPublishedResolveSessionStore(max_entries=1)
    host, _ = _host(plugin, session_store=store)
    first_identity = _identity(session_id="published-capacity-a")
    second_identity = _identity(session_id="published-capacity-b")
    host.open_session(
        identity=first_identity,
        state=_completed_state(
            session_id="published-capacity-a",
            run_id="published-capacity-run-a",
        ),
        trace_store=None,
    )
    store.block_session("published-capacity-b")

    with ThreadPoolExecutor(max_workers=2) as executor:
        opening = executor.submit(
            host.open_session,
            identity=second_identity,
            state=_completed_state(
                session_id="published-capacity-b",
                run_id="published-capacity-run-b",
            ),
            trace_store=None,
        )
        assert store.published.wait(0.5)
        closing = executor.submit(host.close, timeout=1.0)
        sleep(0.02)
        assert not closing.done()
        store.release.set()
        opened = opening.result(timeout=1.0)
        closed = closing.result(timeout=1.0)

    assert opened.status == "degraded"
    assert closed is True
    assert {request.session_handle for request in plugin.close_requests} == {
        "unique-handle-1",
        "unique-handle-2",
    }
    assert host.close(timeout=1.0) is True


def test_capacity_reservation_blocks_eviction_until_the_opener_returns() -> None:
    plugin = UniqueHandleMemoryPlugin()
    store = BlockingPublishedResolveSessionStore(max_entries=1)
    host, _ = _host(plugin, session_store=store)
    first_session = "unreturned-capacity-a"
    second_session = "unreturned-capacity-b"
    store.block_session(first_session)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_opening = executor.submit(
            host.open_session,
            identity=_identity(session_id=first_session),
            state=_completed_state(
                session_id=first_session,
                run_id="unreturned-capacity-run-a",
            ),
            trace_store=None,
        )
        assert store.published.wait(0.5)

        second_blocked = host.open_session(
            identity=_identity(session_id=second_session),
            state=_completed_state(
                session_id=second_session,
                run_id="unreturned-capacity-run-b",
            ),
            trace_store=None,
        )
        assert second_blocked.status == "degraded"
        assert second_blocked.error_codes == ["memory_plugin_unavailable"]
        store.release.set()
        first_opened = first_opening.result(timeout=1.0)

    assert first_opened.status == "ready"
    second_opened = host.open_session(
        identity=_identity(session_id=second_session),
        state=_completed_state(
            session_id=second_session,
            run_id="unreturned-capacity-run-b-retry",
        ),
        trace_store=None,
    )
    assert second_opened.status == "ready"
    assert [request.session_handle for request in plugin.close_requests] == [
        "unique-handle-1"
    ]
    assert host.close(timeout=1.0) is True


def test_clear_during_reset_preserves_both_replaced_and_published_handles() -> None:
    plugin = UniqueHandleMemoryPlugin()
    store = BlockingPublishedResolveSessionStore()
    host, _ = _host(plugin, session_store=store)
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(run_id="clear-reset-before"),
        trace_store=None,
    )
    store.block_session(identity.session_id or "")

    with ThreadPoolExecutor(max_workers=1) as executor:
        resetting = executor.submit(
            host.open_session,
            identity=identity,
            state=_completed_state(run_id="clear-reset-after"),
            trace_store=None,
            reset=True,
        )
        assert store.published.wait(0.5)
        assert (
            host.clear_session(
                user_id=identity.user_id,
                session_id=identity.session_id or "",
            )
            == 1
        )
        store.release.set()
        reset_result = resetting.result(timeout=1.0)

    assert reset_result.status == "degraded"
    assert host.close(timeout=1.0) is True
    assert [request.session_handle for request in plugin.close_requests] == [
        "unique-handle-1",
        "unique-handle-2",
    ]


def test_deferred_close_validation_does_not_hold_the_lifecycle_lock() -> None:
    plugin = TimingOutLateCloseMemoryPlugin()
    plugin.open_release.set()
    host, _ = _host(
        plugin,
        host_type=BlockingCloseValidationHost,
        execution_policy=MemoryPluginExecutionPolicy(
            close_session_timeout_seconds=0.02,
        ),
    )
    assert isinstance(host, BlockingCloseValidationHost)
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )

    try:
        first = host.close_session(identity=identity, timeout=0.02)
        assert first.status == "failed"
        plugin.first_close_release.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            clearing = executor.submit(
                host.clear_session,
                user_id=identity.user_id,
                session_id=identity.session_id or "",
            )
            assert host.validation_started.wait(0.5)
            assert clearing.result(timeout=0.1) == 0

        host.validation_release.set()
        assert host.close_session(identity=identity, timeout=1.0).status == "closed"
        assert host.close(timeout=1.0) is True
    finally:
        plugin.first_close_release.set()
        host.validation_release.set()
        host.close(timeout=1.0)


def test_close_timeout_includes_validation_of_oversized_plugin_result() -> None:
    plugin = OversizedFirstCloseResultMemoryPlugin()
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            close_session_timeout_seconds=0.02,
        ),
    )
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )

    started = monotonic()
    first = host.close_session(identity=identity, timeout=0.02)
    elapsed = monotonic() - started

    assert first.status == "failed"
    assert [issue.code for issue in first.issues] == ["memory_plugin_invalid_result"]
    assert elapsed < 0.2
    assert host.session_store.get(identity) is not None
    second = host.close_session(identity=identity, timeout=1.0)
    assert second.status == "closed"
    assert len(plugin.close_requests) == 2
    assert host.close(timeout=1.0) is True


def test_close_caller_returns_at_deadline_while_result_validation_is_running() -> None:
    plugin = UniqueHandleMemoryPlugin()
    host, _ = _host(
        plugin,
        host_type=BlockingCloseValidationHost,
        execution_policy=MemoryPluginExecutionPolicy(
            close_session_timeout_seconds=0.02,
        ),
    )
    assert isinstance(host, BlockingCloseValidationHost)
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            closing = executor.submit(
                host.close_session,
                identity=identity,
                timeout=0.02,
            )
            assert host.validation_started.wait(0.5)
            first = closing.result(timeout=0.1)
        assert first.status == "failed"
        assert [issue.code for issue in first.issues] == ["memory_plugin_timeout"]
        assert host.session_store.get(identity) is not None

        host.validation_release.set()
        assert host.close_session(identity=identity, timeout=1.0).status == "closed"
        assert len(plugin.close_requests) == 2
        assert host.close(timeout=1.0) is True
    finally:
        host.validation_release.set()
        host.close(timeout=1.0)


def test_ingestion_timeout_keeps_true_ordering_and_session_close_ownership() -> None:
    plugin = ScriptedIngestionMemoryPlugin(
        supports_idempotent_ingestion=False,
        block_ingestion=True,
    )
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            ingest_turn_timeout_seconds=0.01,
        ),
    )
    identity = _identity()
    first = _completed_state(run_id="timeout-ingestion-first")
    second = _completed_state(run_id="timeout-ingestion-second")
    host.open_session(identity=identity, state=first, trace_store=None)

    try:
        assert host.schedule_ingestion(state=first, trace_store=None)
        assert plugin.ingestion_started.wait(0.5)
        assert host.schedule_ingestion(state=second, trace_store=None)
        sleep(0.05)

        assert len(plugin.requests) == 1
        assert host.drain(timeout=0.01) is False
        close_result = host.close_session(identity=identity, timeout=0.01)
        assert close_result.status == "partial"
        assert plugin.close_requests == []

        plugin.ingestion_release.set()
        assert host.drain(timeout=1.0) is True
        assert len(plugin.requests) == 2
        assert host.close_session(identity=identity, timeout=1.0).status == "closed"
        assert len(plugin.close_requests) == 1
    finally:
        plugin.ingestion_release.set()
        host.close(timeout=1.0)


def test_host_close_does_not_close_handle_while_timed_out_ingestion_runs() -> None:
    plugin = ScriptedIngestionMemoryPlugin(
        supports_idempotent_ingestion=False,
        block_ingestion=True,
    )
    host, _ = _host(
        plugin,
        execution_policy=MemoryPluginExecutionPolicy(
            ingest_turn_timeout_seconds=0.01,
        ),
    )
    state = _completed_state(run_id="host-close-timeout-ingestion")
    host.open_session(identity=_identity(), state=state, trace_store=None)
    assert host.schedule_ingestion(state=state, trace_store=None)
    assert plugin.ingestion_started.wait(0.5)

    try:
        sleep(0.05)
        assert host.close(timeout=0.02) is False
        assert plugin.close_requests == []
        plugin.ingestion_release.set()
        assert host.close(timeout=1.0) is True
        assert len(plugin.close_requests) == 1
    finally:
        plugin.ingestion_release.set()
        host.close(timeout=1.0)


@pytest.mark.parametrize("clear_scope", ["session", "user"])
def test_clear_cannot_steal_a_handle_owned_by_host_shutdown(
    clear_scope: str,
) -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    queue = BlockingHostCloseQueue()
    host, _ = _host(plugin, ingestion_queue=queue)
    identity = _identity()
    host.open_session(
        identity=identity,
        state=_completed_state(),
        trace_store=None,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        closing = executor.submit(host.close, timeout=1.0)
        assert queue.close_started.wait(0.5)
        try:
            if clear_scope == "session":
                cleared = host.clear_session(
                    user_id=identity.user_id,
                    session_id=identity.session_id or "",
                )
            else:
                cleared = host.clear_user(
                    user_id=identity.user_id,
                    agent_id=identity.agent_id,
                )
            assert cleared == 0
            assert host.session_store.get(identity) is not None
        finally:
            queue.release_close.set()
        assert closing.result(timeout=1.0) is True

    assert len(plugin.close_requests) == 1
    assert plugin.close_requests[0].session_handle == "handle-sentinel"
    assert plugin.close_requests[0].reason == "shutdown"
    assert host.session_store.get(identity) is None
    assert host.close(timeout=1.0) is True


def test_close_invalidates_a_cached_open_before_it_returns_ready() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    store = BlockingCachedResolveSessionStore()
    host, _ = _host(plugin, session_store=store)
    identity = _identity()
    state = _completed_state()
    assert (
        host.open_session(
            identity=identity,
            state=state,
            trace_store=None,
        ).status
        == "ready"
    )
    store.arm()

    with ThreadPoolExecutor(max_workers=2) as executor:
        opening = executor.submit(
            host.open_session,
            identity=identity,
            state=state,
            trace_store=None,
        )
        assert store.cached_resolved.wait(0.5)
        closing = executor.submit(host.close_session, identity=identity, timeout=1.0)
        sleep(0.02)
        assert not closing.done()

        store.release_cached.set()
        opened = opening.result(timeout=1.0)
        closed = closing.result(timeout=1.0)

    assert opened.status == "degraded"
    assert opened.error_codes == ["memory_plugin_unavailable"]
    assert closed.status == "closed"
    assert plugin.open_calls == 1
    assert len(plugin.close_requests) == 1
    assert host.session_store.get(identity) is None
    host.close(timeout=1.0)


def test_clear_invalidates_a_cached_open_before_it_returns_ready() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    store = BlockingCachedResolveSessionStore()
    host, _ = _host(plugin, session_store=store)
    identity = _identity()
    state = _completed_state()
    assert (
        host.open_session(
            identity=identity,
            state=state,
            trace_store=None,
        ).status
        == "ready"
    )
    store.arm()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(
                host.open_session,
                identity=identity,
                state=state,
                trace_store=None,
            )
            assert store.cached_resolved.wait(0.5)

            assert (
                host.clear_session(
                    user_id=identity.user_id,
                    session_id=identity.session_id or "",
                )
                == 1
            )
            store.release_cached.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "degraded"
        assert opened.error_codes == ["memory_plugin_unavailable"]
        assert plugin.open_calls == 1
        assert plugin.close_requests == []
        assert host.session_store.get(identity) is None
    finally:
        store.release_cached.set()
        host.close(timeout=1.0)


def test_clear_blocks_reopen_until_its_invalidated_late_handle_is_reaped() -> None:
    plugin = SequencedBlockingOpenMemoryPlugin()
    host, _ = _host(plugin)
    identity = _identity()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            opening = executor.submit(
                host.open_session,
                identity=identity,
                state=_completed_state(),
                trace_store=None,
            )
            assert plugin.first_open_started.wait(0.5)
            assert (
                host.clear_session(
                    user_id=identity.user_id,
                    session_id=identity.session_id or "",
                )
                == 1
            )
            reopening = executor.submit(
                host.open_session,
                identity=identity,
                state=_completed_state(run_id="run-reopen-during-clear"),
                trace_store=None,
            )
            try:
                reopened = reopening.result(timeout=0.5)
                assert reopened.status == "degraded"
                assert plugin.open_calls == 1
            finally:
                plugin.first_open_release.set()
            opened = opening.result(timeout=1.0)

        assert opened.status == "degraded"
        assert [request.session_handle for request in plugin.close_requests] == [
            "sequenced-handle-1"
        ]

        final_open = host.open_session(
            identity=identity,
            state=_completed_state(run_id="run-after-clear-reap"),
            trace_store=None,
        )
        assert final_open.status == "ready"
        assert plugin.open_calls == 2
        assert host.close(timeout=1.0) is True
        assert [request.session_handle for request in plugin.close_requests] == [
            "sequenced-handle-1",
            "sequenced-handle-2",
        ]
    finally:
        plugin.first_open_release.set()
        host.close(timeout=1.0)


def test_host_close_can_finish_cleanup_after_an_initial_bounded_timeout() -> None:
    plugin = ScriptedIngestionMemoryPlugin(block_ingestion=True)
    host, _ = _host(plugin)
    state = _completed_state()
    host.open_session(identity=_identity(), state=state, trace_store=None)
    assert host.schedule_ingestion(state=state, trace_store=None)
    assert plugin.ingestion_started.wait(0.5)

    assert host.close(timeout=0.01) is False
    plugin.ingestion_release.set()
    assert host.ingestion_queue.drain(timeout=1.0)
    assert host.close(timeout=1.0) is True


def test_clear_operations_only_remove_host_state_without_plugin_crud() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    host, _ = _host(plugin)
    first_identity = _identity(session_id="session-a")
    second_identity = _identity(session_id="session-b")
    first_state = _completed_state(session_id="session-a", run_id="run-a")
    second_state = _completed_state(session_id="session-b", run_id="run-b")
    host.open_session(identity=first_identity, state=first_state, trace_store=None)
    host.open_session(identity=second_identity, state=second_state, trace_store=None)
    host.prepare_context(state=first_state, trace_store=None, cancel_token=None)
    host.prepare_context(state=second_state, trace_store=None, cancel_token=None)

    try:
        assert host.clear_session(user_id="user-sentinel", session_id="session-a") == 1
        assert host.session_store.get(first_identity) is None
        assert host.attach_frozen_context(first_state) is None
        assert host.session_store.get(second_identity) is not None
        assert plugin.close_requests == []

        assert host.clear_user(user_id="user-sentinel") == 1
        assert host.session_store.get(second_identity) is None
        assert host.attach_frozen_context(second_state) is None
        assert plugin.close_requests == []
    finally:
        host.close(timeout=1.0)


def test_clear_session_discards_an_inflight_prepare_result() -> None:
    plugin = BlockingPrepareMemoryPlugin()
    host, _ = _host(plugin)
    identity = _identity()
    state = _completed_state()
    host.open_session(identity=identity, state=state, trace_store=None)
    waiter_entered = Event()

    def prepare_waiter() -> SessionMemorySnapshot:
        waiter_entered.set()
        return host.prepare_context(
            state=state,
            trace_store=None,
            cancel_token=None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        preparing = executor.submit(
            host.prepare_context,
            state=state,
            trace_store=None,
            cancel_token=None,
        )
        assert plugin.first_started.wait(0.5)
        waiting = executor.submit(prepare_waiter)
        assert waiter_entered.wait(0.5)
        sleep(0.02)
        assert not waiting.done()
        assert not plugin.second_started.is_set()
        assert (
            host.clear_session(
                user_id="user-sentinel",
                session_id="session-sentinel",
            )
            == 1
        )
        plugin.release.set()
        prepared = preparing.result(timeout=1.0)
        waited = waiting.result(timeout=1.0)

    assert prepared.memories == []
    assert waited.memories == []
    assert not plugin.second_started.is_set()
    assert host.session_store.get(identity) is None
    assert host.attach_frozen_context(state) is None
    host.close(timeout=1.0)


def test_host_rejects_unsafe_issue_codes_across_plugin_operations() -> None:
    state = _completed_state()
    trace_store = InMemoryTraceStore()
    unsafe_open_plugin = UnsafeIssueCodeMemoryPlugin()
    unsafe_open_host, _ = _host(unsafe_open_plugin)

    unsafe_opened = unsafe_open_host.open_session(
        identity=_identity(),
        state=state,
        trace_store=trace_store,
    )
    assert unsafe_opened.error_codes == ["memory_plugin_invalid_result"]
    assert unsafe_open_host.session_store.get(_identity()) is None
    assert unsafe_open_host.close(timeout=1.0) is True

    plugin = UnsafeIssueCodeMemoryPlugin(unsafe_open=False)
    host, _ = _host(plugin)
    opened = host.open_session(
        identity=_identity(),
        state=state,
        trace_store=trace_store,
    )
    prepared = host.prepare_context(
        state=state,
        trace_store=trace_store,
        cancel_token=None,
    )
    try:
        assert host.schedule_ingestion(state=state, trace_store=trace_store)
        assert host.drain(timeout=1.0)

        assert opened.error_codes == []
        assert prepared.error_codes == ["memory_plugin_invalid_result"]
        events = trace_store.list_by_run(state.run_id)
        recall_events = [
            event
            for event in events
            if event.canonical_event == "memory.session_recall.finished"
        ]
        finished = next(
            event
            for event in events
            if event.canonical_event == "memory.ingestion.finished"
        )

        assert len(recall_events) == 3
        assert [event.attributes["error_codes"] for event in recall_events] == [
            ["memory_plugin_invalid_result"],
            [],
            ["memory_plugin_invalid_result"],
        ]
        assert [event.output_summary["error_codes"] for event in recall_events] == [
            ["memory_plugin_invalid_result"],
            [],
            ["memory_plugin_invalid_result"],
        ]
        assert [
            event.attributes["memory_plugin_issue_codes"] for event in recall_events
        ] == [
            ["memory_plugin_invalid_result"],
            [],
            ["memory_plugin_invalid_result"],
        ]
        assert finished.attributes["memory_plugin_issue_codes"] == [
            "memory_plugin_invalid_result"
        ]
        assert finished.error == {
            "code": "memory_plugin_invalid_result",
            "message": "memory_plugin_invalid_result",
        }

        serialized = json.dumps(
            [event.model_dump(mode="json") for event in events],
            ensure_ascii=False,
        )
        assert "authorization_secret_sentinel" not in serialized
        assert "sk-secret123" not in serialized
    finally:
        host.close(timeout=1.0)


def test_invalid_open_result_keeps_a_safe_session_handle_for_cleanup() -> None:
    plugin = UnsafeIssueCodeMemoryPlugin()
    host, _ = _host(plugin)

    opened = host.open_session(
        identity=_identity(),
        state=_completed_state(run_id="invalid-open-safe-handle"),
        trace_store=None,
    )

    assert opened.error_codes == ["memory_plugin_invalid_result"]
    assert host.close(timeout=1.0) is True
    assert [request.session_handle for request in plugin.close_requests] == [
        "handle-sentinel"
    ]


def test_memory_observability_normalizes_untrusted_issue_and_error_codes() -> None:
    state = _completed_state()
    trace_store = InMemoryTraceStore()

    record_session_recall(
        trace_store=trace_store,
        state=state,
        status="degraded",
        latency_ms=0,
        error_codes=["plugin.retry", "authorization_secret_sentinel"],
        memory_plugin_id="mem0",
        memory_plugin_version="1.0.0",
        memory_plugin_api_version="assistant_memory_plugin_v1",
        memory_plugin_operation="prepare_context",
        memory_plugin_issue_codes=["sk-secret123", "plugin.retry"],
    )
    record_ingestion_finished(
        trace_store=trace_store,
        state=state,
        status="failed",
        latency_ms=0,
        errors=[
            {"code": "authorization_secret_sentinel", "message": "safe-error"},
            {"code": "plugin.retry", "message": "safe-retry"},
        ],
        error_code="sk-secret123",
        memory_plugin_id="mem0",
        memory_plugin_version="1.0.0",
        memory_plugin_api_version="assistant_memory_plugin_v1",
        memory_plugin_operation="ingest_turn",
        memory_plugin_issue_codes=[
            "authorization_secret_sentinel",
            "plugin.retry",
        ],
    )

    recall, finished = trace_store.list_by_run(state.run_id)
    assert recall.attributes["error_codes"] == [
        "plugin.retry",
        "memory_plugin_invalid_result",
    ]
    assert recall.output_summary["error_codes"] == [
        "plugin.retry",
        "memory_plugin_invalid_result",
    ]
    assert recall.attributes["memory_plugin_issue_codes"] == [
        "memory_plugin_invalid_result",
        "plugin.retry",
    ]
    assert finished.attributes["memory_plugin_issue_codes"] == [
        "memory_plugin_invalid_result",
        "plugin.retry",
    ]
    assert finished.output_summary["errors"] == [
        {"code": "memory_plugin_invalid_result", "message": "safe-error"},
        {"code": "plugin.retry", "message": "safe-retry"},
    ]
    assert finished.error == {
        "code": "memory_plugin_invalid_result",
        "message": "memory_plugin_invalid_result",
    }

    serialized = json.dumps(
        [event.model_dump(mode="json") for event in (recall, finished)],
        ensure_ascii=False,
    )
    assert "authorization_secret_sentinel" not in serialized
    assert "sk-secret123" not in serialized


def test_plugin_observability_keeps_canonical_events_prompt_safe() -> None:
    recoverable = MemoryTurnIngestionResult(
        status="partial",
        changes=[
            MemoryChange(
                operation="updated",
                memory_id="memory-sentinel",
                memory_type="semantic",
            )
        ],
        issues=[
            MemoryPluginIssue(
                code="plugin.retry",
                message="remote-detail-sentinel",
                recoverable=True,
            )
        ],
    )
    plugin = ScriptedIngestionMemoryPlugin(ingestion_results=[recoverable, recoverable])
    host, _ = _host(plugin)
    state = _completed_state(text="private-user-body-sentinel")
    assert state.response is not None
    state.response.message = "private-assistant-body-sentinel"
    trace_store = InMemoryTraceStore()
    host.open_session(identity=_identity(), state=state, trace_store=trace_store)

    try:
        assert host.schedule_ingestion(state=state, trace_store=trace_store)
        assert host.drain(timeout=1.0)
        events = trace_store.list_by_run(state.run_id)
        recall = next(
            event
            for event in events
            if event.canonical_event == "memory.session_recall.finished"
        )
        queued = next(
            event
            for event in events
            if event.canonical_event == "memory.ingestion.queued"
        )
        finished = next(
            event
            for event in events
            if event.canonical_event == "memory.ingestion.finished"
        )

        for event, operation, retry_count in (
            (recall, "open_session", 0),
            (queued, "ingest_turn", 0),
            (finished, "ingest_turn", 1),
        ):
            assert event.attributes["memory_plugin_id"] == "mem0"
            assert event.attributes["memory_plugin_version"] == "1.0.0"
            assert (
                event.attributes["memory_plugin_api_version"]
                == "assistant_memory_plugin_v1"
            )
            assert event.attributes["memory_plugin_operation"] == operation
            assert event.attributes["memory_plugin_retry_count"] == retry_count
        assert finished.attributes["memory_plugin_issue_codes"] == ["plugin.retry"]
        assert finished.attributes["change_counts"] == {"updated": 1}

        serialized = [event.model_dump(mode="json") for event in events]
        assert all("remote-detail-sentinel" not in repr(event) for event in serialized)
        assert all(
            "private-user-body-sentinel" not in repr(event) for event in serialized
        )
        assert all(
            "private-assistant-body-sentinel" not in repr(event) for event in serialized
        )
        assert all("handle-sentinel" not in repr(event) for event in serialized)
    finally:
        host.close(timeout=1.0)


def test_plugin_observability_failure_is_fail_open_for_host_lifecycle() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    host, _ = _host(plugin)
    state = _completed_state()
    trace_store = ExplodingTraceStore()

    try:
        opened = host.open_session(
            identity=_identity(),
            state=state,
            trace_store=trace_store,  # type: ignore[arg-type]
        )
        prepared = host.prepare_context(
            state=state,
            trace_store=trace_store,  # type: ignore[arg-type]
            cancel_token=None,
        )
        assert host.schedule_ingestion(
            state=state,
            trace_store=trace_store,  # type: ignore[arg-type]
        )
        assert host.drain(timeout=1.0)

        assert opened.status == "ready"
        assert prepared.status == "succeeded"
        assert len(plugin.requests) == 1
    finally:
        host.close(timeout=1.0)


def test_plugin_observability_redacts_unsafe_descriptor_attributes() -> None:
    plugin = ScriptedIngestionMemoryPlugin()
    plugin.descriptor = plugin.descriptor.model_copy(
        update={
            "plugin_id": "authorization-secret-sentinel",
            "plugin_version": "api_key-secret-sentinel",
        }
    )
    host, _ = _host(plugin)
    state = _completed_state()
    trace_store = InMemoryTraceStore()
    host.open_session(identity=_identity(), state=state, trace_store=trace_store)

    try:
        assert host.schedule_ingestion(state=state, trace_store=trace_store)
        assert host.drain(timeout=1.0)
        events = [
            event
            for event in trace_store.list_by_run(state.run_id)
            if event.canonical_event
            in {
                "memory.session_recall.finished",
                "memory.ingestion.queued",
                "memory.ingestion.finished",
            }
        ]

        assert len(events) == 3
        for event in events:
            assert event.attributes["memory_plugin_id"] == "[redacted]"
            assert event.attributes["memory_plugin_version"] == "[redacted]"
            assert (
                event.attributes["memory_plugin_api_version"]
                == "assistant_memory_plugin_v1"
            )
            assert "memory_plugin_operation" in event.attributes
            assert "memory_plugin_issue_codes" in event.attributes
            assert "memory_plugin_retry_count" in event.attributes
        serialized = json.dumps(
            [event.model_dump(mode="json") for event in events],
            ensure_ascii=False,
        )
        assert "secret-sentinel" not in serialized
        assert "authorization-secret-sentinel" not in serialized
        assert "api_key-secret-sentinel" not in serialized
    finally:
        host.close(timeout=1.0)


def _descriptor(
    *,
    supports_context_refresh: bool = True,
    supports_idempotent_ingestion: bool = True,
) -> MemoryPluginDescriptor:
    return MemoryPluginDescriptor(
        plugin_id="mem0",
        plugin_version="1.0.0",
        capabilities=MemoryPluginCapabilities(
            modalities={"text", "image"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=supports_context_refresh,
            supports_idempotent_ingestion=supports_idempotent_ingestion,
        ),
    )


def _host(
    plugin: Any,
    *,
    host_type: type[MemoryPluginHost] = MemoryPluginHost,
    session_store: MemoryPluginSessionStore | None = None,
    media_store: ManagedMemoryMediaStore | None = None,
    execution_policy: MemoryPluginExecutionPolicy | None = None,
    ingestion_queue: MemoryIngestionQueue | None = None,
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
        host_type(
            registry=registry,
            session_store=session_store or MemoryPluginSessionStore(),
            media_store=resolved_media_store,
            ingestion_queue=ingestion_queue,
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


def _completed_state(
    *,
    run_id: str = "run-sentinel",
    user_id: str = "user-sentinel",
    session_id: str = "session-sentinel",
    turn_index: int | str = 1,
    text: str = "request-sentinel",
) -> AgentState:
    state = _state(
        run_id=run_id,
        user_id=user_id,
        session_id=session_id,
        metadata={"conversation_turn_index": turn_index},
        text=text,
    )
    state.set_response(AgentResponse(message="response-sentinel"))
    return state


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
    elif kind == "camel-api-key":
        item = MemoryContextItem(
            memory_id="unsafe",
            text="unsafe",
            source="long_term",
            metadata={"serviceApiKey": "credential-sentinel"},
        )
    elif kind == "camel-access-token":
        item = MemoryContextItem(
            memory_id="unsafe",
            text="unsafe",
            source="long_term",
            metadata={"accessToken": "credential-sentinel"},
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
    if kind == "posix-root":
        item = _item("unsafe", "/")
    elif kind == "workspace-text-path":
        item = _item("unsafe", "构建产物：/workspace/project/private.txt")
    elif kind == "metadata-key-path":
        item = MemoryContextItem(
            memory_id="unsafe",
            text="unsafe",
            source="long_term",
            metadata={"artifact /workspace/project/private.txt": "unsafe"},
        )
    elif kind == "windows-drive-root-forward":
        item = _item("unsafe", "drive C:/")
    elif kind == "windows-drive-root-backslash":
        item = _item("unsafe", "drive C:\\")
    else:
        item = _item("unsafe", r"share \\server\share\private.txt")
    return MemoryContextContribution(items=[item], status="succeeded")
