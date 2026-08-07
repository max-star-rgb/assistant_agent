"""Governed lifecycle Host for the active Memory Plugin."""

from __future__ import annotations

import hashlib
import json
import math
import re
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Condition
from time import monotonic, perf_counter
from typing import Any, TypeVar

from pydantic import BaseModel

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.memory.observability import record_session_recall
from assistant_agent.memory.plugins.contracts import (
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
    MemoryIdentity,
    MemoryMessage,
    MemoryPluginDescriptor,
    MemoryPluginExecutionPolicy,
    MemoryPluginIssue,
    MemorySessionOpenRequest,
    MemorySessionOpenResult,
)
from assistant_agent.memory.plugins.media import (
    ManagedMemoryMediaStore,
    MemoryMediaAccessError,
)
from assistant_agent.memory.plugins.registry import MemoryPluginRegistry
from assistant_agent.memory.plugins.session_store import (
    MemoryPluginSessionRecord,
    MemoryPluginSessionStore,
    runtime_memory_identity_key,
)
from assistant_agent.observability.trace_store import TraceStore
from assistant_agent.runtime.cancellation import is_cancelled
from assistant_agent.runtime.state import AgentState


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_RunMemoryKey = tuple[str, str, str, str]
_SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
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


class MemoryPluginHost:
    """Validate and freeze recall from the Registry's active Plugin."""

    def __init__(
        self,
        *,
        registry: MemoryPluginRegistry,
        session_store: MemoryPluginSessionStore | None = None,
        media_store: ManagedMemoryMediaStore | None = None,
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

        started = perf_counter()
        descriptor = self._active_descriptor
        if descriptor is None or self._active_plugin is None:
            return SessionMemorySnapshot(
                status="degraded",
                error_codes=[MEMORY_PLUGIN_INTERNAL_ERROR],
            )
        cached = self.session_store.get(identity)
        plugin_changed = cached is not None and (
            cached.plugin_id != descriptor.plugin_id
            or cached.plugin_version != descriptor.plugin_version
        )
        cancellation = _DelegatingCancellationToken(None)
        call_deadline = monotonic() + self.execution_policy.open_session_timeout_seconds
        resolution = self.session_store.resolve(
            identity,
            reset=reset or plugin_changed,
            loader=lambda: self._open_record(
                identity=identity,
                state=state,
                descriptor=descriptor,
                cancellation=cancellation,
                call_deadline=call_deadline,
            ),
            before_publish=lambda record: _guard_open_record_for_publish(
                record,
                cancellation=cancellation,
                call_deadline=call_deadline,
            ),
        )
        snapshot = resolution.record.baseline.model_copy(deep=True)
        if resolution.status == "loaded":
            record_session_recall(
                trace_store=trace_store,
                state=state,
                status=snapshot.status,
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
                memory_count=len(snapshot.memories),
                error_codes=list(snapshot.error_codes),
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

        del trace_store  # Task 5 owns the expanded Plugin observability projection.
        run_key = _run_memory_key(state)
        with self._run_condition:
            while True:
                frozen = self._frozen_run_contexts.get(run_key)
                if frozen is not None:
                    return self._publish_snapshot_to_state(state, frozen)
                if run_key not in self._preparing_runs:
                    self._preparing_runs.add(run_key)
                    break
                self._run_condition.wait()

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
            self._frozen_run_contexts[run_key] = frozen
            self._preparing_runs.discard(run_key)
            self._run_condition.notify_all()
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
        raw, failure_code = self._invoke(
            lambda: self._active_plugin.prepare_context(request),
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

    def _open_record(
        self,
        *,
        identity: RequestIdentity,
        state: AgentState,
        descriptor: MemoryPluginDescriptor,
        cancellation: MemoryCancellationToken,
        call_deadline: float,
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
        raw, failure_code = self._invoke(
            lambda: self._active_plugin.open_session(request),
            timeout_seconds=self.execution_policy.open_session_timeout_seconds,
            cancellation=cancellation,
            call_deadline=call_deadline,
        )
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
            return _degraded_open_record(
                identity=identity,
                descriptor=descriptor,
                plugin_identity=plugin_identity,
                memory_session_id=memory_session_id,
                issue_code=code,
            )

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
            return _degraded_open_record(
                identity=identity,
                descriptor=descriptor,
                plugin_identity=plugin_identity,
                memory_session_id=memory_session_id,
                issue_code=stopped.issue_code,
            )

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

    def _invoke(
        self,
        callback: Any,
        *,
        timeout_seconds: float,
        cancellation: MemoryCancellationToken,
        call_deadline: float | None = None,
    ) -> tuple[object | None, str | None]:
        future: Future[Any] = self._executor.submit(callback)
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
    return bool(_SAFE_CODE_RE.fullmatch(issue.code)) and not _forbidden_string(
        issue.message
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
) -> MemoryPluginSessionRecord:
    return MemoryPluginSessionRecord(
        plugin_id=descriptor.plugin_id,
        plugin_version=descriptor.plugin_version,
        runtime_identity_key=runtime_memory_identity_key(identity),
        identity=plugin_identity,
        memory_session_id=memory_session_id,
        session_handle=None,
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
) -> MemoryPluginSessionRecord:
    issue_code = _call_failure_code(cancellation, call_deadline)
    if issue_code is None:
        return record
    return MemoryPluginSessionRecord(
        plugin_id=record.plugin_id,
        plugin_version=record.plugin_version,
        runtime_identity_key=record.runtime_identity_key,
        identity=record.identity,
        memory_session_id=record.memory_session_id,
        session_handle=None,
        baseline=SessionMemorySnapshot(
            plugin_id=record.plugin_id,
            status="degraded",
            error_codes=[issue_code],
        ),
        status="degraded",
    )


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


def _run_memory_key(state: AgentState) -> _RunMemoryKey:
    return state.user_id, state.agent_id, state.session_id, state.run_id


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
