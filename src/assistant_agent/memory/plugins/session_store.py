"""Process-local session state for the active Memory Plugin."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition
from typing import Literal

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.memory.plugins.contracts import MemoryIdentity


RuntimeMemoryIdentityKey = tuple[str, str, str]
MemoryPluginSessionResolutionStatus = Literal["loaded", "reused"]


@dataclass(frozen=True)
class MemoryPluginSessionRecord:
    """Host-owned state for one Runtime session and one active Plugin."""

    plugin_id: str
    plugin_version: str
    runtime_identity_key: RuntimeMemoryIdentityKey
    identity: MemoryIdentity
    memory_session_id: str
    session_handle: str | None
    baseline: SessionMemorySnapshot
    status: str

    def __post_init__(self) -> None:
        if not self.plugin_id or not self.plugin_version or not self.memory_session_id:
            raise ValueError("memory plugin session identifiers must be non-empty")
        if len(self.runtime_identity_key) != 3 or not all(
            isinstance(value, str) and value for value in self.runtime_identity_key
        ):
            raise ValueError("runtime identity key must contain three values")
        object.__setattr__(self, "baseline", self.baseline.model_copy(deep=True))

    @property
    def plugin_identity(self) -> MemoryIdentity:
        return self.identity.model_copy(deep=True)

    @property
    def handle(self) -> str | None:
        return self.session_handle


@dataclass(frozen=True)
class MemoryPluginSessionResolution:
    record: MemoryPluginSessionRecord
    status: MemoryPluginSessionResolutionStatus


class MemoryPluginSessionStore:
    """Resolve one isolated Plugin session record per Runtime session."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise TypeError("max_entries must be an integer")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._condition = Condition()
        self._entries: OrderedDict[
            RuntimeMemoryIdentityKey, MemoryPluginSessionRecord
        ] = OrderedDict()
        self._loading: dict[RuntimeMemoryIdentityKey, int] = {}
        self._epochs: dict[RuntimeMemoryIdentityKey, int] = {}

    def resolve(
        self,
        identity: RequestIdentity,
        *,
        loader: Callable[[], MemoryPluginSessionRecord],
        before_publish: Callable[[MemoryPluginSessionRecord], MemoryPluginSessionRecord]
        | None = None,
        reset: bool = False,
    ) -> MemoryPluginSessionResolution:
        """Run ``loader`` once for a session and return defensive copies."""

        key = runtime_memory_identity_key(identity)
        reset_pending = reset
        while True:
            with self._condition:
                if reset_pending:
                    self._invalidate(key)
                    reset_pending = False
                while key in self._loading:
                    self._condition.wait()
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    return MemoryPluginSessionResolution(
                        record=_copy_record(cached),
                        status="reused",
                    )
                load_epoch = self._epochs.get(key, 0)
                self._loading[key] = load_epoch

            try:
                loaded = loader()
                if loaded.runtime_identity_key != key:
                    raise ValueError("memory plugin session identity mismatch")
                frozen = _copy_record(loaded)
            except BaseException as error:
                with self._condition:
                    stale = self._epochs.get(key, 0) != load_epoch
                    if self._loading.get(key) == load_epoch:
                        self._loading.pop(key, None)
                    self._condition.notify_all()
                if stale and isinstance(error, Exception):
                    continue
                raise

            with self._condition:
                if self._epochs.get(key, 0) != load_epoch:
                    if self._loading.get(key) == load_epoch:
                        self._loading.pop(key, None)
                    self._condition.notify_all()
                    continue
                if before_publish is not None:
                    try:
                        frozen = before_publish(frozen)
                        if frozen.runtime_identity_key != key:
                            raise ValueError("memory plugin session identity mismatch")
                    except BaseException:
                        if self._loading.get(key) == load_epoch:
                            self._loading.pop(key, None)
                        self._condition.notify_all()
                        raise
                self._entries[key] = frozen
                self._entries.move_to_end(key)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
                if self._loading.get(key) == load_epoch:
                    self._loading.pop(key, None)
                self._condition.notify_all()
            return MemoryPluginSessionResolution(
                record=_copy_record(frozen),
                status="loaded",
            )

    def get(self, identity: RequestIdentity) -> MemoryPluginSessionRecord | None:
        key = runtime_memory_identity_key(identity)
        with self._condition:
            record = self._entries.get(key)
            if record is None:
                return None
            self._entries.move_to_end(key)
            return _copy_record(record)

    def clear_session(self, *, user_id: str, session_id: str) -> int:
        with self._condition:
            keys = {
                key
                for key in (*self._entries, *self._loading)
                if key[0] == user_id and key[2] == session_id
            }
            for key in keys:
                self._invalidate(key)
            self._condition.notify_all()
            return len(keys)

    def clear_user(self, *, user_id: str, agent_id: str | None = None) -> int:
        with self._condition:
            keys = {
                key
                for key in (*self._entries, *self._loading)
                if key[0] == user_id and (agent_id is None or key[1] == agent_id)
            }
            for key in keys:
                self._invalidate(key)
            self._condition.notify_all()
            return len(keys)

    def _invalidate(self, key: RuntimeMemoryIdentityKey) -> None:
        self._entries.pop(key, None)
        self._epochs[key] = self._epochs.get(key, 0) + 1


def runtime_memory_identity_key(
    identity: RequestIdentity,
) -> RuntimeMemoryIdentityKey:
    if not identity.session_id:
        raise ValueError("session_id is required for Memory Plugin sessions")
    return identity.user_id, identity.agent_id, identity.session_id


def _copy_record(record: MemoryPluginSessionRecord) -> MemoryPluginSessionRecord:
    return MemoryPluginSessionRecord(
        plugin_id=record.plugin_id,
        plugin_version=record.plugin_version,
        runtime_identity_key=tuple(record.runtime_identity_key),
        identity=record.identity.model_copy(deep=True),
        memory_session_id=record.memory_session_id,
        session_handle=record.session_handle,
        baseline=record.baseline.model_copy(deep=True),
        status=record.status,
    )
