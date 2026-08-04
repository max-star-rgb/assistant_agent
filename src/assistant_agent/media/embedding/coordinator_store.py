"""Thread-safe ownership store for session embedding coordinators."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Callable, Generic, Protocol, TypeVar


class ClosableCoordinator(Protocol):
    def close(self) -> None: ...


CoordinatorT = TypeVar("CoordinatorT", bound=ClosableCoordinator)


@dataclass
class _Entry(Generic[CoordinatorT]):
    coordinator: CoordinatorT
    touched_at: float
    active_leases: int = 0


class SessionEmbeddingCoordinatorLease(Generic[CoordinatorT]):
    """Idempotent active-use lease preventing idle/LRU eviction."""

    def __init__(
        self,
        coordinator: CoordinatorT,
        release: Callable[[], None],
    ) -> None:
        self.coordinator = coordinator
        self._release = release
        self._lock = Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()


class SessionEmbeddingCoordinatorStore(Generic[CoordinatorT]):
    """Reuse coordinators by owner/session and close them on eviction or teardown."""

    def __init__(
        self,
        *,
        factory: Callable[[str, str], CoordinatorT],
        ttl_seconds: float = 1800.0,
        max_sessions: int = 256,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("coordinator store ttl must be positive")
        if max_sessions <= 0:
            raise ValueError("coordinator store size must be positive")
        self._factory = factory
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], _Entry[CoordinatorT]] = OrderedDict()
        self._lock = Lock()
        self._closed = False

    def resolve(self, user_id: str, session_id: str) -> CoordinatorT:
        if not user_id or not session_id:
            raise ValueError("user and session ids must be non-empty")
        now = self._clock()
        to_close: list[CoordinatorT] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("embedding_coordinator_store_closed")
            to_close.extend(self._evict_expired(now))
            key = (user_id, session_id)
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(self._factory(user_id, session_id), now)
                self._entries[key] = entry
            else:
                entry.touched_at = now
                self._entries.move_to_end(key)
            to_close.extend(self._evict_over_capacity(protected_key=key))
            result = entry.coordinator
        self._close_all(to_close)
        return result

    def acquire(
        self,
        user_id: str,
        session_id: str,
    ) -> SessionEmbeddingCoordinatorLease[CoordinatorT]:
        if not user_id or not session_id:
            raise ValueError("user and session ids must be non-empty")
        now = self._clock()
        key = (user_id, session_id)
        to_close: list[CoordinatorT] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("embedding_coordinator_store_closed")
            to_close.extend(self._evict_expired(now))
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(self._factory(user_id, session_id), now)
                self._entries[key] = entry
            else:
                entry.touched_at = now
                self._entries.move_to_end(key)
            entry.active_leases += 1
            to_close.extend(self._evict_over_capacity(protected_key=key))
            coordinator = entry.coordinator
        self._close_all(to_close)
        return SessionEmbeddingCoordinatorLease(
            coordinator,
            lambda: self._release_lease(key, coordinator),
        )

    def clear_session(self, user_id: str, session_id: str) -> bool:
        with self._lock:
            entry = self._entries.pop((user_id, session_id), None)
        if entry is None:
            return False
        self._close_all([entry.coordinator])
        return True

    def peek(self, user_id: str, session_id: str) -> CoordinatorT | None:
        """Return an existing live coordinator without creating session state."""

        now = self._clock()
        with self._lock:
            if self._closed:
                return None
            expired = self._evict_expired(now)
            entry = self._entries.get((user_id, session_id))
            if entry is not None:
                entry.touched_at = now
                self._entries.move_to_end((user_id, session_id))
                coordinator = entry.coordinator
            else:
                coordinator = None
        self._close_all(expired)
        return coordinator

    def clear_user(self, user_id: str) -> int:
        with self._lock:
            keys = [key for key in self._entries if key[0] == user_id]
            coordinators = [self._entries.pop(key).coordinator for key in keys]
        self._close_all(coordinators)
        return len(coordinators)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            coordinators = [entry.coordinator for entry in self._entries.values()]
            self._entries.clear()
        self._close_all(coordinators)

    def _evict_expired(self, now: float) -> list[CoordinatorT]:
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.active_leases == 0
            and now - entry.touched_at >= self._ttl_seconds
        ]
        return [self._entries.pop(key).coordinator for key in expired]

    def _evict_over_capacity(
        self,
        *,
        protected_key: tuple[str, str],
    ) -> list[CoordinatorT]:
        evicted: list[CoordinatorT] = []
        while len(self._entries) > self._max_sessions:
            candidate = next(
                (
                    key
                    for key, entry in self._entries.items()
                    if key != protected_key and entry.active_leases == 0
                ),
                None,
            )
            if candidate is None:
                break
            evicted.append(self._entries.pop(candidate).coordinator)
        return evicted

    def _release_lease(
        self,
        key: tuple[str, str],
        coordinator: CoordinatorT,
    ) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.coordinator is not coordinator:
                return
            if entry.active_leases > 0:
                entry.active_leases -= 1
            entry.touched_at = self._clock()

    @staticmethod
    def _close_all(coordinators: list[CoordinatorT]) -> None:
        for coordinator in coordinators:
            try:
                coordinator.close()
            except Exception:
                pass
