"""Session ownership pool for visual semantic stores."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from time import monotonic

from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore


@dataclass
class _StoreEntry:
    store: SessionVisualSemanticStore
    touched_at: float


class SessionVisualSemanticStorePool:
    """Reuse stores by trusted owner/session identity and close on eviction."""

    def __init__(
        self,
        *,
        root: Path | str,
        ttl_seconds: float = 1800.0,
        max_sessions: int = 256,
        max_records: int = 256,
        max_evidence_bytes: int = 256 * 1024 * 1024,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("visual semantic store ttl must be positive")
        if max_sessions <= 0:
            raise ValueError("visual semantic store pool size must be positive")
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.max_records = max_records
        self.max_evidence_bytes = max_evidence_bytes
        self.clock = clock
        self._entries: OrderedDict[tuple[str, str], _StoreEntry] = OrderedDict()
        self._lock = Lock()
        self._closed = False

    def resolve(self, user_id: str, session_id: str) -> SessionVisualSemanticStore:
        _validate_identity(user_id, session_id)
        now = self.clock()
        to_close: list[SessionVisualSemanticStore] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("visual_semantic_store_pool_closed")
            to_close.extend(self._evict_expired_locked(now))
            key = (user_id, session_id)
            entry = self._entries.get(key)
            if entry is None:
                entry = _StoreEntry(
                    store=SessionVisualSemanticStore(
                        root=self.root / _identity_digest(user_id, session_id),
                        session_id=session_id,
                        max_records=self.max_records,
                        max_evidence_bytes=self.max_evidence_bytes,
                    ),
                    touched_at=now,
                )
                self._entries[key] = entry
            else:
                entry.touched_at = now
                self._entries.move_to_end(key)
            while len(self._entries) > self.max_sessions:
                _, evicted = self._entries.popitem(last=False)
                to_close.append(evicted.store)
            result = entry.store
        self._close_all(to_close)
        return result

    def peek(self, user_id: str, session_id: str) -> SessionVisualSemanticStore | None:
        _validate_identity(user_id, session_id)
        now = self.clock()
        with self._lock:
            if self._closed:
                return None
            expired = self._evict_expired_locked(now)
            key = (user_id, session_id)
            entry = self._entries.get(key)
            if entry is not None:
                entry.touched_at = now
                self._entries.move_to_end(key)
                result = entry.store
            else:
                result = None
        self._close_all(expired)
        return result

    def clear_session(self, user_id: str, session_id: str) -> bool:
        _validate_identity(user_id, session_id)
        with self._lock:
            entry = self._entries.pop((user_id, session_id), None)
        if entry is None:
            return False
        self._close_all([entry.store])
        return True

    def clear_user(self, user_id: str) -> int:
        if not user_id:
            raise ValueError("user id must be non-empty")
        with self._lock:
            keys = [key for key in self._entries if key[0] == user_id]
            stores = [self._entries.pop(key).store for key in keys]
        self._close_all(stores)
        return len(stores)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stores = [entry.store for entry in self._entries.values()]
            self._entries.clear()
        self._close_all(stores)

    def _evict_expired_locked(self, now: float) -> list[SessionVisualSemanticStore]:
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.touched_at >= self.ttl_seconds
        ]
        return [self._entries.pop(key).store for key in expired]

    @staticmethod
    def _close_all(stores: list[SessionVisualSemanticStore]) -> None:
        for store in stores:
            try:
                store.close()
            except Exception:
                pass


def _identity_digest(user_id: str, session_id: str) -> str:
    return sha256(f"{user_id}\0{session_id}".encode()).hexdigest()


def _validate_identity(user_id: str, session_id: str) -> None:
    if not user_id or not session_id:
        raise ValueError("user and session ids must be non-empty")
