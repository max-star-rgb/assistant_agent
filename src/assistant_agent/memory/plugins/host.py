"""Governed lifecycle Host for the active Memory Plugin."""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
_SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[\s('\"=])(?:/(?:[^/\s]+/)*[^/\s]+|[A-Za-z]:[\\/][^\s]+)"
)
_BASE64_RE = re.compile(
    r"(?:[A-Za-z0-9+/]{80,}={0,2}|data:[^;\s]+;base64,)", re.IGNORECASE
)
_BLOCKED_METADATA_KEYS = frozenset(
    {
        "api_key",
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
        "system_prompt",
        "token",
    }
)


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

    @property
    def active_plugin(self):  # type: ignore[no-untyped-def]
        return self.registry.active_plugin

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
        descriptor = self.active_plugin.descriptor
        cached = self.session_store.get(identity)
        plugin_changed = cached is not None and (
            cached.plugin_id != descriptor.plugin_id
            or cached.plugin_version != descriptor.plugin_version
        )
        resolution = self.session_store.resolve(
            identity,
            reset=reset or plugin_changed,
            loader=lambda: self._open_record(
                identity=identity,
                state=state,
                descriptor=descriptor,
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
        if state.memory_context_prepared:
            attached = self.attach_frozen_context(state=state)
            return attached or SessionMemorySnapshot(
                status="unavailable",
                error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
            )

        identity = _identity_from_state(state)
        record = self.session_store.get(identity)
        if record is None:
            return self._freeze_for_run(
                state,
                SessionMemorySnapshot(
                    plugin_id=self.active_plugin.descriptor.plugin_id,
                    status="unavailable",
                    error_codes=[MEMORY_PLUGIN_UNAVAILABLE],
                ),
            )

        descriptor = self.active_plugin.descriptor
        if (
            record.plugin_id != descriptor.plugin_id
            or record.plugin_version != descriptor.plugin_version
        ):
            return self._freeze_for_run(
                state,
                _fallback_snapshot(record, MEMORY_PLUGIN_UNAVAILABLE),
            )
        if not descriptor.capabilities.supports_context_refresh:
            return self._freeze_for_run(state, record.baseline)

        token = _DelegatingCancellationToken(cancel_token)
        if token.is_cancelled():
            return self._freeze_for_run(
                state,
                _fallback_snapshot(record, MEMORY_PLUGIN_UNAVAILABLE),
            )
        current_text = state.request.text
        if not isinstance(current_text, str) or not current_text:
            return self._freeze_for_run(
                state,
                _fallback_snapshot(record, MEMORY_PLUGIN_INTERNAL_ERROR),
            )

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
            cancellation=token,
        )
        raw, failure_code = self._invoke(
            lambda: self.active_plugin.prepare_context(request),
            timeout_seconds=self.execution_policy.prepare_context_timeout_seconds,
            cancellation=token,
        )
        if failure_code is not None:
            return self._freeze_for_run(
                state,
                _fallback_snapshot(
                    record,
                    failure_code,
                    extra_codes=[issue.code for issue in media_issues],
                ),
            )

        contribution = self._validated_contribution(
            raw,
            descriptor=descriptor,
            owner_scope=record.identity.session_id,
        )
        if contribution is None:
            return self._freeze_for_run(
                state,
                _fallback_snapshot(
                    record,
                    MEMORY_PLUGIN_INVALID_RESULT,
                    extra_codes=[issue.code for issue in media_issues],
                ),
            )
        merged = _merge_items(record.baseline.memories, contribution.items)
        snapshot = SessionMemorySnapshot(
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
        return self._freeze_for_run(state, snapshot)

    def attach_frozen_context(
        self,
        state: AgentState,
    ) -> SessionMemorySnapshot | None:
        """Attach an independent copy of this run's immutable source snapshot."""

        frozen = state.frozen_memory_context
        if frozen is None:
            record = self.session_store.get(_identity_from_state(state))
            if record is None:
                state.session_memory_snapshot = None
                return None
            frozen = record.baseline.model_copy(deep=True)
        attached = frozen.model_copy(deep=True)
        state.session_memory_snapshot = attached
        return attached.model_copy(deep=True)

    def _open_record(
        self,
        *,
        identity: RequestIdentity,
        state: AgentState,
        descriptor: MemoryPluginDescriptor,
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
        cancellation = _DelegatingCancellationToken(None)
        request = MemorySessionOpenRequest(
            memory_session_id=memory_session_id,
            identity=plugin_identity,
            opened_at=self._now(),
            entry_profile=_entry_profile(state),
            deadline=self._deadline(self.execution_policy.open_session_timeout_seconds),
            cancellation=cancellation,
        )
        raw, failure_code = self._invoke(
            lambda: self.active_plugin.open_session(request),
            timeout_seconds=self.execution_policy.open_session_timeout_seconds,
            cancellation=cancellation,
        )
        result = self._validated_open_result(
            raw,
            descriptor=descriptor,
            owner_scope=plugin_identity.session_id,
        )
        if failure_code is not None or result is None:
            code = failure_code or MEMORY_PLUGIN_INVALID_RESULT
            baseline = SessionMemorySnapshot(
                plugin_id=descriptor.plugin_id,
                status="degraded",
                error_codes=[code],
            )
            return MemoryPluginSessionRecord(
                plugin_id=descriptor.plugin_id,
                plugin_version=descriptor.plugin_version,
                runtime_identity_key=runtime_memory_identity_key(identity),
                identity=plugin_identity,
                memory_session_id=memory_session_id,
                session_handle=None,
                baseline=baseline,
                status="degraded",
            )

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
        return MemoryPluginSessionRecord(
            plugin_id=descriptor.plugin_id,
            plugin_version=descriptor.plugin_version,
            runtime_identity_key=runtime_memory_identity_key(identity),
            identity=plugin_identity,
            memory_session_id=memory_session_id,
            session_handle=result.session_handle,
            baseline=baseline,
            status=result.status,
        )

    def _validated_open_result(
        self,
        raw: object,
        *,
        descriptor: MemoryPluginDescriptor,
        owner_scope: str,
    ) -> MemorySessionOpenResult | None:
        result = _round_trip_model(raw, MemorySessionOpenResult)
        if result is None:
            return None
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
                    )
                },
                deep=True,
            )
        if result.session_handle is not None and _forbidden_string(
            result.session_handle
        ):
            return None
        if not all(_valid_issue(issue) for issue in result.issues):
            return None
        if result.initial_contribution is not None:
            contribution = self._validated_contribution(
                result.initial_contribution,
                descriptor=descriptor,
                owner_scope=owner_scope,
            )
            if contribution is None:
                return None
            result = result.model_copy(
                update={"initial_contribution": contribution},
                deep=True,
            )
        return result

    def _validated_contribution(
        self,
        raw: object,
        *,
        descriptor: MemoryPluginDescriptor,
        owner_scope: str,
    ) -> MemoryContextContribution | None:
        contribution = _round_trip_model(raw, MemoryContextContribution)
        if contribution is None:
            return None
        contribution = _restore_signed_media_refs(contribution, raw)
        if len(contribution.items) > self.execution_policy.max_context_items:
            return None
        if not all(_valid_issue(issue) for issue in contribution.issues):
            return None

        total_chars = 0
        total_media_items = 0
        total_media_bytes = 0
        for item in contribution.items:
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
            except (TypeError, ValueError, OverflowError):
                return None
            if _forbidden_metadata(item.metadata):
                return None
            total_chars += len(item.text) + len(metadata_json)
            if total_chars > self.execution_policy.max_context_chars:
                return None

            total_media_items += len(item.media_refs)
            if total_media_items > self.execution_policy.max_media_items_per_turn:
                return None
            for ref in item.media_refs:
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
        return contribution.model_copy(deep=True)

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
    ) -> tuple[object | None, str | None]:
        future: Future[Any] = self._executor.submit(callback)
        call_deadline = monotonic() + timeout_seconds
        while True:
            if cancellation.is_cancelled():
                future.cancel()
                return None, MEMORY_PLUGIN_UNAVAILABLE
            remaining = call_deadline - monotonic()
            if remaining <= 0:
                future.cancel()
                return None, MEMORY_PLUGIN_TIMEOUT
            try:
                result = future.result(timeout=min(remaining, 0.01))
            except FutureTimeout:
                continue
            except Exception:
                return None, MEMORY_PLUGIN_INTERNAL_ERROR
            if cancellation.is_cancelled():
                return None, MEMORY_PLUGIN_UNAVAILABLE
            return result, None

    def _freeze_for_run(
        self,
        state: AgentState,
        snapshot: SessionMemorySnapshot,
    ) -> SessionMemorySnapshot:
        state.frozen_memory_context = snapshot.model_copy(deep=True)
        state.memory_context_prepared = True
        attached = self.attach_frozen_context(state=state)
        assert attached is not None
        return attached

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Memory Plugin Host clock must be timezone-aware")
        return value

    def _deadline(self, timeout_seconds: float) -> datetime:
        return self._now() + timedelta(seconds=timeout_seconds)


