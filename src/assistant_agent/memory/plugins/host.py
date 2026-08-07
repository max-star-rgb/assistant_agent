"""Governed lifecycle Host for the active Memory Plugin."""

from __future__ import annotations

import hashlib
import json
import math
import re
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Condition, Event, Thread
from time import monotonic, perf_counter
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.memory.observability import (
    MemoryObservationContext,
    record_ingestion_finished,
    record_ingestion_queued,
    record_session_recall,
)
from assistant_agent.memory.plugins.contracts import (
    CompletedMemoryTurn,
    MEMORY_PLUGIN_INTERNAL_ERROR,
    MEMORY_PLUGIN_INVALID_RESULT,
    MEMORY_PLUGIN_TIMEOUT,
    MEMORY_PLUGIN_UNAVAILABLE,
    ManagedMediaRef,
    MemoryBudgetHint,
    MemoryCancellationToken,
    MemoryContextContribution,
    MemoryContextItem,
    MemoryContextRequest,
    MemoryChange,
    MemoryIdentity,
    MemoryMessage,
    MemoryPluginDescriptor,
    MemoryPluginExecutionPolicy,
    MemoryPluginIssue,
    MemorySessionCloseRequest,
    MemorySessionCloseResult,
    MemorySessionOpenRequest,
    MemorySessionOpenResult,
    MemoryToolEvidence,
    MemoryTurnIngestionRequest,
    MemoryTurnIngestionResult,
)
from assistant_agent.memory.plugins.media import (
    ManagedMemoryMediaStore,
    MemoryMediaAccessError,
)
from assistant_agent.memory.plugins.registry import MemoryPluginRegistry
from assistant_agent.memory.plugins.session_store import (
    MemoryPluginSessionLoadInvalidated,
    MemoryPluginSessionRecord,
    MemoryPluginSessionResolutionStatus,
    MemoryPluginSessionStore,
    runtime_memory_identity_key,
)
from assistant_agent.observability.trace_store import TraceStore
from assistant_agent.runtime.cancellation import is_cancelled
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.ids import VISUAL_REMINDER_MANAGE_TOOL_NAME


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_RunMemoryKey = tuple[str, str, str, str]
_SessionMemoryKey = tuple[str, str, str]
_SessionCloseReason = Literal[
    "normal", "reset", "expired", "shutdown", "plugin_replaced"
]
_SessionCloseSource = Literal["pending", "stored"]
_SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_UNSAFE_ISSUE_CODE_RE = re.compile(
    r"(?:api[_-]?key|authorization|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
_POSIX_PATH_CANDIDATE_RE = re.compile(r"/(?!/)[^\s\"'<>]*")
_WINDOWS_DRIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>]*")
_UNC_PATH_RE = re.compile(r"(?<!\\)\\\\[^\s\\/]+[\\/][^\s\"'<>]+")
_BASE64_RE = re.compile(
    r"(?:[A-Za-z0-9+/]{80,}={0,2}|data:[^;\s]+;base64,)", re.IGNORECASE
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:api[_-]?key|auth(?:orization)?|access[_-]?token|refresh[_-]?token|"
    r"credential(?:s)?|password|secret(?:[_-]?token)?|token)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]{4,}",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{4,}", re.IGNORECASE)
_KEY_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|pk)-[A-Za-z0-9_-]{4,}", re.IGNORECASE
)
_BLOCKED_METADATA_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "auth",
        "authorization",
        "base64",
        "bytes",
        "credential",
        "credentials",
        "data_uri",
        "developer_prompt",
        "inline_data",
        "messages",
        "password",
        "prompt_patch",
        "role",
        "secret",
        "secret_token",
        "system_prompt",
        "token",
        "refresh_token",
    }
)
_CREDENTIAL_METADATA_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
        "secret_token",
        "token",
    }
)
_CREDENTIAL_METADATA_COMPACT_KEYS = frozenset(
    key.replace("_", "") for key in _CREDENTIAL_METADATA_KEYS
)
_MAX_VALIDATION_DEPTH = 256
_MAX_VALIDATION_NODES = 1_000_000
_MAX_CLOSE_RESULT_ISSUES = 1_024


def bind_memory_plugin_identity(
    identity: RequestIdentity,
    *,
    namespace: str,
) -> MemoryIdentity:
    """Map trusted Runtime identity to stable Plugin-scoped opaque IDs.

    The algorithm intentionally matches the historical Mem0 mapping. Passing
    the default Mem0 namespace therefore preserves every existing opaque ID.
    """

    if not identity.session_id:
        raise ValueError("session_id is required for Memory Plugin identity")
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("Memory Plugin identity namespace must be non-empty")

    def digest(kind: str, value: str) -> str:
        payload = f"assistant_agent:{namespace}:{kind}:{value}".encode()
        return hashlib.sha256(payload).hexdigest()[:32]

    run_seed = "\x1f".join((identity.user_id, identity.agent_id, identity.session_id))
    return MemoryIdentity(
        user_id=f"usr_{digest('user', identity.user_id)}",
        agent_id=f"agt_{digest('agent', identity.agent_id)}",
        session_id=f"run_{digest('run', run_seed)}",
    )


@dataclass(frozen=True)
class _DelegatingCancellationToken:
    token: Any | None

    def is_cancelled(self) -> bool:
        return is_cancelled(self.token)

    def raise_if_cancelled(self) -> None:
        if self.token is None:
            return
        raiser = getattr(self.token, "raise_if_cancelled", None)
        if callable(raiser):
            raiser()
            return
        if self.is_cancelled():
            raise RuntimeError("Memory Plugin call cancelled.")


class _MemoryCallStopped(RuntimeError):
    def __init__(self, issue_code: str) -> None:
        self.issue_code = issue_code
        super().__init__("memory_plugin_call_stopped")


class _OpenRecordRejected(RuntimeError):
    def __init__(
        self,
        issue_code: str,
        record: MemoryPluginSessionRecord | None = None,
    ) -> None:
        self.issue_code = issue_code
        self.record = record
        super().__init__("memory_plugin_open_record_rejected")


@dataclass(frozen=True)
class _ScheduledMemoryIngestion:
    request: MemoryTurnIngestionRequest
    ordering_key: _SessionMemoryKey
    trace_store: TraceStore | None
    observation_context: MemoryObservationContext
    plugin_id: str
    plugin_version: str
    api_version: str
    initial_issue_codes: tuple[str, ...]


@dataclass
class _PendingSessionReap:
    reap_id: int
    record: MemoryPluginSessionRecord
    reason: _SessionCloseReason
    cache_terminal: bool
    eager_close: bool = True


@dataclass
class _ActiveSessionClose:
    future: Future[Any]
    record: MemoryPluginSessionRecord
    reason: _SessionCloseReason
    source: _SessionCloseSource
    pending_reap_id: int | None
    cache_terminal: bool
    deferred: bool = False


@dataclass(frozen=True)
class _OpenCallOutcome:
    raw: object | None
    failure_code: str | None
    cleanup_record: MemoryPluginSessionRecord | None


@dataclass
class _ActiveSessionOpen:
    open_id: int
    future: Future[_OpenCallOutcome]
    invalidation: Event
    fallback_record: MemoryPluginSessionRecord
    deferred: bool = False
    publication_done: bool = False


