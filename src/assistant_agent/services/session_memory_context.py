"""Process-local session snapshots for bounded long-term memory context."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, Lock
from typing import Literal

from assistant_agent.memory.manager import MemoryContext
from assistant_agent.schemas.identity import RequestIdentity


SessionMemorySnapshotStatus = Literal["loaded", "reused", "missing"]
SessionMemoryKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class SessionMemoryContextResolution:
    """One immutable resolution from the session snapshot boundary."""

    context: MemoryContext | None
    status: SessionMemorySnapshotStatus


class SessionMemoryContextStore:
    """Keep one immutable long-term memory snapshot per retained session."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._condition = Condition()
        self._entries: OrderedDict[SessionMemoryKey, MemoryContext] = OrderedDict()
        self._loading: set[SessionMemoryKey] = set()

    def resolve(
        self,
        identity: RequestIdentity,
        *,
        loader: Callable[[], MemoryContext],
        allow_load: bool,
        reset: bool = False,
    ) -> SessionMemoryContextResolution:
        """Load once or reuse; a later-turn miss never triggers recall."""

        key = _session_memory_key(identity)
        with self._condition:
            if reset:
                self._entries.pop(key, None)
            while key in self._loading:
                self._condition.wait()
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    return SessionMemoryContextResolution(
                        context=cached.model_copy(deep=True),
                        status="reused",
                    )
                if not allow_load:
                    return SessionMemoryContextResolution(context=None, status="missing")
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return SessionMemoryContextResolution(
                    context=cached.model_copy(deep=True),
                    status="reused",
                )
            if not allow_load:
                return SessionMemoryContextResolution(context=None, status="missing")
            self._loading.add(key)

        try:
            loaded = loader()
            frozen = loaded.model_copy(deep=True)
        except Exception:
            with self._condition:
                self._loading.discard(key)
                self._condition.notify_all()
            raise

        with self._condition:
            self._entries[key] = frozen
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            self._loading.discard(key)
            self._condition.notify_all()
        return SessionMemoryContextResolution(
            context=frozen.model_copy(deep=True),
            status="loaded",
        )

    def clear(self, identity: RequestIdentity) -> bool:
        """Remove one retained session snapshot."""

        with self._condition:
            return self._entries.pop(_session_memory_key(identity), None) is not None

    def clear_session(self, *, user_id: str, session_id: str) -> int:
        """Remove retained snapshots matching one public user/session."""

        with self._condition:
            keys = [
                key
                for key in self._entries
                if key[1] == user_id and key[3] == session_id
            ]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def clear_user(self, *, user_id: str, tenant_id: str | None = None) -> int:
        """Remove every retained snapshot for one governed user."""

        with self._condition:
            keys = [
                key
                for key in self._entries
                if key[1] == user_id
                and (tenant_id is None or key[0] == tenant_id)
            ]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)


def _session_memory_key(identity: RequestIdentity) -> SessionMemoryKey:
    return (
        identity.tenant_id or "",
        identity.user_id,
        identity.project_id or "",
        identity.session_id or "",
    )


_DEFAULT_STORES: dict[int, SessionMemoryContextStore] = {}
_DEFAULT_STORES_LOCK = Lock()


def get_default_session_memory_context_store(
    *,
    max_entries: int = 1024,
) -> SessionMemoryContextStore:
    """Return one process-owned store for runtimes sharing session history."""

    with _DEFAULT_STORES_LOCK:
        store = _DEFAULT_STORES.get(max_entries)
        if store is None:
            store = SessionMemoryContextStore(max_entries=max_entries)
            _DEFAULT_STORES[max_entries] = store
        return store