def _round_trip_model(raw: object, model_type: type[_ModelT]) -> _ModelT | None:
    if type(raw) is not model_type:
        return None
    try:
        if not _has_exact_model_shape(raw, model_type):
            return None
        encoded = raw.model_dump_json()
        return model_type.model_validate_json(encoded)
    except Exception:
        return None


def _has_exact_model_shape(raw: BaseModel, model_type: type[BaseModel]) -> bool:
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
        return (
            type(items) is list
            and type(issues) is list
            and all(_has_exact_model_shape(item, MemoryContextItem) for item in items)
            and all(
                _has_exact_model_shape(issue, MemoryPluginIssue) for issue in issues
            )
        )
    if model_type is MemoryContextItem:
        media_refs = dict.__getitem__(values, "media_refs")
        return type(media_refs) is list and all(
            _has_exact_model_shape(ref, ManagedMediaRef) for ref in media_refs
        )
    if model_type is MemorySessionOpenResult:
        issues = dict.__getitem__(values, "issues")
        contribution = dict.__getitem__(values, "initial_contribution")
        return (
            type(issues) is list
            and all(
                _has_exact_model_shape(issue, MemoryPluginIssue) for issue in issues
            )
            and (
                contribution is None
                or _has_exact_model_shape(contribution, MemoryContextContribution)
            )
        )
    return True