class MemoryPluginHost:
    """Validate and freeze recall from the Registry's active Plugin."""

    def __init__(
        self,
        *,
        registry: MemoryPluginRegistry,
        session_store: MemoryPluginSessionStore | None = None,
        media_store: ManagedMemoryMediaStore | None = None,
        ingestion_queue: MemoryIngestionQueue | None = None,
        execution_policy: MemoryPluginExecutionPolicy | None = None,
        identity_namespace: str = "assistant-agent",
        clock: Any | None = None,
    ) -> None:
        if not isinstance(registry, MemoryPluginRegistry):
            raise TypeError("registry must be a sealed MemoryPluginRegistry")
        if not isinstance(identity_namespace, str) or not identity_namespace:
            raise ValueError("identity_namespace must be non-empty")
        self.registry = registry
        self._active_plugin, self._active_descriptor = _sealed_registry_runtime(
            registry
        )
        self.session_store = session_store or MemoryPluginSessionStore()
        self.media_store = media_store or ManagedMemoryMediaStore(
            max_total_bytes=32 * 1024 * 1024
        )
        self.ingestion_queue = ingestion_queue or MemoryIngestionQueue()
        self.execution_policy = (
            execution_policy or MemoryPluginExecutionPolicy()
        ).model_copy(deep=True)
        self.identity_namespace = identity_namespace
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="assistant-memory-plugin",
        )
        self._run_condition = Condition()
        self._preparing_runs: set[_RunMemoryKey] = set()
        self._frozen_run_contexts: dict[_RunMemoryKey, SessionMemorySnapshot] = {}
        self._run_epochs: dict[_RunMemoryKey, int] = {}
        self._lifecycle_condition = Condition()
        self._opening_sessions: dict[_SessionMemoryKey, set[Event]] = {}
        self._open_capacity_reservations: dict[_SessionMemoryKey, int] = {}
        self._active_session_opens: dict[int, _ActiveSessionOpen] = {}
        self._next_active_open_id = 0
        self._deferred_open_barriers: set[_SessionMemoryKey] = set()
        self._closing_sessions: set[_SessionMemoryKey] = set()
        self._session_admission_closed: set[_SessionMemoryKey] = set()
        self._clear_admission_barriers: set[_SessionMemoryKey] = set()
        self._session_close_reasons: dict[
            _SessionMemoryKey,
            _SessionCloseReason,
        ] = {}
        self._pending_reaps: dict[int, _PendingSessionReap] = {}
        self._next_pending_reap_id = 0
        self._active_session_closes: dict[_SessionMemoryKey, _ActiveSessionClose] = {}
        self._active_session_operations: dict[_SessionMemoryKey, set[int]] = {}
        self._next_session_operation_id = 0
        self._closed_session_results: dict[
            _SessionMemoryKey, MemorySessionCloseResult
        ] = {}
        self._maintenance_thread: Thread | None = None
        self._maintenance_threads: set[Thread] = set()
        self._maintenance_requested = False
        self._maintenance_stopping = False
        self._host_accepting = True
        self._host_close_in_progress = False
        self._host_close_result: bool | None = None
        self._executor_condition = Condition()
        self._executor_futures: set[Future[Any]] = set()
        self._executor_accepting = True

    @property
    def active_plugin(self):  # type: ignore[no-untyped-def]
        return self._active_plugin

    def open_session(
        self,
        *,
        identity: RequestIdentity,
        state: AgentState,
        trace_store: TraceStore | None,
        reset: bool = False,
    ) -> SessionMemorySnapshot:
        """Open the active Plugin once and freeze its session baseline."""

        self._reap_pending_sessions(eager_only=False)
        started = perf_counter()
        descriptor = self._active_descriptor
        session_key = runtime_memory_identity_key(identity)
        open_invalidation = Event()
        capacity_reserved = False
        with self._lifecycle_condition:
            self._adopt_session_store_retirements_locked()
            cached_for_admission = self.session_store.get(identity)
            requires_new_handle = (
                reset
                or cached_for_admission is None
                or descriptor is None
                or cached_for_admission.plugin_id != descriptor.plugin_id
                or cached_for_admission.plugin_version != descriptor.plugin_version
            )
            existing_reservation = session_key in self._open_capacity_reservations
            unresolved_reaps = len(self._pending_reaps) + len(
                self._active_session_closes
            )
            unresolved_reaps += len(
                {
                    active.fallback_record.runtime_identity_key
                    for active in self._active_session_opens.values()
                    if active.deferred
                }
            )
            capacity_pressure = unresolved_reaps + len(self._open_capacity_reservations)
            if (
                not self._host_accepting
                or session_key in self._closing_sessions
                or session_key in self._session_admission_closed
                or (
                    requires_new_handle
                    and (
                        (reset and existing_reservation)
                        or (
                            not existing_reservation
                            and capacity_pressure >= self.session_store.max_entries
                        )
                    )
                )
            ):
                return SessionMemorySnapshot(
                    plugin_id=descriptor.plugin_id if descriptor is not None else None,
                    status="degraded",
                    error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
                )
            self._closed_session_results.pop(session_key, None)
            if descriptor is None or self._active_plugin is None:
                return SessionMemorySnapshot(
                    status="degraded",
                    error_codes=[MEMORY_PLUGIN_INTERNAL_ERROR],
                )
            if requires_new_handle:
                self._open_capacity_reservations[session_key] = (
                    self._open_capacity_reservations.get(session_key, 0) + 1
                )
                capacity_reserved = True
            self._opening_sessions.setdefault(session_key, set()).add(open_invalidation)
        resolution_status: MemoryPluginSessionResolutionStatus | None = None
        try:
            cached = self.session_store.get(identity)
            plugin_changed = cached is not None and (
                cached.plugin_id != descriptor.plugin_id
                or cached.plugin_version != descriptor.plugin_version
            )
            cancellation = _DelegatingCancellationToken(None)
            call_deadline = (
                monotonic() + self.execution_policy.open_session_timeout_seconds
            )
            resolution = self.session_store.resolve(
                identity,
                reset=reset or plugin_changed,
                reset_reason="plugin_replaced" if plugin_changed else "reset",
                retry_on_invalidation=False,
                resolution_invalidation=open_invalidation,
                loader=lambda: self._open_record_if_current(
                    identity=identity,
                    state=state,
                    descriptor=descriptor,
                    cancellation=cancellation,
                    call_deadline=call_deadline,
                    invalidation=open_invalidation,
                ),
                before_publish=lambda record: _guard_open_record_for_publish(
                    record,
                    cancellation=cancellation,
                    call_deadline=call_deadline,
                    invalidation=open_invalidation,
                ),
            )
            snapshot = resolution.record.baseline.model_copy(deep=True)
            snapshot = self._complete_open(
                identity=identity,
                session_key=session_key,
                invalidation=open_invalidation,
                snapshot=snapshot,
                descriptor=descriptor,
            )
            resolution_status = resolution.status
        except _OpenRecordRejected as rejected:
            if rejected.record is not None:
                self._reap_invalidated_open(
                    identity=identity,
                    record=rejected.record,
                )
            snapshot = self._complete_open(
                identity=identity,
                session_key=session_key,
                invalidation=open_invalidation,
                snapshot=SessionMemorySnapshot(
                    plugin_id=descriptor.plugin_id,
                    status="degraded",
                    error_codes=[rejected.issue_code],
                ),
                descriptor=descriptor,
            )
            resolution_status = "loaded"
        except MemoryPluginSessionLoadInvalidated as invalidated:
            if invalidated.record is not None:
                self._reap_invalidated_open(
                    identity=identity,
                    record=invalidated.record,
                )
            snapshot = self._complete_open(
                identity=identity,
                session_key=session_key,
                invalidation=open_invalidation,
                snapshot=SessionMemorySnapshot(
                    plugin_id=descriptor.plugin_id,
                    status="degraded",
                    error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
                ),
                descriptor=descriptor,
            )
        finally:
            self.session_store.release_resolution(identity, open_invalidation)
            self._discard_opening(
                session_key=session_key,
                invalidation=open_invalidation,
            )
            if capacity_reserved:
                with self._lifecycle_condition:
                    reservation_count = self._open_capacity_reservations.get(
                        session_key,
                        0,
                    )
                    if reservation_count <= 1:
                        self._open_capacity_reservations.pop(session_key, None)
                    else:
                        self._open_capacity_reservations[session_key] = (
                            reservation_count - 1
                        )
                    self._lifecycle_condition.notify_all()
        self._reap_pending_sessions(eager_only=True)
        snapshot = self._degrade_open_while_reap_pending(
            session_key=session_key,
            snapshot=snapshot,
            descriptor=descriptor,
        )
        if resolution_status == "loaded":
            record_session_recall(
                trace_store=trace_store,
                state=state,
                status=snapshot.status,
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
                memory_count=len(snapshot.memories),
                error_codes=list(snapshot.error_codes),
                memory_plugin_id=descriptor.plugin_id,
                memory_plugin_version=descriptor.plugin_version,
                memory_plugin_api_version=descriptor.api_version,
                memory_plugin_operation="open_session",
                memory_plugin_issue_codes=list(snapshot.error_codes),
                memory_plugin_retry_count=0,
            )
        return snapshot

    def prepare_context(
        self,
        *,
        state: AgentState,
        trace_store: TraceStore | None,
        cancel_token: Any | None,
    ) -> SessionMemorySnapshot:
        """Recall at most once for this run and freeze the merged contribution."""

        run_key = _run_memory_key(state)
        with self._run_condition:
            entry_epoch = self._run_epochs.get(run_key, 0)
            while True:
                if self._run_epochs.get(run_key, 0) != entry_epoch:
                    invalidated = SessionMemorySnapshot(
                        plugin_id=(
                            self._active_descriptor.plugin_id
                            if self._active_descriptor is not None
                            else None
                        ),
                        status="degraded",
                        error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
                    )
                    return self._publish_snapshot_to_state(state, invalidated)
                frozen = self._frozen_run_contexts.get(run_key)
                if frozen is not None:
                    return self._publish_snapshot_to_state(state, frozen)
                if run_key not in self._preparing_runs:
                    self._preparing_runs.add(run_key)
                    run_epoch = entry_epoch
                    break
                self._run_condition.wait()

        started = perf_counter()
        cancellation = _DelegatingCancellationToken(cancel_token)
        call_deadline = (
            monotonic() + self.execution_policy.prepare_context_timeout_seconds
        )
        try:
            try:
                snapshot = self._prepare_context_once(
                    state=state,
                    cancellation=cancellation,
                    call_deadline=call_deadline,
                )
            except Exception:
                snapshot = self._internal_prepare_fallback(state)
            failure_code = _call_failure_code(cancellation, call_deadline)
            if failure_code is not None:
                snapshot = self._prepare_fallback(state, failure_code)
            frozen = snapshot.model_copy(deep=True)
        except BaseException:
            with self._run_condition:
                self._preparing_runs.discard(run_key)
                self._run_condition.notify_all()
            raise

        with self._run_condition:
            failure_code = _call_failure_code(cancellation, call_deadline)
            if failure_code is not None:
                frozen = self._prepare_fallback(
                    state,
                    failure_code,
                ).model_copy(deep=True)
            if self._run_epochs.get(run_key, 0) != run_epoch:
                frozen = SessionMemorySnapshot(
                    plugin_id=(
                        self._active_descriptor.plugin_id
                        if self._active_descriptor is not None
                        else None
                    ),
                    status="degraded",
                    error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
                )
            else:
                self._frozen_run_contexts[run_key] = frozen
            self._preparing_runs.discard(run_key)
            self._run_condition.notify_all()
        descriptor = self._active_descriptor
        if descriptor is not None:
            record_session_recall(
                trace_store=trace_store,
                state=state,
                status=frozen.status,
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
                memory_count=len(frozen.memories),
                error_codes=list(frozen.error_codes),
                memory_plugin_id=descriptor.plugin_id,
                memory_plugin_version=descriptor.plugin_version,
                memory_plugin_api_version=descriptor.api_version,
                memory_plugin_operation="prepare_context",
                memory_plugin_issue_codes=list(frozen.error_codes),
                memory_plugin_retry_count=0,
            )
        return self._publish_snapshot_to_state(state, frozen)

    def _prepare_context_once(
        self,
        *,
        state: AgentState,
        cancellation: MemoryCancellationToken,
        call_deadline: float,
    ) -> SessionMemorySnapshot:
        identity = _identity_from_state(state)
        record = self.session_store.get(identity)
        if record is None:
            if self._active_descriptor is None or self._active_plugin is None:
                return SessionMemorySnapshot(
                    status="degraded",
                    error_codes=[MEMORY_PLUGIN_INTERNAL_ERROR],
                )
            return SessionMemorySnapshot(
                plugin_id=(
                    self._active_descriptor.plugin_id
                    if self._active_descriptor is not None
                    else None
                ),
                status="unavailable",
                error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
            )

        descriptor = self._active_descriptor
        if descriptor is None or self._active_plugin is None:
            return _fallback_snapshot(record, MEMORY_PLUGIN_INTERNAL_ERROR)
        if (
            record.plugin_id != descriptor.plugin_id
            or record.plugin_version != descriptor.plugin_version
        ):
            return _fallback_snapshot(record, MEMORY_PLUGIN_UNAVAILABLE)
        if not descriptor.capabilities.supports_context_refresh:
            return record.baseline.model_copy(deep=True)

        failure_code = _call_failure_code(cancellation, call_deadline)
        if failure_code is not None:
            return _fallback_snapshot(record, failure_code)
        current_text = state.request.text
        if not isinstance(current_text, str) or not current_text:
            return _fallback_snapshot(record, MEMORY_PLUGIN_INTERNAL_ERROR)

        media_refs, media_issues = self._resolve_request_media(
            state=state,
            record=record,
            descriptor=descriptor,
        )
        request = MemoryContextRequest(
            memory_session_id=record.memory_session_id,
            session_handle=record.session_handle,
            identity=record.identity.model_copy(deep=True),
            current_turn=MemoryMessage(role="user", text=current_text),
            media_refs=media_refs,
            context_budget_hint=MemoryBudgetHint(
                max_items=self.execution_policy.max_context_items,
                max_chars=self.execution_policy.max_context_chars,
            ),
            deadline=self._deadline(
                self.execution_policy.prepare_context_timeout_seconds
            ),
            cancellation=cancellation,
        )
        raw, failure_code = self._invoke_session_operation(
            lambda: self._active_plugin.prepare_context(request),
            record=record,
            timeout_seconds=self.execution_policy.prepare_context_timeout_seconds,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
        if failure_code is not None:
            return _fallback_snapshot(
                record,
                failure_code,
                extra_codes=[issue.code for issue in media_issues],
            )

        try:
            _ensure_call_active(cancellation, call_deadline)
            contribution = self._validated_contribution(
                raw,
                descriptor=descriptor,
                owner_scope=record.identity.session_id,
                cancellation=cancellation,
                call_deadline=call_deadline,
            )
        except _MemoryCallStopped as stopped:
            return _fallback_snapshot(
                record,
                stopped.issue_code,
                extra_codes=[issue.code for issue in media_issues],
            )
        if contribution is None:
            return _fallback_snapshot(
                record,
                MEMORY_PLUGIN_INVALID_RESULT,
                extra_codes=[issue.code for issue in media_issues],
            )
        failure_code = _call_failure_code(cancellation, call_deadline)
        if failure_code is not None:
            return _fallback_snapshot(
                record,
                failure_code,
                extra_codes=[issue.code for issue in media_issues],
            )
        try:
            merged = _merge_items(
                record.baseline.memories,
                contribution.items,
                cancellation=cancellation,
                call_deadline=call_deadline,
            )
        except _MemoryCallStopped as stopped:
            return _fallback_snapshot(
                record,
                stopped.issue_code,
                extra_codes=[issue.code for issue in media_issues],
            )
        failure_code = _call_failure_code(cancellation, call_deadline)
        if failure_code is not None:
            return _fallback_snapshot(
                record,
                failure_code,
                extra_codes=[issue.code for issue in media_issues],
            )
        return SessionMemorySnapshot(
            memories=merged,
            plugin_id=record.plugin_id,
            status=contribution.status,
            error_codes=_unique_codes(
                [
                    *record.baseline.error_codes,
                    *(issue.code for issue in media_issues),
                    *(issue.code for issue in contribution.issues),
                ]
            ),
        )

    def attach_frozen_context(
        self,
        state: AgentState,
    ) -> SessionMemorySnapshot | None:
        """Attach an independent copy of this run's immutable source snapshot."""

        run_key = _run_memory_key(state)
        with self._run_condition:
            while run_key in self._preparing_runs:
                self._run_condition.wait()
            frozen = self._frozen_run_contexts.get(run_key)
        if frozen is None:
            record = self.session_store.get(_identity_from_state(state))
            if record is None:
                state.session_memory_snapshot = None
                return None
            frozen = record.baseline.model_copy(deep=True)
            state.session_memory_snapshot = frozen.model_copy(deep=True)
            return frozen.model_copy(deep=True)
        return self._publish_snapshot_to_state(state, frozen)

    def release_run_context(
        self,
        *,
        identity: RequestIdentity,
        run_id: str,
    ) -> bool:
        """Release one terminal run's Host-owned frozen context idempotently."""

        session_key = runtime_memory_identity_key(identity)
        if type(run_id) is not str or not run_id:
            raise ValueError("run_id must be a non-empty string")
        run_key = (*session_key, run_id)
        with self._run_condition:
            released = self._frozen_run_contexts.pop(run_key, None) is not None
            if run_key in self._preparing_runs:
                self._run_epochs[run_key] = self._run_epochs.get(run_key, 0) + 1
            else:
                self._run_epochs.pop(run_key, None)
            self._run_condition.notify_all()
            return released

    def schedule_ingestion(
        self,
        *,
        state: AgentState,
        trace_store: TraceStore | None,
    ) -> bool:
        """Capture and enqueue one completed turn without retaining AgentState."""

        descriptor = self._active_descriptor
        if (
            descriptor is None
            or self._active_plugin is None
            or not descriptor.capabilities.supports_turn_ingestion
        ):
            state.request.metadata["memory_ingestion"] = {"status": "skipped"}
            return False
        skip_reason = _structured_ingestion_skip_reason(state)
        if skip_reason is not None:
            state.request.metadata["memory_ingestion"] = {
                "status": "skipped",
                "reason": skip_reason,
            }
            return False

        identity = _identity_from_state(state)
        ordering_key = runtime_memory_identity_key(identity)
        with self._lifecycle_condition:
            if (
                not self._host_accepting
                or ordering_key in self._closing_sessions
                or ordering_key in self._session_admission_closed
                or ordering_key in self._closed_session_results
            ):
                state.request.metadata["memory_ingestion"] = {
                    "status": "failed",
                    "error_code": "memory_plugin_session_closed",
                }
                return False
            record = self.session_store.get(identity)
            if (
                record is None
                or record.plugin_id != descriptor.plugin_id
                or record.plugin_version != descriptor.plugin_version
            ):
                state.request.metadata["memory_ingestion"] = {"status": "skipped"}
                return False
            if record.status == "unavailable":
                state.request.metadata["memory_ingestion"] = {
                    "status": "skipped",
                    "reason": "memory_plugin_unavailable",
                }
                return False
            scheduled = self._scheduled_ingestion(
                state=state,
                record=record,
                descriptor=descriptor,
                trace_store=trace_store,
            )
            if scheduled is None:
                state.request.metadata["memory_ingestion"] = {"status": "skipped"}
                return False

            operation_id = self._acquire_session_operation_locked(record)
            if operation_id is None:
                state.request.metadata["memory_ingestion"] = {
                    "status": "failed",
                    "error_code": "memory_plugin_session_closed",
                }
                return False

            start_gate = Event()
            submitted = self.ingestion_queue.submit(
                ordering_key=scheduled.ordering_key,
                callback=lambda: self._run_scheduled_ingestion_after_gate(
                    scheduled,
                    start_gate,
                    operation_id,
                ),
            )
            if not submitted.accepted:
                self._release_session_operation_locked(
                    ordering_key,
                    operation_id,
                )
                state.request.metadata["memory_ingestion"] = {
                    "status": "failed",
                    "error_code": submitted.reason,
                }
                return False
            state.request.metadata["memory_ingestion"] = {
                "status": "queued",
                "pending_count": submitted.pending_count,
            }
            record_ingestion_queued(
                trace_store=trace_store,
                context=scheduled.observation_context,
                pending_count=submitted.pending_count,
                memory_plugin_id=scheduled.plugin_id,
                memory_plugin_version=scheduled.plugin_version,
                memory_plugin_api_version=scheduled.api_version,
                memory_plugin_operation="ingest_turn",
                memory_plugin_issue_codes=list(scheduled.initial_issue_codes),
                memory_plugin_retry_count=0,
            )
            start_gate.set()
            return True

    def drain(self, *, timeout: float | None = None) -> bool:
        """Drain accepted ingestion work within an explicit Host bound."""

        return self.ingestion_queue.drain(timeout=self._bounded_timeout(timeout))

    def close_session(
        self,
        *,
        identity: RequestIdentity,
        reason: _SessionCloseReason = "normal",
        timeout: float | None = None,
    ) -> MemorySessionCloseResult:
        """Stop session ingestion, drain accepted work, and close exactly once."""

        ordering_key = runtime_memory_identity_key(identity)
        wait_timeout = self._bounded_timeout(timeout)
        wait_deadline = monotonic() + wait_timeout
        with self._lifecycle_condition:
            self._adopt_session_store_retirements_locked()
            self._harvest_completed_active_closes_locked(ordering_key)
            cached = self._closed_session_results.get(ordering_key)
            if cached is not None:
                return cached.model_copy(deep=True)
            while self._host_close_in_progress:
                remaining = wait_deadline - monotonic()
                if remaining <= 0:
                    return _close_failure(MEMORY_PLUGIN_TIMEOUT, partial=True)
                self._lifecycle_condition.wait(remaining)
                self._harvest_completed_active_closes_locked(ordering_key)
                cached = self._closed_session_results.get(ordering_key)
                if cached is not None:
                    return cached.model_copy(deep=True)
            while True:
                self._harvest_completed_active_closes_locked(ordering_key)
                if ordering_key not in self._closing_sessions:
                    break
                remaining = wait_deadline - monotonic()
                if remaining <= 0:
                    return _close_failure(MEMORY_PLUGIN_TIMEOUT, partial=True)
                self._lifecycle_condition.wait(remaining)
                self._harvest_completed_active_closes_locked(ordering_key)
                cached = self._closed_session_results.get(ordering_key)
                if cached is not None:
                    return cached.model_copy(deep=True)
            self._session_admission_closed.add(ordering_key)
            self._session_close_reasons.setdefault(ordering_key, reason)
            stable_reason = self._session_close_reasons[ordering_key]
            self._mark_pending_reaps_terminal_locked(ordering_key)
            self._closing_sessions.add(ordering_key)
            for invalidation in self._opening_sessions.get(ordering_key, ()):
                invalidation.set()
            opening_drained = True
            while self._opening_sessions.get(ordering_key):
                remaining = wait_deadline - monotonic()
                if remaining <= 0:
                    opening_drained = False
                    break
                self._lifecycle_condition.wait(remaining)
            cached = self._closed_session_results.get(ordering_key)
            if cached is not None:
                self._closing_sessions.discard(ordering_key)
                self._lifecycle_condition.notify_all()
                return cached.model_copy(deep=True)

        return self._finish_session_close_owner(
            identity=identity,
            ordering_key=ordering_key,
            reason=stable_reason,
            wait_deadline=wait_deadline,
            opening_drained=opening_drained,
            cache_terminal=True,
            include_stored=True,
            drain_ingestion=True,
        )

    def _finish_session_close_owner(
        self,
        *,
        identity: RequestIdentity,
        ordering_key: _SessionMemoryKey,
        reason: _SessionCloseReason,
        wait_deadline: float,
        opening_drained: bool,
        cache_terminal: bool,
        include_stored: bool,
        drain_ingestion: bool,
    ) -> MemorySessionCloseResult:
        if not opening_drained:
            self._release_session_close_owner(ordering_key)
            return _close_failure(MEMORY_PLUGIN_TIMEOUT, partial=True)

        if not self._wait_for_active_session_opens(
            ordering_key,
            wait_deadline=wait_deadline,
        ):
            self._release_session_close_owner(
                ordering_key,
                retry_eager=True,
            )
            return _close_failure(MEMORY_PLUGIN_TIMEOUT, partial=True)

        if drain_ingestion:
            try:
                drained = self.ingestion_queue.drain(
                    ordering_key=ordering_key,
                    timeout=max(0.0, wait_deadline - monotonic()),
                )
            except Exception:
                self._release_session_close_owner(ordering_key)
                return _close_failure(MEMORY_PLUGIN_INTERNAL_ERROR, partial=True)
            if not drained:
                self._release_session_close_owner(
                    ordering_key,
                    retry_eager=True,
                )
                return _close_failure(MEMORY_PLUGIN_TIMEOUT, partial=True)

        if not self._wait_for_session_operations(
            ordering_key,
            wait_deadline=wait_deadline,
        ):
            self._release_session_close_owner(
                ordering_key,
                retry_eager=True,
            )
            return _close_failure(MEMORY_PLUGIN_TIMEOUT, partial=True)

        terminal_result = MemorySessionCloseResult(status="closed")
        while True:
            with self._lifecycle_condition:
                pending = self._next_pending_reap_locked(ordering_key)
            if pending is not None:
                result, deferred = self._close_plugin_session(
                    record=pending.record,
                    reason=pending.reason,
                    source="pending",
                    pending_reap_id=pending.reap_id,
                    cache_terminal=(cache_terminal or pending.cache_terminal),
                    timeout_seconds=max(0.0, wait_deadline - monotonic()),
                )
                if result.status != "closed":
                    if not deferred:
                        self._release_session_close_owner(ordering_key)
                    return result.model_copy(deep=True)
                terminal_result = result.model_copy(deep=True)
                with self._lifecycle_condition:
                    current = self._pending_reaps.get(pending.reap_id)
                    if current is pending:
                        cache_terminal = cache_terminal or current.cache_terminal
                        self._pending_reaps.pop(pending.reap_id, None)
                    self._lifecycle_condition.notify_all()
                continue

            record = self.session_store.get(identity) if include_stored else None
            if record is not None:
                result, deferred = self._close_plugin_session(
                    record=record,
                    reason=reason,
                    source="stored",
                    pending_reap_id=None,
                    cache_terminal=cache_terminal,
                    timeout_seconds=max(0.0, wait_deadline - monotonic()),
                )
                if result.status != "closed":
                    if not deferred:
                        self._release_session_close_owner(ordering_key)
                    return result.model_copy(deep=True)
                terminal_result = result.model_copy(deep=True)
                self.session_store.pop(identity)
                continue

            with self._lifecycle_condition:
                if self._next_pending_reap_locked(ordering_key) is not None:
                    continue
            break

        if include_stored or cache_terminal:
            self._clear_frozen_contexts(lambda run_key: run_key[:3] == ordering_key)
        with self._lifecycle_condition:
            if cache_terminal:
                self._closed_session_results[ordering_key] = terminal_result.model_copy(
                    deep=True
                )
            clear_barrier_waiting = (
                ordering_key in self._clear_admission_barriers
                and bool(self._opening_sessions.get(ordering_key))
            )
            if not clear_barrier_waiting:
                self._clear_admission_barriers.discard(ordering_key)
                self._session_admission_closed.discard(ordering_key)
                self._session_close_reasons.pop(ordering_key, None)
            self._closing_sessions.discard(ordering_key)
            self._lifecycle_condition.notify_all()
        return terminal_result.model_copy(deep=True)

    def _release_session_close_owner(
        self,
        ordering_key: _SessionMemoryKey,
        *,
        retry_eager: bool = False,
    ) -> None:
        with self._lifecycle_condition:
            self._closing_sessions.discard(ordering_key)
            if retry_eager and not self._active_session_operations.get(ordering_key):
                self._request_maintenance_reap_locked(ordering_key=ordering_key)
            self._lifecycle_condition.notify_all()

    def clear_session(self, *, user_id: str, session_id: str) -> int:
        """Clear only Host-owned state; never issue remote memory CRUD."""

        with self._lifecycle_condition:
            self._harvest_completed_active_closes_locked()
            if (
                not self._host_accepting
                or any(
                    key[0] == user_id and key[2] == session_id
                    for key in self._session_admission_closed
                )
                or any(
                    key[0] == user_id and key[2] == session_id
                    for key in self._closing_sessions
                )
                or any(
                    pending.record.runtime_identity_key[0] == user_id
                    and pending.record.runtime_identity_key[2] == session_id
                    for pending in self._pending_reaps.values()
                )
                or any(
                    key[0] == user_id and key[2] == session_id
                    for key in self._active_session_closes
                )
            ):
                return 0
            for key, invalidations in self._opening_sessions.items():
                if key[0] == user_id and key[2] == session_id:
                    self._clear_admission_barriers.add(key)
                    self._session_admission_closed.add(key)
                    for invalidation in invalidations:
                        invalidation.set()
            cleared = self.session_store.clear_session(
                user_id=user_id,
                session_id=session_id,
            )
            self._clear_frozen_contexts(
                lambda run_key: run_key[0] == user_id and run_key[2] == session_id
            )
            return cleared

    def clear_user(self, *, user_id: str, agent_id: str | None = None) -> int:
        """Clear one user's Host state without granting Plugin deletion rights."""

        with self._lifecycle_condition:
            self._harvest_completed_active_closes_locked()
            if (
                not self._host_accepting
                or any(
                    key[0] == user_id and (agent_id is None or key[1] == agent_id)
                    for key in self._session_admission_closed
                )
                or any(
                    key[0] == user_id and (agent_id is None or key[1] == agent_id)
                    for key in self._closing_sessions
                )
                or any(
                    pending.record.runtime_identity_key[0] == user_id
                    and (
                        agent_id is None
                        or pending.record.runtime_identity_key[1] == agent_id
                    )
                    for pending in self._pending_reaps.values()
                )
                or any(
                    key[0] == user_id and (agent_id is None or key[1] == agent_id)
                    for key in self._active_session_closes
                )
            ):
                return 0
            for key, invalidations in self._opening_sessions.items():
                if key[0] == user_id and (agent_id is None or key[1] == agent_id):
                    self._clear_admission_barriers.add(key)
                    self._session_admission_closed.add(key)
                    for invalidation in invalidations:
                        invalidation.set()
            cleared = self.session_store.clear_user(
                user_id=user_id,
                agent_id=agent_id,
            )
            self._clear_frozen_contexts(
                lambda run_key: (
                    run_key[0] == user_id
                    and (agent_id is None or run_key[1] == agent_id)
                )
            )
            return cleared

    def close(self, *, timeout: float | None = None) -> bool:
        """Close every session and worker within one retryable Host deadline."""

        close_timeout = self._bounded_timeout(timeout)
        started = monotonic()
        close_deadline = started + close_timeout
        with self._lifecycle_condition:
            self._adopt_session_store_retirements_locked()
            self._harvest_completed_active_opens_locked()
            self._harvest_completed_active_closes_locked()
            while self._host_close_in_progress:
                remaining = close_timeout - (monotonic() - started)
                if remaining <= 0:
                    return False
                self._lifecycle_condition.wait(remaining)
            if self._host_close_result is True:
                return True
            self._host_close_in_progress = True
            self._host_accepting = False
            self._maintenance_stopping = True
            self._maintenance_requested = False
            for invalidations in self._opening_sessions.values():
                for invalidation in invalidations:
                    invalidation.set()
            self._lifecycle_condition.notify_all()

        queue_closed = self.ingestion_queue.close(
            timeout=_remaining_timeout(started, close_timeout)
        )
        with self._lifecycle_condition:
            while self._opening_sessions:
                remaining = _remaining_timeout(started, close_timeout)
                if remaining <= 0:
                    break
                self._lifecycle_condition.wait(remaining)
            self._adopt_session_store_retirements_locked()
            self._harvest_completed_active_opens_locked()
            openings_drained = not self._opening_sessions
        if not queue_closed or not openings_drained:
            return self._finish_host_close_attempt(False)

        with self._lifecycle_condition:
            while True:
                self._harvest_completed_active_closes_locked()
                if not self._closing_sessions:
                    break
                remaining = _remaining_timeout(started, close_timeout)
                if remaining <= 0:
                    break
                self._lifecycle_condition.wait(remaining)
            self._harvest_completed_active_closes_locked()
            session_owners_drained = not self._closing_sessions
        if not session_owners_drained:
            return self._finish_host_close_attempt(False)

        while True:
            with self._lifecycle_condition:
                self._adopt_session_store_retirements_locked()
                published = self.session_store.list_records()
                remaining_keys = {record.runtime_identity_key for record in published}
                remaining_keys.update(
                    pending.record.runtime_identity_key
                    for pending in self._pending_reaps.values()
                )
                remaining_keys.update(
                    active.fallback_record.runtime_identity_key
                    for active in self._active_session_opens.values()
                )
                if not remaining_keys:
                    break
                ordering_key = min(remaining_keys)
                self._session_admission_closed.add(ordering_key)
                self._session_close_reasons.setdefault(ordering_key, "shutdown")
                stable_reason = self._session_close_reasons[ordering_key]
                self._mark_pending_reaps_terminal_locked(ordering_key)
                self._closing_sessions.add(ordering_key)
            if _remaining_timeout(started, close_timeout) <= 0:
                self._release_session_close_owner(ordering_key)
                return self._finish_host_close_attempt(False)
            identity = _identity_from_runtime_key(ordering_key)
            result = self._finish_session_close_owner(
                identity=identity,
                ordering_key=ordering_key,
                reason=stable_reason,
                wait_deadline=close_deadline,
                opening_drained=True,
                cache_terminal=True,
                include_stored=True,
                drain_ingestion=True,
            )
            if result.status != "closed":
                return self._finish_host_close_attempt(False)

        with self._lifecycle_condition:
            self._adopt_session_store_retirements_locked()
            self._harvest_completed_active_opens_locked()
            handles_remain = bool(
                self._pending_reaps
                or self._active_session_opens
                or self._active_session_closes
                or self._active_session_operations
                or self.session_store.list_records()
                or self.session_store.list_retired_records()
            )
        if handles_remain:
            return self._finish_host_close_attempt(False)

        with self._lifecycle_condition:
            maintenance_threads = tuple(self._maintenance_threads)
        for maintenance_thread in maintenance_threads:
            maintenance_thread.join(max(0.0, close_deadline - monotonic()))
            if maintenance_thread.is_alive():
                return self._finish_host_close_attempt(False)
        with self._lifecycle_condition:
            self._maintenance_threads.difference_update(maintenance_threads)

        with self._executor_condition:
            self._executor_accepting = False
            while self._executor_futures:
                remaining = _remaining_timeout(started, close_timeout)
                if remaining <= 0:
                    break
                self._executor_condition.wait(remaining)
            executor_drained = not self._executor_futures
            if not executor_drained:
                self._executor_accepting = True
                self._executor_condition.notify_all()
        if not executor_drained:
            return self._finish_host_close_attempt(False)

        self._executor.shutdown(wait=True, cancel_futures=True)
        return self._finish_host_close_attempt(True)

    def _finish_host_close_attempt(self, result: bool) -> bool:
        with self._lifecycle_condition:
            self._host_close_result = result
            self._host_close_in_progress = False
            self._lifecycle_condition.notify_all()
        return result

    def _scheduled_ingestion(
        self,
        *,
        state: AgentState,
        record: MemoryPluginSessionRecord,
        descriptor: MemoryPluginDescriptor,
        trace_store: TraceStore | None,
    ) -> _ScheduledMemoryIngestion | None:
        response = state.response
        if state.status != "completed" or response is None:
            return None
        user_text = state.request.text
        assistant_text = response.message
        if not _valid_memory_message_text(user_text) or not _valid_memory_message_text(
            assistant_text
        ):
            return None

        evidence: list[MemoryToolEvidence] = []
        for result in state.tool_results:
            tool_name = result.tool_name
            if not _safe_bounded_string(tool_name, max_chars=128):
                continue
            output_ref = result.output_ref
            if not _safe_bounded_string(output_ref, max_chars=512):
                output_ref = None
            status: Literal["succeeded", "failed", "partial"]
            if not result.success:
                status = "failed"
            elif (
                isinstance(result.data, dict) and result.data.get("status") == "partial"
            ):
                status = "partial"
            else:
                status = "succeeded"
            evidence.append(
                MemoryToolEvidence(
                    tool_name=tool_name,
                    status=status,
                    output_ref=output_ref,
                )
            )

        media_refs, media_issues = self._resolve_request_media(
            state=state,
            record=record,
            descriptor=descriptor,
        )
        turn_index = _conversation_turn_index(state)
        idempotency_key = hashlib.sha256(
            f"{descriptor.plugin_id}{state.run_id}{turn_index}".encode()
        ).hexdigest()
        try:
            request = MemoryTurnIngestionRequest(
                memory_session_id=record.memory_session_id,
                session_handle=record.session_handle,
                identity=record.identity.model_copy(deep=True),
                turn=CompletedMemoryTurn(
                    user_message=MemoryMessage(role="user", text=user_text),
                    assistant_message=MemoryMessage(
                        role="assistant",
                        text=assistant_text,
                    ),
                    tool_evidence=evidence,
                    media_refs=media_refs,
                    occurred_at=self._now(),
                ),
                idempotency_key=idempotency_key,
                deadline=self._deadline(
                    self.execution_policy.ingest_turn_timeout_seconds
                ),
                cancellation=_DelegatingCancellationToken(None),
            )
        except Exception:
            return None
        return _ScheduledMemoryIngestion(
            request=request.model_copy(deep=True),
            ordering_key=record.runtime_identity_key,
            trace_store=trace_store,
            observation_context=MemoryObservationContext.from_state(state),
            plugin_id=descriptor.plugin_id,
            plugin_version=descriptor.plugin_version,
            api_version=descriptor.api_version,
            initial_issue_codes=tuple(issue.code for issue in media_issues),
        )

    def _run_scheduled_ingestion_after_gate(
        self,
        scheduled: _ScheduledMemoryIngestion,
        start_gate: Event,
        operation_id: int,
    ) -> None:
        try:
            start_gate.wait()
            self._run_scheduled_ingestion(scheduled)
        finally:
            self._release_session_operation(
                scheduled.ordering_key,
                operation_id,
            )

    def _run_scheduled_ingestion(
        self,
        scheduled: _ScheduledMemoryIngestion,
    ) -> None:
        started = perf_counter()
        descriptor = self._active_descriptor
        retry_count = 0
        result: MemoryTurnIngestionResult | None = None
        failure_code: str | None = None
        while True:
            call_deadline = (
                monotonic() + self.execution_policy.ingest_turn_timeout_seconds
            )
            raw, failure_code = self._invoke_ingestion(
                lambda: self._active_plugin.ingest_turn(scheduled.request),
                timeout_seconds=self.execution_policy.ingest_turn_timeout_seconds,
                cancellation=scheduled.request.cancellation,
                call_deadline=call_deadline,
            )
            if failure_code is None:
                result = self._validated_ingestion_result(raw)
                if result is None:
                    failure_code = MEMORY_PLUGIN_INVALID_RESULT
            retryable = (
                descriptor is not None
                and descriptor.capabilities.supports_idempotent_ingestion
                and retry_count == 0
                and (
                    failure_code
                    in {
                        MEMORY_PLUGIN_TIMEOUT,
                        MEMORY_PLUGIN_UNAVAILABLE,
                        MEMORY_PLUGIN_INTERNAL_ERROR,
                    }
                    or (
                        result is not None
                        and any(issue.recoverable for issue in result.issues)
                    )
                )
            )
            if retryable:
                retry_count += 1
                result = None
                failure_code = None
                continue
            break

        issue_codes = list(scheduled.initial_issue_codes)
        if result is not None:
            issue_codes.extend(issue.code for issue in result.issues)
            status = {
                "accepted": "succeeded",
                "partial": "partial",
                "rejected": "failed",
                "failed": "failed",
            }[result.status]
            changes: list[MemoryChange] = list(result.changes)
        else:
            issue_codes.append(failure_code or MEMORY_PLUGIN_INTERNAL_ERROR)
            status = "failed"
            changes = []
        record_ingestion_finished(
            trace_store=scheduled.trace_store,
            context=scheduled.observation_context,
            status=status,
            latency_ms=max(0, int((perf_counter() - started) * 1000)),
            changes=changes,
            source_turn=scheduled.request.idempotency_key[:24],
            source_user_text=scheduled.request.turn.user_message.text,
            source_assistant_text=(
                scheduled.request.turn.assistant_message.text
            ),
            error_code=failure_code if result is None else None,
            memory_plugin_id=scheduled.plugin_id,
            memory_plugin_version=scheduled.plugin_version,
            memory_plugin_api_version=scheduled.api_version,
            memory_plugin_operation="ingest_turn",
            memory_plugin_issue_codes=_unique_codes(issue_codes),
            memory_plugin_retry_count=retry_count,
        )

    def _validated_ingestion_result(
        self,
        raw: object,
    ) -> MemoryTurnIngestionResult | None:
        if not _raw_result_is_safe(raw):
            return None
        result = _round_trip_model(raw, MemoryTurnIngestionResult)
        if result is None:
            return None
        for issue in result.issues:
            if not _valid_issue(issue):
                return None
        for change in result.changes:
            if _forbidden_string(change.memory_id):
                return None
            if change.memory_type is not None and _forbidden_string(change.memory_type):
                return None
        return result.model_copy(deep=True)

    def _execute_open_call(
        self,
        callback: Any,
        *,
        identity: RequestIdentity,
        descriptor: MemoryPluginDescriptor,
        plugin_identity: MemoryIdentity,
        memory_session_id: str,
    ) -> _OpenCallOutcome:
        try:
            raw = callback()
        except Exception:
            return _OpenCallOutcome(
                raw=None,
                failure_code=MEMORY_PLUGIN_INTERNAL_ERROR,
                cleanup_record=None,
            )
        cleanup_record = self._cleanup_open_record(
            raw,
            identity=identity,
            descriptor=descriptor,
            plugin_identity=plugin_identity,
            memory_session_id=memory_session_id,
        )
        return _OpenCallOutcome(
            raw=raw,
            failure_code=None,
            cleanup_record=cleanup_record,
        )

    @staticmethod
    def _cleanup_open_record(
        raw: object,
        *,
        identity: RequestIdentity,
        descriptor: MemoryPluginDescriptor,
        plugin_identity: MemoryIdentity,
        memory_session_id: str,
    ) -> MemoryPluginSessionRecord | None:
        if type(raw) is not MemorySessionOpenResult:
            return None
        try:
            raw_values = object.__getattribute__(raw, "__dict__")
            session_handle = dict.__getitem__(raw_values, "session_handle")
        except Exception:
            return None
        if session_handle is not None and (
            type(session_handle) is not str
            or len(session_handle) > 512
            or _forbidden_string(session_handle)
        ):
            return None
        return _degraded_open_record(
            identity=identity,
            descriptor=descriptor,
            plugin_identity=plugin_identity,
            memory_session_id=memory_session_id,
            issue_code=MEMORY_PLUGIN_INVALID_RESULT,
            session_handle=session_handle,
        )

    def _close_plugin_session(
        self,
        *,
        record: MemoryPluginSessionRecord,
        reason: _SessionCloseReason,
        source: _SessionCloseSource,
        pending_reap_id: int | None,
        cache_terminal: bool,
        timeout_seconds: float | None = None,
    ) -> tuple[MemorySessionCloseResult, bool]:
        descriptor = self._active_descriptor
        if (
            descriptor is None
            or self._active_plugin is None
            or record.plugin_id != descriptor.plugin_id
            or record.plugin_version != descriptor.plugin_version
        ):
            return _close_failure(MEMORY_PLUGIN_UNAVAILABLE), False
        resolved_timeout = self.execution_policy.close_session_timeout_seconds
        if timeout_seconds is not None:
            resolved_timeout = min(resolved_timeout, max(0.0, timeout_seconds))
        if resolved_timeout <= 0:
            return _close_failure(MEMORY_PLUGIN_TIMEOUT), False
        cancellation = _DelegatingCancellationToken(None)
        request = MemorySessionCloseRequest(
            memory_session_id=record.memory_session_id,
            session_handle=record.session_handle,
            identity=record.identity.model_copy(deep=True),
            reason=reason,
            deadline=self._deadline(resolved_timeout),
            cancellation=cancellation,
        )
        call_deadline = monotonic() + resolved_timeout
        raw, failure_code, deferred = self._invoke_owned_close(
            lambda: self._execute_close_call(
                lambda: self._active_plugin.close_session(request),
                cancellation=cancellation,
                call_deadline=call_deadline,
            ),
            record=record,
            reason=reason,
            source=source,
            pending_reap_id=pending_reap_id,
            cache_terminal=cache_terminal,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
        if failure_code is not None:
            return _close_failure(failure_code), deferred
        if type(raw) is not MemorySessionCloseResult:
            return _close_failure(MEMORY_PLUGIN_INTERNAL_ERROR), False
        return raw, False

    def _execute_close_call(
        self,
        callback: Any,
        *,
        cancellation: MemoryCancellationToken,
        call_deadline: float,
    ) -> MemorySessionCloseResult:
        try:
            _ensure_call_active(cancellation, call_deadline)
            raw = callback()
        except _MemoryCallStopped as stopped:
            return _close_failure(stopped.issue_code)
        except Exception:
            return _close_failure(MEMORY_PLUGIN_INTERNAL_ERROR)

        failure_code = _call_failure_code(cancellation, call_deadline)
        validation_deadline = call_deadline
        if failure_code == MEMORY_PLUGIN_TIMEOUT:
            # The caller has already timed out, but the Host still owns this
            # future. Validate a late cleanup result in the worker under a new
            # policy-sized bound so harvest never parses Plugin data while
            # holding the lifecycle lock.
            validation_deadline = (
                monotonic() + self.execution_policy.close_session_timeout_seconds
            )
        elif failure_code is not None:
            return _close_failure(failure_code)
        try:
            result = self._validated_close_result(
                raw,
                cancellation=cancellation,
                call_deadline=validation_deadline,
            )
            _ensure_call_active(cancellation, validation_deadline)
        except _MemoryCallStopped as stopped:
            return _close_failure(stopped.issue_code)
        if result is None:
            return _close_failure(MEMORY_PLUGIN_INVALID_RESULT)
        return result

    @staticmethod
    def _validated_close_result(
        raw: object,
        *,
        cancellation: MemoryCancellationToken | None = None,
        call_deadline: float | None = None,
    ) -> MemorySessionCloseResult | None:
        _ensure_call_active(cancellation, call_deadline)
        if type(raw) is not MemorySessionCloseResult:
            return None
        try:
            raw_values = object.__getattribute__(raw, "__dict__")
            raw_issues = dict.__getitem__(raw_values, "issues")
        except Exception:
            return None
        if type(raw_issues) is not list or len(raw_issues) > _MAX_CLOSE_RESULT_ISSUES:
            return None
        if not _raw_result_is_safe(
            raw,
            cancellation=cancellation,
            call_deadline=call_deadline,
        ):
            return None
        _ensure_call_active(cancellation, call_deadline)
        result = _round_trip_model(
            raw,
            MemorySessionCloseResult,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
        if result is None:
            return None
        for issue in result.issues:
            _ensure_call_active(cancellation, call_deadline)
            if not _valid_issue(issue):
                return None
        _ensure_call_active(cancellation, call_deadline)
        frozen = result.model_copy(deep=True)
        _ensure_call_active(cancellation, call_deadline)
        return frozen

    def _clear_frozen_contexts(self, predicate: Any) -> None:
        with self._run_condition:
            keys = {
                key
                for key in (*self._frozen_run_contexts, *self._preparing_runs)
                if predicate(key)
            }
            for key in keys:
                self._frozen_run_contexts.pop(key, None)
                self._run_epochs[key] = self._run_epochs.get(key, 0) + 1
            self._run_condition.notify_all()

    def _bounded_timeout(self, timeout: float | None) -> float:
        if timeout is None:
            return self.ingestion_queue.shutdown_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise TypeError("timeout must be a number")
        return max(0.0, float(timeout))

    def _internal_prepare_fallback(
        self,
        state: AgentState,
    ) -> SessionMemorySnapshot:
        return self._prepare_fallback(state, MEMORY_PLUGIN_INTERNAL_ERROR)

    def _prepare_fallback(
        self,
        state: AgentState,
        issue_code: str,
    ) -> SessionMemorySnapshot:
        try:
            record = self.session_store.get(_identity_from_state(state))
        except Exception:
            record = None
        if record is not None:
            return _fallback_snapshot(record, issue_code)
        return SessionMemorySnapshot(
            plugin_id=(
                self._active_descriptor.plugin_id
                if self._active_descriptor is not None
                else None
            ),
            status="degraded",
            error_codes=[issue_code],
        )

    @staticmethod
    def _publish_snapshot_to_state(
        state: AgentState,
        snapshot: SessionMemorySnapshot,
    ) -> SessionMemorySnapshot:
        state.frozen_memory_context = snapshot.model_copy(deep=True)
        state.session_memory_snapshot = snapshot.model_copy(deep=True)
        state.memory_context_prepared = True
        return snapshot.model_copy(deep=True)

    def _complete_open(
        self,
        *,
        identity: RequestIdentity,
        session_key: _SessionMemoryKey,
        invalidation: Event,
        snapshot: SessionMemorySnapshot,
        descriptor: MemoryPluginDescriptor,
    ) -> SessionMemorySnapshot:
        with self._lifecycle_condition:
            owns_deferred_open_barrier = any(
                active.invalidation is invalidation and active.deferred
                for active in self._active_session_opens.values()
            )
            lease_invalidated = self.session_store.release_resolution(
                identity,
                invalidation,
            )
            self._adopt_session_store_retirements_locked()
            invalidated = (
                lease_invalidated
                or invalidation.is_set()
                or not self._host_accepting
                or session_key in self._closing_sessions
                or (
                    session_key in self._session_admission_closed
                    and not owns_deferred_open_barrier
                )
            )
            self._discard_opening_locked(
                session_key=session_key,
                invalidation=invalidation,
            )
        if not invalidated:
            return snapshot
        return SessionMemorySnapshot(
            plugin_id=descriptor.plugin_id,
            status="degraded",
            error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
        )

    def _degrade_open_while_reap_pending(
        self,
        *,
        session_key: _SessionMemoryKey,
        snapshot: SessionMemorySnapshot,
        descriptor: MemoryPluginDescriptor,
    ) -> SessionMemorySnapshot:
        """Do not advertise a reset session while its old handle is still live."""

        with self._lifecycle_condition:
            self._adopt_session_store_retirements_locked()
            pending = self._next_pending_reap_locked(session_key) is not None
        if not pending:
            return snapshot
        return SessionMemorySnapshot(
            plugin_id=descriptor.plugin_id,
            status="degraded",
            error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
        )

    def _discard_opening(
        self,
        *,
        session_key: _SessionMemoryKey,
        invalidation: Event,
    ) -> None:
        with self._lifecycle_condition:
            self._discard_opening_locked(
                session_key=session_key,
                invalidation=invalidation,
            )

    def _discard_opening_locked(
        self,
        *,
        session_key: _SessionMemoryKey,
        invalidation: Event,
    ) -> None:
        self._adopt_session_store_retirements_locked()
        openings = self._opening_sessions.get(session_key)
        if openings is not None:
            openings.discard(invalidation)
        if not openings:
            self._opening_sessions.pop(session_key, None)
        for active in self._active_session_opens.values():
            if active.invalidation is invalidation and active.deferred:
                active.publication_done = True
                self._request_maintenance_reap_locked(ordering_key=session_key)
        self._release_clear_admission_if_drained_locked(session_key)
        self._lifecycle_condition.notify_all()

    def _release_clear_admission_if_drained_locked(
        self,
        session_key: _SessionMemoryKey,
    ) -> None:
        if session_key not in self._clear_admission_barriers:
            return
        if (
            self._opening_sessions.get(session_key)
            or self._next_pending_reap_locked(session_key) is not None
            or session_key in self._closing_sessions
        ):
            return
        self._clear_admission_barriers.discard(session_key)
        self._session_admission_closed.discard(session_key)
        self._session_close_reasons.pop(session_key, None)

    def _open_record_if_current(
        self,
        *,
        identity: RequestIdentity,
        state: AgentState,
        descriptor: MemoryPluginDescriptor,
        cancellation: MemoryCancellationToken,
        call_deadline: float,
        invalidation: Event,
    ) -> MemoryPluginSessionRecord:
        if invalidation.is_set():
            raise MemoryPluginSessionLoadInvalidated()
        return self._open_record(
            identity=identity,
            state=state,
            descriptor=descriptor,
            cancellation=cancellation,
            call_deadline=call_deadline,
            invalidation=invalidation,
        )

    def _reap_invalidated_open(
        self,
        *,
        identity: RequestIdentity,
        record: MemoryPluginSessionRecord,
    ) -> None:
        ordering_key = runtime_memory_identity_key(identity)
        with self._lifecycle_condition:
            terminal_requested = (
                not self._host_accepting
                or self._host_close_in_progress
                or ordering_key in self._session_close_reasons
            )
            reason = self._session_close_reasons.get(
                ordering_key,
                "shutdown" if not self._host_accepting else "reset",
            )
            self._register_pending_reap_locked(
                record=record,
                reason=reason,
                cache_terminal=terminal_requested,
            )
            self._session_admission_closed.add(ordering_key)
            self._session_close_reasons.setdefault(ordering_key, reason)
            if self._host_close_in_progress or ordering_key in self._closing_sessions:
                self._lifecycle_condition.notify_all()
                return
            self._closing_sessions.add(ordering_key)
            self._lifecycle_condition.notify_all()

        self._finish_session_close_owner(
            identity=identity,
            ordering_key=ordering_key,
            reason=reason,
            wait_deadline=(
                monotonic() + self.execution_policy.close_session_timeout_seconds
            ),
            opening_drained=True,
            cache_terminal=terminal_requested,
            include_stored=terminal_requested,
            drain_ingestion=terminal_requested,
        )

    def _acquire_session_operation_locked(
        self,
        record: MemoryPluginSessionRecord,
    ) -> int | None:
        """Lease one published handle before dispatching Plugin work."""

        ordering_key = record.runtime_identity_key
        if (
            not self._host_accepting
            or self._host_close_in_progress
            or ordering_key in self._closing_sessions
            or ordering_key in self._session_admission_closed
            or ordering_key in self._closed_session_results
        ):
            return None
        current = self.session_store.get(_identity_from_runtime_key(ordering_key))
        if current != record:
            return None
        self._next_session_operation_id += 1
        operation_id = self._next_session_operation_id
        self._active_session_operations.setdefault(ordering_key, set()).add(
            operation_id
        )
        return operation_id

    def _release_session_operation(
        self,
        ordering_key: _SessionMemoryKey,
        operation_id: int,
    ) -> None:
        with self._lifecycle_condition:
            self._release_session_operation_locked(ordering_key, operation_id)

    def _release_session_operation_locked(
        self,
        ordering_key: _SessionMemoryKey,
        operation_id: int,
    ) -> None:
        operations = self._active_session_operations.get(ordering_key)
        if operations is not None:
            operations.discard(operation_id)
            if not operations:
                self._active_session_operations.pop(ordering_key, None)
        if (
            ordering_key not in self._active_session_operations
            and ordering_key not in self._closing_sessions
        ):
            self._request_maintenance_reap_locked(ordering_key=ordering_key)
        self._lifecycle_condition.notify_all()

    def _wait_for_session_operations(
        self,
        ordering_key: _SessionMemoryKey,
        *,
        wait_deadline: float,
    ) -> bool:
        with self._lifecycle_condition:
            while self._active_session_operations.get(ordering_key):
                remaining = wait_deadline - monotonic()
                if remaining <= 0:
                    return False
                self._lifecycle_condition.wait(remaining)
            return True

    def _request_maintenance_reap_locked(
        self,
        *,
        ordering_key: _SessionMemoryKey | None = None,
    ) -> None:
        if (
            not self._host_accepting
            or self._host_close_in_progress
            or self._maintenance_stopping
        ):
            return
        eager_pending = any(
            (
                pending.eager_close
                or pending.record.runtime_identity_key in self._session_admission_closed
            )
            and not pending.cache_terminal
            and (
                ordering_key is None
                or pending.record.runtime_identity_key == ordering_key
            )
            for pending in self._pending_reaps.values()
        )
        deferred_open = any(
            active.deferred
            and active.publication_done
            and (
                ordering_key is None
                or active.fallback_record.runtime_identity_key == ordering_key
            )
            for active in self._active_session_opens.values()
        )
        if not eager_pending and not deferred_open:
            return
        self._maintenance_requested = True
        self._maintenance_threads = {
            thread for thread in self._maintenance_threads if thread.is_alive()
        }
        thread = self._maintenance_thread
        if thread is None or not thread.is_alive():
            thread = Thread(
                target=self._maintenance_reaper_loop,
                name="assistant-memory-plugin-maintenance",
                daemon=True,
            )
            self._maintenance_thread = thread
            self._maintenance_threads.add(thread)
            try:
                thread.start()
            except Exception:
                self._maintenance_thread = None
                self._maintenance_threads.discard(thread)
                self._maintenance_requested = False
        self._lifecycle_condition.notify_all()

    def _maintenance_reaper_loop(self) -> None:
        try:
            while True:
                with self._lifecycle_condition:
                    if (
                        self._maintenance_stopping
                        or not self._host_accepting
                        or self._host_close_in_progress
                    ):
                        return
                    if not self._maintenance_requested:
                        return
                    self._maintenance_requested = False
                self._reap_pending_sessions(eager_only=True)
        finally:
            with self._lifecycle_condition:
                self._maintenance_thread = None
                if self._maintenance_requested:
                    self._request_maintenance_reap_locked()
                self._lifecycle_condition.notify_all()

    def _register_pending_reap_locked(
        self,
        *,
        record: MemoryPluginSessionRecord,
        reason: _SessionCloseReason,
        cache_terminal: bool,
        eager_close: bool = True,
    ) -> _PendingSessionReap:
        self._next_pending_reap_id += 1
        pending = _PendingSessionReap(
            reap_id=self._next_pending_reap_id,
            record=record,
            reason=reason,
            cache_terminal=cache_terminal,
            eager_close=eager_close,
        )
        self._pending_reaps[pending.reap_id] = pending
        return pending

    def _adopt_session_store_retirements_locked(self) -> None:
        terminal_requested = not self._host_accepting or self._host_close_in_progress
        for retired in self.session_store.claim_retired_records():
            self._register_pending_reap_locked(
                record=retired.record,
                reason=retired.reason,
                cache_terminal=terminal_requested,
                eager_close=retired.eager_close,
            )
            if terminal_requested:
                ordering_key = retired.record.runtime_identity_key
                self._session_admission_closed.add(ordering_key)
                self._session_close_reasons.setdefault(ordering_key, "shutdown")

    def _reap_pending_sessions(self, *, eager_only: bool) -> None:
        """Close displaced handles without allowing an unbounded reap backlog."""

        reap_deadline = (
            monotonic() + self.execution_policy.close_session_timeout_seconds
        )
        while True:
            with self._lifecycle_condition:
                self._adopt_session_store_retirements_locked()
                self._harvest_completed_active_opens_locked()
                self._harvest_completed_active_closes_locked()
                if not self._host_accepting or self._host_close_in_progress:
                    return
                candidate = next(
                    (
                        pending
                        for pending in self._pending_reaps.values()
                        if not pending.cache_terminal
                        and (not eager_only or pending.eager_close)
                        and pending.record.runtime_identity_key
                        not in self._closing_sessions
                        and pending.record.runtime_identity_key
                        not in self._opening_sessions
                    ),
                    None,
                )
                if candidate is None:
                    return
                ordering_key = candidate.record.runtime_identity_key
                self._session_admission_closed.add(ordering_key)
                self._session_close_reasons.setdefault(
                    ordering_key,
                    candidate.reason,
                )
                self._closing_sessions.add(ordering_key)

            if monotonic() >= reap_deadline:
                self._release_session_close_owner(ordering_key)
                return
            result = self._finish_session_close_owner(
                identity=_identity_from_runtime_key(ordering_key),
                ordering_key=ordering_key,
                reason=candidate.reason,
                wait_deadline=reap_deadline,
                opening_drained=True,
                cache_terminal=False,
                include_stored=False,
                drain_ingestion=True,
            )
            if result.status != "closed":
                return

    def _next_pending_reap_locked(
        self,
        ordering_key: _SessionMemoryKey,
    ) -> _PendingSessionReap | None:
        return next(
            (
                pending
                for pending in self._pending_reaps.values()
                if pending.record.runtime_identity_key == ordering_key
            ),
            None,
        )

    def _mark_pending_reaps_terminal_locked(
        self,
        ordering_key: _SessionMemoryKey,
    ) -> None:
        for pending in self._pending_reaps.values():
            if pending.record.runtime_identity_key == ordering_key:
                pending.cache_terminal = True

    def _open_record(
        self,
        *,
        identity: RequestIdentity,
        state: AgentState,
        descriptor: MemoryPluginDescriptor,
        cancellation: MemoryCancellationToken,
        call_deadline: float,
        invalidation: Event,
    ) -> MemoryPluginSessionRecord:
        plugin_namespace = (
            self.identity_namespace
            if descriptor.plugin_id == "mem0"
            else f"{self.identity_namespace}:{descriptor.plugin_id}"
        )
        plugin_identity = bind_memory_plugin_identity(
            identity,
            namespace=plugin_namespace,
        )
        memory_session_id = _memory_session_id(
            descriptor.plugin_id,
            plugin_identity,
        )
        request = MemorySessionOpenRequest(
            memory_session_id=memory_session_id,
            identity=plugin_identity,
            opened_at=self._now(),
            entry_profile=_entry_profile(state),
            deadline=self._deadline(self.execution_policy.open_session_timeout_seconds),
            cancellation=cancellation,
        )
        timeout_record = _degraded_open_record(
            identity=identity,
            descriptor=descriptor,
            plugin_identity=plugin_identity,
            memory_session_id=memory_session_id,
            issue_code=MEMORY_PLUGIN_TIMEOUT,
        )
        outcome, failure_code = self._invoke_owned_open(
            lambda: self._execute_open_call(
                lambda: self._active_plugin.open_session(request),
                identity=identity,
                descriptor=descriptor,
                plugin_identity=plugin_identity,
                memory_session_id=memory_session_id,
            ),
            invalidation=invalidation,
            fallback_record=timeout_record,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
        if outcome is not None and outcome.failure_code is not None:
            failure_code = outcome.failure_code
        raw = outcome.raw if outcome is not None else None
        try:
            _ensure_call_active(cancellation, call_deadline)
            result = self._validated_open_result(
                raw,
                descriptor=descriptor,
                owner_scope=plugin_identity.session_id,
                cancellation=cancellation,
                call_deadline=call_deadline,
            )
        except _MemoryCallStopped as stopped:
            failure_code = stopped.issue_code
            result = None
        except Exception:
            failure_code = MEMORY_PLUGIN_INVALID_RESULT
            result = None
        if failure_code is not None or result is None:
            code = failure_code or MEMORY_PLUGIN_INVALID_RESULT
            cleanup_record = outcome.cleanup_record if outcome is not None else None
            raise _OpenRecordRejected(code, cleanup_record)

        try:
            _ensure_call_active(cancellation, call_deadline)
            contribution = result.initial_contribution
            baseline = SessionMemorySnapshot(
                memories=list(contribution.items) if contribution is not None else [],
                plugin_id=descriptor.plugin_id,
                status=result.status,
                error_codes=_unique_codes(
                    [
                        *(issue.code for issue in result.issues),
                        *(
                            (issue.code for issue in contribution.issues)
                            if contribution is not None
                            else ()
                        ),
                    ]
                ),
            )
            _ensure_call_active(cancellation, call_deadline)
            record = MemoryPluginSessionRecord(
                plugin_id=descriptor.plugin_id,
                plugin_version=descriptor.plugin_version,
                runtime_identity_key=runtime_memory_identity_key(identity),
                identity=plugin_identity,
                memory_session_id=memory_session_id,
                session_handle=result.session_handle,
                baseline=baseline,
                status=result.status,
            )
            _ensure_call_active(cancellation, call_deadline)
            return record
        except _MemoryCallStopped as stopped:
            cleanup_record = outcome.cleanup_record if outcome is not None else None
            raise _OpenRecordRejected(stopped.issue_code, cleanup_record) from None

    def _validated_open_result(
        self,
        raw: object,
        *,
        descriptor: MemoryPluginDescriptor,
        owner_scope: str,
        cancellation: MemoryCancellationToken | None = None,
        call_deadline: float | None = None,
    ) -> MemorySessionOpenResult | None:
        _ensure_call_active(cancellation, call_deadline)
        if not _raw_result_is_safe(
            raw,
            cancellation=cancellation,
            call_deadline=call_deadline,
        ):
            return None
        result = _round_trip_model(
            raw,
            MemorySessionOpenResult,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
        if result is None:
            return None
        _ensure_call_active(cancellation, call_deadline)
        raw_open_values = object.__getattribute__(raw, "__dict__")
        raw_contribution = dict.__getitem__(
            raw_open_values,
            "initial_contribution",
        )
        if result.initial_contribution is not None and raw_contribution is not None:
            result = result.model_copy(
                update={
                    "initial_contribution": _restore_signed_media_refs(
                        result.initial_contribution,
                        raw_contribution,
                        cancellation=cancellation,
                        call_deadline=call_deadline,
                    )
                },
                deep=True,
            )
        if result.session_handle is not None and _forbidden_string(
            result.session_handle
        ):
            return None
        for issue in result.issues:
            _ensure_call_active(cancellation, call_deadline)
            if not _valid_issue(issue):
                return None
        if result.initial_contribution is not None:
            contribution = self._validated_contribution(
                result.initial_contribution,
                descriptor=descriptor,
                owner_scope=owner_scope,
                cancellation=cancellation,
                call_deadline=call_deadline,
            )
            if contribution is None:
                return None
            result = result.model_copy(
                update={"initial_contribution": contribution},
                deep=True,
            )
        _ensure_call_active(cancellation, call_deadline)
        return result

    def _validated_contribution(
        self,
        raw: object,
        *,
        descriptor: MemoryPluginDescriptor,
        owner_scope: str,
        cancellation: MemoryCancellationToken | None = None,
        call_deadline: float | None = None,
    ) -> MemoryContextContribution | None:
        _ensure_call_active(cancellation, call_deadline)
        if not _raw_result_is_safe(
            raw,
            cancellation=cancellation,
            call_deadline=call_deadline,
        ):
            return None
        _ensure_call_active(cancellation, call_deadline)
        contribution = _round_trip_model(
            raw,
            MemoryContextContribution,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
        if contribution is None:
            return None
        _ensure_call_active(cancellation, call_deadline)
        contribution = _restore_signed_media_refs(
            contribution,
            raw,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
        if len(contribution.items) > self.execution_policy.max_context_items:
            return None
        for issue in contribution.issues:
            _ensure_call_active(cancellation, call_deadline)
            if not _valid_issue(issue):
                return None

        total_chars = 0
        total_media_items = 0
        total_media_bytes = 0
        for item in contribution.items:
            _ensure_call_active(cancellation, call_deadline)
            if not _source_allowed(item, descriptor):
                return None
            if _forbidden_string(item.memory_id) or _forbidden_string(item.text):
                return None
            try:
                metadata_json = json.dumps(
                    item.metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except Exception:
                return None
            _ensure_call_active(cancellation, call_deadline)
            if _forbidden_metadata(
                item.metadata,
                cancellation=cancellation,
                call_deadline=call_deadline,
            ):
                return None
            total_chars += len(item.text) + len(metadata_json)
            if total_chars > self.execution_policy.max_context_chars:
                return None

            total_media_items += len(item.media_refs)
            if total_media_items > self.execution_policy.max_media_items_per_turn:
                return None
            for ref in item.media_refs:
                _ensure_call_active(cancellation, call_deadline)
                total_media_bytes += ref.size_bytes
                if total_media_bytes > self.execution_policy.max_media_bytes_per_turn:
                    return None
                try:
                    self.media_store.read(
                        ref,
                        owner_scope=owner_scope,
                        max_bytes=self.execution_policy.max_media_bytes_per_turn,
                        allowed_modalities={
                            modality
                            for modality in descriptor.capabilities.modalities
                            if modality != "text"
                        },
                    )
                except (MemoryMediaAccessError, TypeError, ValueError):
                    return None
                _ensure_call_active(cancellation, call_deadline)
        _ensure_call_active(cancellation, call_deadline)
        validated = contribution.model_copy(deep=True)
        _ensure_call_active(cancellation, call_deadline)
        return validated

    def _resolve_request_media(
        self,
        *,
        state: AgentState,
        record: MemoryPluginSessionRecord,
        descriptor: MemoryPluginDescriptor,
    ) -> tuple[list[ManagedMediaRef], list[MemoryPluginIssue]]:
        try:
            return self.media_store.resolve_request_refs(
                state.request,
                owner_scope=record.identity.session_id,
                allowed_modalities={
                    modality
                    for modality in descriptor.capabilities.modalities
                    if modality != "text"
                },
                max_items=self.execution_policy.max_media_items_per_turn,
                max_total_bytes=self.execution_policy.max_media_bytes_per_turn,
            )
        except Exception:
            return [], [
                MemoryPluginIssue(
                    code=MEMORY_PLUGIN_INVALID_RESULT,
                    message=MEMORY_PLUGIN_INVALID_RESULT,
                    recoverable=False,
                )
            ]

    def _invoke_owned_open(
        self,
        callback: Any,
        *,
        invalidation: Event,
        fallback_record: MemoryPluginSessionRecord,
        cancellation: MemoryCancellationToken,
        call_deadline: float,
    ) -> tuple[_OpenCallOutcome | None, str | None]:
        ordering_key = fallback_record.runtime_identity_key
        failure_code = _call_failure_code(cancellation, call_deadline)
        if failure_code is not None:
            return None, failure_code
        with self._lifecycle_condition:
            if (
                invalidation.is_set()
                or not self._host_accepting
                or ordering_key in self._closing_sessions
                or ordering_key in self._session_admission_closed
            ):
                return None, MEMORY_PLUGIN_UNAVAILABLE
            with self._executor_condition:
                if not self._executor_accepting:
                    return None, MEMORY_PLUGIN_UNAVAILABLE
                try:
                    future: Future[_OpenCallOutcome] = self._executor.submit(callback)
                except Exception:
                    return None, MEMORY_PLUGIN_INTERNAL_ERROR
                self._executor_futures.add(future)
                self._next_active_open_id += 1
                active = _ActiveSessionOpen(
                    open_id=self._next_active_open_id,
                    future=future,
                    invalidation=invalidation,
                    fallback_record=fallback_record,
                )
                self._active_session_opens[active.open_id] = active
            self._lifecycle_condition.notify_all()
        future.add_done_callback(self._forget_executor_future)
        future.add_done_callback(self._notify_active_open_completion)

        while True:
            failure_code = _call_failure_code(cancellation, call_deadline)
            if failure_code is not None:
                cancelled = future.cancel()
                with self._lifecycle_condition:
                    if cancelled:
                        if self._active_session_opens.get(active.open_id) is active:
                            self._active_session_opens.pop(active.open_id, None)
                    elif self._active_session_opens.get(active.open_id) is active:
                        active.deferred = True
                        self._deferred_open_barriers.add(ordering_key)
                        self._session_admission_closed.add(ordering_key)
                        self._request_maintenance_reap_locked(ordering_key=ordering_key)
                    self._lifecycle_condition.notify_all()
                return None, failure_code
            remaining = call_deadline - monotonic()
            try:
                outcome = future.result(timeout=min(remaining, 0.01))
            except FutureTimeout:
                continue
            except Exception:
                with self._lifecycle_condition:
                    if self._active_session_opens.get(active.open_id) is active:
                        self._active_session_opens.pop(active.open_id, None)
                    self._lifecycle_condition.notify_all()
                return None, MEMORY_PLUGIN_INTERNAL_ERROR
            failure_code = _call_failure_code(cancellation, call_deadline)
            if failure_code is not None:
                with self._lifecycle_condition:
                    if self._active_session_opens.get(active.open_id) is active:
                        active.deferred = True
                        self._deferred_open_barriers.add(ordering_key)
                        self._session_admission_closed.add(ordering_key)
                        self._request_maintenance_reap_locked(ordering_key=ordering_key)
                    self._lifecycle_condition.notify_all()
                return None, failure_code
            with self._lifecycle_condition:
                if self._active_session_opens.get(active.open_id) is active:
                    self._active_session_opens.pop(active.open_id, None)
                self._lifecycle_condition.notify_all()
            if type(outcome) is not _OpenCallOutcome:
                return None, MEMORY_PLUGIN_INTERNAL_ERROR
            return outcome, None

    def _invoke_owned_close(
        self,
        callback: Any,
        *,
        record: MemoryPluginSessionRecord,
        reason: _SessionCloseReason,
        source: _SessionCloseSource,
        pending_reap_id: int | None,
        cache_terminal: bool,
        cancellation: MemoryCancellationToken,
        call_deadline: float,
    ) -> tuple[object | None, str | None, bool]:
        ordering_key = record.runtime_identity_key
        failure_code = _call_failure_code(cancellation, call_deadline)
        if failure_code is not None:
            return None, failure_code, False
        with self._lifecycle_condition:
            if ordering_key in self._active_session_closes:
                return None, MEMORY_PLUGIN_TIMEOUT, True
            with self._executor_condition:
                if not self._executor_accepting:
                    return None, MEMORY_PLUGIN_UNAVAILABLE, False
                try:
                    future: Future[Any] = self._executor.submit(callback)
                except Exception:
                    return None, MEMORY_PLUGIN_INTERNAL_ERROR, False
                self._executor_futures.add(future)
                active = _ActiveSessionClose(
                    future=future,
                    record=record,
                    reason=reason,
                    source=source,
                    pending_reap_id=pending_reap_id,
                    cache_terminal=cache_terminal,
                )
                self._active_session_closes[ordering_key] = active
            self._lifecycle_condition.notify_all()
        future.add_done_callback(self._forget_executor_future)
        future.add_done_callback(self._notify_active_close_completion)

        while True:
            failure_code = _call_failure_code(cancellation, call_deadline)
            if failure_code is not None:
                cancelled = future.cancel()
                if cancelled:
                    with self._lifecycle_condition:
                        if self._active_session_closes.get(ordering_key) is active:
                            self._active_session_closes.pop(ordering_key, None)
                        self._lifecycle_condition.notify_all()
                else:
                    with self._lifecycle_condition:
                        if self._active_session_closes.get(ordering_key) is active:
                            active.deferred = True
                            self._request_maintenance_reap_locked(
                                ordering_key=ordering_key
                            )
                        self._lifecycle_condition.notify_all()
                return None, failure_code, not cancelled
            remaining = call_deadline - monotonic()
            try:
                result = future.result(timeout=min(remaining, 0.01))
            except FutureTimeout:
                continue
            except Exception:
                with self._lifecycle_condition:
                    if self._active_session_closes.get(ordering_key) is active:
                        self._active_session_closes.pop(ordering_key, None)
                    self._lifecycle_condition.notify_all()
                return None, MEMORY_PLUGIN_INTERNAL_ERROR, False
            failure_code = _call_failure_code(cancellation, call_deadline)
            if failure_code is not None:
                with self._lifecycle_condition:
                    if self._active_session_closes.get(ordering_key) is active:
                        active.deferred = True
                        self._request_maintenance_reap_locked(ordering_key=ordering_key)
                    self._lifecycle_condition.notify_all()
                return None, failure_code, True
            with self._lifecycle_condition:
                if self._active_session_closes.get(ordering_key) is active:
                    self._active_session_closes.pop(ordering_key, None)
                self._lifecycle_condition.notify_all()
            return result, None, False

    def _wait_for_active_session_opens(
        self,
        ordering_key: _SessionMemoryKey,
        *,
        wait_deadline: float,
    ) -> bool:
        with self._lifecycle_condition:
            while True:
                self._adopt_session_store_retirements_locked()
                self._harvest_completed_active_opens_locked(ordering_key)
                active = any(
                    candidate.fallback_record.runtime_identity_key == ordering_key
                    for candidate in self._active_session_opens.values()
                )
                if not active:
                    return True
                remaining = wait_deadline - monotonic()
                if remaining <= 0:
                    return False
                self._lifecycle_condition.wait(remaining)

    def _harvest_completed_active_opens_locked(
        self,
        ordering_key: _SessionMemoryKey | None = None,
    ) -> None:
        candidates = tuple(self._active_session_opens.items())
        for open_id, active in candidates:
            session_key = active.fallback_record.runtime_identity_key
            if ordering_key is not None and session_key != ordering_key:
                continue
            if (
                not active.deferred
                or not active.publication_done
                or not active.future.done()
            ):
                continue
            if self._active_session_opens.get(open_id) is not active:
                continue
            self._active_session_opens.pop(open_id, None)
            try:
                outcome = active.future.result()
            except Exception:
                outcome = None
            cleanup_record = (
                outcome.cleanup_record if type(outcome) is _OpenCallOutcome else None
            )
            self._deferred_open_barriers.discard(session_key)
            if cleanup_record is not None:
                terminal_requested = (
                    not self._host_accepting
                    or self._host_close_in_progress
                    or session_key in self._session_close_reasons
                )
                reason = self._session_close_reasons.get(
                    session_key,
                    "shutdown" if not self._host_accepting else "reset",
                )
                self._register_pending_reap_locked(
                    record=cleanup_record,
                    reason=reason,
                    cache_terminal=terminal_requested,
                    eager_close=True,
                )
                self._session_admission_closed.add(session_key)
                self._session_close_reasons.setdefault(session_key, reason)
            elif (
                session_key not in self._closing_sessions
                and session_key not in self._clear_admission_barriers
                and self._next_pending_reap_locked(session_key) is None
                and session_key not in self._session_close_reasons
            ):
                self._session_admission_closed.discard(session_key)
            self._lifecycle_condition.notify_all()

    def _harvest_completed_active_closes_locked(
        self,
        ordering_key: _SessionMemoryKey | None = None,
    ) -> None:
        candidates = tuple(self._active_session_closes.items())
        for session_key, active in candidates:
            if ordering_key is not None and session_key != ordering_key:
                continue
            if not active.deferred or not active.future.done():
                continue
            if self._active_session_closes.get(session_key) is not active:
                continue
            self._active_session_closes.pop(session_key, None)
            try:
                raw = active.future.result()
            except Exception:
                result = _close_failure(MEMORY_PLUGIN_INTERNAL_ERROR)
            else:
                result = (
                    raw
                    if type(raw) is MemorySessionCloseResult
                    else _close_failure(MEMORY_PLUGIN_INTERNAL_ERROR)
                )

            if result.status == "closed":
                if active.source == "pending" and active.pending_reap_id is not None:
                    pending = self._pending_reaps.get(active.pending_reap_id)
                    if pending is not None and pending.record == active.record:
                        self._pending_reaps.pop(active.pending_reap_id, None)
                elif active.source == "stored":
                    identity = _identity_from_runtime_key(session_key)
                    stored = self.session_store.get(identity)
                    if stored == active.record:
                        self.session_store.pop(identity)

                pending_handles_remain = (
                    self._next_pending_reap_locked(session_key) is not None
                )
                stored_handle_remains = (
                    self.session_store.get(_identity_from_runtime_key(session_key))
                    is not None
                )
                nonterminal_cleanup_finished = (
                    active.source == "pending"
                    and not active.cache_terminal
                    and not pending_handles_remain
                )
                if nonterminal_cleanup_finished:
                    clear_barrier_waiting = (
                        session_key in self._clear_admission_barriers
                        and bool(self._opening_sessions.get(session_key))
                    )
                    if not clear_barrier_waiting:
                        self._clear_admission_barriers.discard(session_key)
                        self._session_admission_closed.discard(session_key)
                        self._session_close_reasons.pop(session_key, None)
                elif not pending_handles_remain and not stored_handle_remains:
                    if active.cache_terminal:
                        self._closed_session_results[session_key] = result.model_copy(
                            deep=True
                        )
                    if active.source == "stored" or active.cache_terminal:
                        self._clear_frozen_contexts(
                            lambda run_key: run_key[:3] == session_key
                        )
                    clear_barrier_waiting = (
                        session_key in self._clear_admission_barriers
                        and bool(self._opening_sessions.get(session_key))
                    )
                    if not clear_barrier_waiting:
                        self._clear_admission_barriers.discard(session_key)
                        self._session_admission_closed.discard(session_key)
                        self._session_close_reasons.pop(session_key, None)
            self._closing_sessions.discard(session_key)
            self._lifecycle_condition.notify_all()

    def _invoke(
        self,
        callback: Any,
        *,
        timeout_seconds: float,
        cancellation: MemoryCancellationToken,
        call_deadline: float | None = None,
    ) -> tuple[object | None, str | None]:
        with self._executor_condition:
            if not self._executor_accepting:
                return None, MEMORY_PLUGIN_UNAVAILABLE
            try:
                future: Future[Any] = self._executor.submit(callback)
            except Exception:
                return None, MEMORY_PLUGIN_INTERNAL_ERROR
            self._executor_futures.add(future)
        future.add_done_callback(self._forget_executor_future)
        resolved_deadline = (
            call_deadline
            if call_deadline is not None
            else monotonic() + timeout_seconds
        )
        while True:
            failure_code = _call_failure_code(cancellation, resolved_deadline)
            if failure_code is not None:
                future.cancel()
                return None, failure_code
            remaining = resolved_deadline - monotonic()
            try:
                result = future.result(timeout=min(remaining, 0.01))
            except FutureTimeout:
                continue
            except Exception:
                return None, MEMORY_PLUGIN_INTERNAL_ERROR
            failure_code = _call_failure_code(cancellation, resolved_deadline)
            if failure_code is not None:
                return None, failure_code
            return result, None

    def _invoke_session_operation(
        self,
        callback: Any,
        *,
        record: MemoryPluginSessionRecord,
        timeout_seconds: float,
        cancellation: MemoryCancellationToken,
        call_deadline: float | None = None,
    ) -> tuple[object | None, str | None]:
        """Dispatch Plugin work while retaining handle-scoped ownership."""

        ordering_key = record.runtime_identity_key
        with self._lifecycle_condition:
            operation_id = self._acquire_session_operation_locked(record)
            if operation_id is None:
                return None, MEMORY_PLUGIN_UNAVAILABLE
            with self._executor_condition:
                if not self._executor_accepting:
                    self._release_session_operation_locked(
                        ordering_key,
                        operation_id,
                    )
                    return None, MEMORY_PLUGIN_UNAVAILABLE
                try:
                    future: Future[Any] = self._executor.submit(callback)
                except Exception:
                    self._release_session_operation_locked(
                        ordering_key,
                        operation_id,
                    )
                    return None, MEMORY_PLUGIN_INTERNAL_ERROR
                self._executor_futures.add(future)
        future.add_done_callback(self._forget_executor_future)
        future.add_done_callback(
            lambda completed: self._release_session_operation(
                ordering_key,
                operation_id,
            )
        )
        resolved_deadline = (
            call_deadline
            if call_deadline is not None
            else monotonic() + timeout_seconds
        )
        while True:
            failure_code = _call_failure_code(cancellation, resolved_deadline)
            if failure_code is not None:
                future.cancel()
                return None, failure_code
            remaining = resolved_deadline - monotonic()
            try:
                result = future.result(timeout=min(remaining, 0.01))
            except FutureTimeout:
                continue
            except Exception:
                return None, MEMORY_PLUGIN_INTERNAL_ERROR
            failure_code = _call_failure_code(cancellation, resolved_deadline)
            if failure_code is not None:
                return None, failure_code
            return result, None

    def _invoke_ingestion(
        self,
        callback: Any,
        *,
        timeout_seconds: float,
        cancellation: MemoryCancellationToken,
        call_deadline: float | None = None,
    ) -> tuple[object | None, str | None]:
        """Keep accepted ingestion owned until its Plugin call truly stops.

        A Python thread cannot be force-killed. If the caller deadline expires
        after work has started, the queue callback deliberately remains active
        and holds the per-identity ordering slot until the future terminates.
        """

        with self._executor_condition:
            if not self._executor_accepting:
                return None, MEMORY_PLUGIN_UNAVAILABLE
            try:
                future: Future[Any] = self._executor.submit(callback)
            except Exception:
                return None, MEMORY_PLUGIN_INTERNAL_ERROR
            self._executor_futures.add(future)
        future.add_done_callback(self._forget_executor_future)
        resolved_deadline = (
            call_deadline
            if call_deadline is not None
            else monotonic() + timeout_seconds
        )
        while True:
            failure_code = _call_failure_code(cancellation, resolved_deadline)
            if failure_code is not None:
                if not future.cancel():
                    try:
                        future.result()
                    except Exception:
                        pass
                return None, failure_code
            remaining = resolved_deadline - monotonic()
            try:
                result = future.result(timeout=min(remaining, 0.01))
            except FutureTimeout:
                continue
            except Exception:
                return None, MEMORY_PLUGIN_INTERNAL_ERROR
            failure_code = _call_failure_code(cancellation, resolved_deadline)
            if failure_code is not None:
                return None, failure_code
            return result, None

    def _forget_executor_future(self, future: Future[Any]) -> None:
        with self._executor_condition:
            self._executor_futures.discard(future)
            self._executor_condition.notify_all()

    def _notify_active_open_completion(self, future: Future[Any]) -> None:
        with self._lifecycle_condition:
            for active in self._active_session_opens.values():
                if active.future is future and active.deferred:
                    self._request_maintenance_reap_locked(
                        ordering_key=active.fallback_record.runtime_identity_key
                    )
                    break
            self._lifecycle_condition.notify_all()

    def _notify_active_close_completion(self, future: Future[Any]) -> None:
        with self._lifecycle_condition:
            if any(
                active.future is future and active.deferred
                for active in self._active_session_closes.values()
            ):
                self._request_maintenance_reap_locked()
            self._lifecycle_condition.notify_all()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Memory Plugin Host clock must be timezone-aware")
        return value

    def _deadline(self, timeout_seconds: float) -> datetime:
        return self._now() + timedelta(seconds=timeout_seconds)


def _round_trip_model(
    raw: object,
    model_type: type[_ModelT],
    *,
    cancellation: MemoryCancellationToken | None = None,
    call_deadline: float | None = None,
) -> _ModelT | None:
    if type(raw) is not model_type:
        return None
    try:
        _ensure_call_active(cancellation, call_deadline)
        if not _has_exact_model_shape(
            raw,
            model_type,
            cancellation=cancellation,
            call_deadline=call_deadline,
        ):
            return None
        _ensure_call_active(cancellation, call_deadline)
        encoded = raw.model_dump_json()
        _ensure_call_active(cancellation, call_deadline)
        validated = model_type.model_validate_json(encoded)
        _ensure_call_active(cancellation, call_deadline)
        return validated
    except _MemoryCallStopped:
        raise
    except Exception:
        return None


def _has_exact_model_shape(
    raw: BaseModel,
    model_type: type[BaseModel],
    *,
    cancellation: MemoryCancellationToken | None = None,
    call_deadline: float | None = None,
) -> bool:
    _ensure_call_active(cancellation, call_deadline)
    if type(raw) is not model_type:
        return False
    values = object.__getattribute__(raw, "__dict__")
    if type(values) is not dict:
        return False
    keys = list(values)
    if any(type(key) is not str for key in keys):
        return False
    if set(keys) != set(model_type.model_fields):
        return False
    if model_type is MemoryContextContribution:
        items = dict.__getitem__(values, "items")
        issues = dict.__getitem__(values, "issues")
        if type(items) is not list or type(issues) is not list:
            return False
        for item in items:
            _ensure_call_active(cancellation, call_deadline)
            if not _has_exact_model_shape(
                item,
                MemoryContextItem,
                cancellation=cancellation,
                call_deadline=call_deadline,
            ):
                return False
        for issue in issues:
            _ensure_call_active(cancellation, call_deadline)
            if not _has_exact_model_shape(
                issue,
                MemoryPluginIssue,
                cancellation=cancellation,
                call_deadline=call_deadline,
            ):
                return False
        return True
    if model_type is MemoryContextItem:
        media_refs = dict.__getitem__(values, "media_refs")
        if type(media_refs) is not list:
            return False
        for ref in media_refs:
            _ensure_call_active(cancellation, call_deadline)
            if not _has_exact_model_shape(
                ref,
                ManagedMediaRef,
                cancellation=cancellation,
                call_deadline=call_deadline,
            ):
                return False
        return True
    if model_type is MemorySessionOpenResult:
        issues = dict.__getitem__(values, "issues")
        contribution = dict.__getitem__(values, "initial_contribution")
        if type(issues) is not list:
            return False
        for issue in issues:
            _ensure_call_active(cancellation, call_deadline)
            if not _has_exact_model_shape(
                issue,
                MemoryPluginIssue,
                cancellation=cancellation,
                call_deadline=call_deadline,
            ):
                return False
        return contribution is None or _has_exact_model_shape(
            contribution,
            MemoryContextContribution,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
    if model_type is MemoryTurnIngestionResult:
        changes = dict.__getitem__(values, "changes")
        issues = dict.__getitem__(values, "issues")
        if type(changes) is not list or type(issues) is not list:
            return False
        return all(
            _has_exact_model_shape(
                change,
                MemoryChange,
                cancellation=cancellation,
                call_deadline=call_deadline,
            )
            for change in changes
        ) and all(
            _has_exact_model_shape(
                issue,
                MemoryPluginIssue,
                cancellation=cancellation,
                call_deadline=call_deadline,
            )
            for issue in issues
        )
    if model_type is MemorySessionCloseResult:
        issues = dict.__getitem__(values, "issues")
        if type(issues) is not list:
            return False
        return all(
            _has_exact_model_shape(
                issue,
                MemoryPluginIssue,
                cancellation=cancellation,
                call_deadline=call_deadline,
            )
            for issue in issues
        )
    return True


def _restore_signed_media_refs(
    validated: MemoryContextContribution,
    raw: MemoryContextContribution,
    *,
    cancellation: MemoryCancellationToken | None = None,
    call_deadline: float | None = None,
) -> MemoryContextContribution:
    """Keep exact Host-issued timestamps while retaining full JSON validation."""

    raw_values = object.__getattribute__(raw, "__dict__")
    raw_items = dict.__getitem__(raw_values, "items")
    restored_items: list[MemoryContextItem] = []
    for validated_item, raw_item in zip(validated.items, raw_items, strict=True):
        _ensure_call_active(cancellation, call_deadline)
        item_values = object.__getattribute__(raw_item, "__dict__")
        raw_refs = dict.__getitem__(item_values, "media_refs")
        restored_items.append(
            validated_item.model_copy(
                update={"media_refs": [ref.model_copy(deep=True) for ref in raw_refs]},
                deep=True,
            )
        )
    _ensure_call_active(cancellation, call_deadline)
    restored = validated.model_copy(update={"items": restored_items}, deep=True)
    _ensure_call_active(cancellation, call_deadline)
    return restored


def _valid_issue(issue: MemoryPluginIssue) -> bool:
    return bool(
        _SAFE_CODE_RE.fullmatch(issue.code)
        and not _UNSAFE_ISSUE_CODE_RE.search(issue.code)
        and not _forbidden_string(issue.code)
        and not _forbidden_string(issue.message)
    )


def _source_allowed(
    item: MemoryContextItem,
    descriptor: MemoryPluginDescriptor,
) -> bool:
    modalities = descriptor.capabilities.modalities
    if item.source in {"long_term", "episodic", "semantic"}:
        return "text" in modalities
    if item.source == "visual":
        return "image" in modalities or "video" in modalities
    return item.source in modalities


def _forbidden_metadata(
    value: Any,
    *,
    cancellation: MemoryCancellationToken | None = None,
    call_deadline: float | None = None,
) -> bool:
    stack: list[tuple[Any, str | None, int]] = [(value, None, 0)]
    visited = 0
    seen: set[int] = set()
    while stack:
        _ensure_call_active(cancellation, call_deadline)
        item, key, depth = stack.pop()
        visited += 1
        if visited > _MAX_VALIDATION_NODES or depth > _MAX_VALIDATION_DEPTH:
            return True
        if key is not None and _forbidden_metadata_key(key):
            return True
        if item is None or type(item) in {bool, int}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                return True
            continue
        if type(item) is str:
            if _forbidden_string(item):
                return True
            continue
        if type(item) not in {dict, list}:
            return True
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if type(item) is list:
            stack.extend((child, None, depth + 1) for child in reversed(item))
            continue
        for item_key, item_value in reversed(tuple(item.items())):
            if type(item_key) is not str:
                return True
            stack.append((item_value, item_key, depth + 1))
    return False


def _forbidden_metadata_key(key: str) -> bool:
    if _forbidden_string(key):
        return True
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip())
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", camel_split).strip("_").lower()
    compact = normalized.replace("_", "")
    return bool(
        normalized in _BLOCKED_METADATA_KEYS
        or compact in _CREDENTIAL_METADATA_COMPACT_KEYS
        or compact.endswith(tuple(_CREDENTIAL_METADATA_COMPACT_KEYS))
        or normalized.endswith(
            tuple(f"_{blocked}" for blocked in _CREDENTIAL_METADATA_KEYS)
        )
        or normalized.endswith(("_base64", "_bytes", "_blob", "_data_uri"))
    )


def _forbidden_string(value: str) -> bool:
    if not isinstance(value, str):
        return True
    return bool(
        "file://" in value.lower()
        or _contains_absolute_path(value)
        or _BASE64_RE.search(value)
        or _SECRET_ASSIGNMENT_RE.search(value)
        or _BEARER_RE.search(value)
        or _KEY_PREFIX_RE.search(value)
    )


def _contains_absolute_path(value: str) -> bool:
    if _WINDOWS_DRIVE_PATH_RE.search(value) or _UNC_PATH_RE.search(value):
        return True
    for match in _POSIX_PATH_CANDIDATE_RE.finditer(value):
        start = match.start()
        if start == 0:
            return True
        previous = value[start - 1]
        if previous in ":/\\" or previous.isalnum() or previous in "._-":
            continue
        return True
    return False


def _call_failure_code(
    cancellation: MemoryCancellationToken | None,
    call_deadline: float | None,
) -> str | None:
    if cancellation is not None:
        try:
            if cancellation.is_cancelled():
                return MEMORY_PLUGIN_UNAVAILABLE
        except Exception:
            return MEMORY_PLUGIN_INTERNAL_ERROR
    if call_deadline is not None and monotonic() >= call_deadline:
        return MEMORY_PLUGIN_TIMEOUT
    return None


def _ensure_call_active(
    cancellation: MemoryCancellationToken | None,
    call_deadline: float | None,
) -> None:
    issue_code = _call_failure_code(cancellation, call_deadline)
    if issue_code is not None:
        raise _MemoryCallStopped(issue_code)


def _raw_result_is_safe(
    value: Any,
    *,
    cancellation: MemoryCancellationToken | None = None,
    call_deadline: float | None = None,
) -> bool:
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    visited = 0
    seen: set[int] = set()
    while stack:
        _ensure_call_active(cancellation, call_deadline)
        item, depth, in_metadata = stack.pop()
        visited += 1
        if visited > _MAX_VALIDATION_NODES or depth > _MAX_VALIDATION_DEPTH:
            return False
        if item is None or type(item) in {bool, int, str}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                return False
            continue
        if isinstance(item, datetime):
            if in_metadata:
                return False
            continue
        if isinstance(item, BaseModel):
            if in_metadata:
                return False
            try:
                values = object.__getattribute__(item, "__dict__")
            except Exception:
                return False
            if type(values) is not dict:
                return False
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            for field_name, field_value in reversed(tuple(values.items())):
                if type(field_name) is not str:
                    return False
                stack.append(
                    (
                        field_value,
                        depth + 1,
                        type(item) is MemoryContextItem and field_name == "metadata",
                    )
                )
            continue
        if type(item) not in {dict, list}:
            return False
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if type(item) is list:
            stack.extend((child, depth + 1, in_metadata) for child in reversed(item))
            continue
        for item_key, item_value in reversed(tuple(item.items())):
            if type(item_key) is not str:
                return False
            stack.append((item_value, depth + 1, in_metadata))
    return True


def _fallback_snapshot(
    record: MemoryPluginSessionRecord,
    issue_code: str,
    *,
    extra_codes: list[str] | None = None,
) -> SessionMemorySnapshot:
    return SessionMemorySnapshot(
        memories=record.baseline.memories,
        plugin_id=record.plugin_id,
        status="degraded",
        error_codes=_unique_codes(
            [*record.baseline.error_codes, *(extra_codes or []), issue_code]
        ),
    )


def _degraded_open_record(
    *,
    identity: RequestIdentity,
    descriptor: MemoryPluginDescriptor,
    plugin_identity: MemoryIdentity,
    memory_session_id: str,
    issue_code: str,
    session_handle: str | None = None,
) -> MemoryPluginSessionRecord:
    return MemoryPluginSessionRecord(
        plugin_id=descriptor.plugin_id,
        plugin_version=descriptor.plugin_version,
        runtime_identity_key=runtime_memory_identity_key(identity),
        identity=plugin_identity,
        memory_session_id=memory_session_id,
        session_handle=session_handle,
        baseline=SessionMemorySnapshot(
            plugin_id=descriptor.plugin_id,
            status="degraded",
            error_codes=[issue_code],
        ),
        status="degraded",
    )


def _guard_open_record_for_publish(
    record: MemoryPluginSessionRecord,
    *,
    cancellation: MemoryCancellationToken,
    call_deadline: float,
    invalidation: Event,
) -> MemoryPluginSessionRecord:
    if invalidation.is_set():
        raise MemoryPluginSessionLoadInvalidated(record)
    issue_code = _call_failure_code(cancellation, call_deadline)
    if issue_code is None:
        return record
    raise _OpenRecordRejected(issue_code, record)


def _merge_items(
    baseline: list[MemoryContextItem],
    current: list[MemoryContextItem],
    *,
    cancellation: MemoryCancellationToken | None = None,
    call_deadline: float | None = None,
) -> list[MemoryContextItem]:
    merged: dict[str, MemoryContextItem] = {}
    for items in (baseline, current):
        for item in items:
            _ensure_call_active(cancellation, call_deadline)
            merged[item.memory_id] = item.model_copy(deep=True)
    _ensure_call_active(cancellation, call_deadline)
    return list(merged.values())


def _unique_codes(codes: Any) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for code in codes:
        if code and code not in seen:
            seen.add(code)
            unique.append(code)
    return unique


def _memory_session_id(plugin_id: str, identity: MemoryIdentity) -> str:
    payload = "\x1f".join(
        (plugin_id, identity.user_id, identity.agent_id, identity.session_id)
    )
    return "mem_" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def _entry_profile(state: AgentState) -> str:
    value = state.request.metadata.get("entry_profile")
    if isinstance(value, str) and 0 < len(value) <= 128:
        return value
    return "runtime"


def _identity_from_state(state: AgentState) -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=state.user_id,
        agent_id=state.agent_id,
        session_id=state.session_id,
    )


def _identity_from_runtime_key(
    ordering_key: _SessionMemoryKey,
) -> RequestIdentity:
    user_id, agent_id, session_id = ordering_key
    return RequestIdentity.for_user(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )


def _run_memory_key(state: AgentState) -> _RunMemoryKey:
    return state.user_id, state.agent_id, state.session_id, state.run_id


def _structured_ingestion_skip_reason(state: AgentState) -> str | None:
    if state.tool_results and all(
        result.tool_name == VISUAL_REMINDER_MANAGE_TOOL_NAME
        for result in state.tool_results
    ):
        return "connection_scoped_visual_reminder"
    return None


def _valid_memory_message_text(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 20_000


def _safe_bounded_string(value: object, *, max_chars: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= max_chars
        and not _forbidden_string(value)
    )


def _conversation_turn_index(state: AgentState) -> str:
    value = state.request.metadata.get("conversation_turn_index")
    if type(value) is int and value > 0:
        return str(value)
    if type(value) is str and 0 < len(value) <= 128:
        return value
    return "1"


def _close_failure(
    issue_code: str,
    *,
    partial: bool = False,
) -> MemorySessionCloseResult:
    return MemorySessionCloseResult(
        status="partial" if partial else "failed",
        issues=[
            MemoryPluginIssue(
                code=issue_code,
                message=issue_code,
                recoverable=partial or issue_code == MEMORY_PLUGIN_TIMEOUT,
            )
        ],
    )


def _merge_close_issue(
    result: MemorySessionCloseResult,
    issue_code: str,
) -> MemorySessionCloseResult:
    issues = list(result.issues)
    if issue_code not in {issue.code for issue in issues}:
        issues.append(
            MemoryPluginIssue(
                code=issue_code,
                message=issue_code,
                recoverable=True,
            )
        )
    status: Literal["closed", "partial", "failed"] = result.status
    if status == "closed":
        status = "partial"
    return MemorySessionCloseResult(status=status, issues=issues)


def _remaining_timeout(started: float, timeout: float) -> float:
    return max(0.0, timeout - (monotonic() - started))


def _sealed_registry_runtime(
    registry: MemoryPluginRegistry,
) -> tuple[object | None, MemoryPluginDescriptor | None]:
    try:
        active_plugin = registry.active_plugin
        report = registry.assembly_report
        active_records = [record for record in report.records if record.active]
        if len(active_records) != 1:
            return None, None
        active_record = active_records[0]
        if active_record.descriptor.plugin_id != report.active_slot:
            return None, None
        descriptor = active_record.descriptor.model_copy(deep=True)
    except Exception:
        return None, None
    return active_plugin, descriptor