def _restore_signed_media_refs(
    validated: MemoryContextContribution,
    raw: MemoryContextContribution,
) -> MemoryContextContribution:
    """Keep exact Host-issued timestamps while retaining full JSON validation."""

    raw_values = object.__getattribute__(raw, "__dict__")
    raw_items = dict.__getitem__(raw_values, "items")
    restored_items: list[MemoryContextItem] = []
    for validated_item, raw_item in zip(validated.items, raw_items, strict=True):
        item_values = object.__getattribute__(raw_item, "__dict__")
        raw_refs = dict.__getitem__(item_values, "media_refs")
        restored_items.append(
            validated_item.model_copy(
                update={"media_refs": [ref.model_copy(deep=True) for ref in raw_refs]},
                deep=True,
            )
        )
    return validated.model_copy(update={"items": restored_items}, deep=True)


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


def _forbidden_metadata(value: Any, *, key: str | None = None) -> bool:
    if key is not None:
        normalized = key.strip().lower()
        if normalized in _BLOCKED_METADATA_KEYS or normalized.endswith(
            ("_base64", "_bytes", "_blob", "_data_uri")
        ):
            return True
    if isinstance(value, str):
        return _forbidden_string(value)
    if isinstance(value, list):
        return any(_forbidden_metadata(item) for item in value)
    if isinstance(value, dict):
        return any(
            not isinstance(item_key, str)
            or _forbidden_metadata(item_value, key=item_key)
            for item_key, item_value in value.items()
        )
    return False


def _forbidden_string(value: str) -> bool:
    if not isinstance(value, str):
        return True
    return bool(
        value.lower().startswith("file://")
        or _ABSOLUTE_PATH_RE.search(value)
        or _BASE64_RE.search(value)
    )


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


def _merge_items(
    baseline: list[MemoryContextItem],
    current: list[MemoryContextItem],
) -> list[MemoryContextItem]:
    merged: dict[str, MemoryContextItem] = {}
    for item in [*baseline, *current]:
        merged[item.memory_id] = item.model_copy(deep=True)
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
