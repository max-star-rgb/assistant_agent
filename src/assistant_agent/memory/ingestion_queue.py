"""Bounded background queue for completed-turn memory ingestion."""

from __future__ import annotations

import atexit
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from threading import Condition, Thread
from time import monotonic


IngestionCallback = Callable[[], None]


@dataclass(frozen=True)
class MemoryIngestionSubmitResult:
    """Prompt-safe result of a non-blocking ingestion submission."""

    accepted: bool
    pending_count: int
    reason: str | None = None


class MemoryIngestionQueue:
    """Run bounded memory ingestions in the background with per-key FIFO ordering."""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_pending: int = 64,
        shutdown_timeout_seconds: float = 10.0,
    ) -> None:
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers <= 0
        ):
            raise ValueError("max_workers must be a positive integer")
        if (
            isinstance(max_pending, bool)
            or not isinstance(max_pending, int)
            or max_pending <= 0
        ):
            raise ValueError("max_pending must be a positive integer")
        if (
            isinstance(shutdown_timeout_seconds, bool)
            or not isinstance(shutdown_timeout_seconds, int | float)
            or shutdown_timeout_seconds < 0
        ):
            raise ValueError("shutdown_timeout_seconds must be non-negative")
        self.max_workers = max_workers
        self.max_pending = max_pending
        self.shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._condition = Condition()
        self._queues: dict[Hashable, deque[IngestionCallback]] = {}
        self._ready_keys: deque[Hashable] = deque()
        self._active_keys: set[Hashable] = set()
        self._workers: list[Thread] = []
        self._pending_count = 0
        self._pending_by_key: dict[Hashable, int] = {}
        self._accepting = True
        self._stopping = False
        self._atexit_registered = False

    @property
    def pending_count(self) -> int:
        with self._condition:
            return self._pending_count

    def submit(
        self,
        *,
        ordering_key: Hashable,
        callback: IngestionCallback,
    ) -> MemoryIngestionSubmitResult:
        """Enqueue without waiting for capacity or ingestion completion."""

        with self._condition:
            if not self._accepting:
                return MemoryIngestionSubmitResult(
                    accepted=False,
                    pending_count=self._pending_count,
                    reason="memory_ingestion_queue_closed",
                )
            if self._pending_count >= self.max_pending:
                return MemoryIngestionSubmitResult(
                    accepted=False,
                    pending_count=self._pending_count,
                    reason="memory_ingestion_queue_full",
                )
            self._start_workers_locked()
            queue = self._queues.setdefault(ordering_key, deque())
            queue.append(callback)
            self._pending_count += 1
            self._pending_by_key[ordering_key] = (
                self._pending_by_key.get(ordering_key, 0) + 1
            )
            if ordering_key not in self._active_keys and len(queue) == 1:
                self._ready_keys.append(ordering_key)
            self._condition.notify()
            return MemoryIngestionSubmitResult(
                accepted=True,
                pending_count=self._pending_count,
            )

    def drain(
        self,
        *,
        timeout: float | None = None,
        ordering_key: Hashable | None = None,
    ) -> bool:
        """Wait until accepted ingestions finish globally or for one key."""

        deadline = None if timeout is None else monotonic() + max(0.0, timeout)
        with self._condition:
            while self._pending_for_key_locked(ordering_key):
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, *, timeout: float | None = None) -> bool:
        """Stop accepting work, drain within the bound, and request worker exit."""

        started = monotonic()
        with self._condition:
            self._accepting = False
        drained = self.drain(
            timeout=_remaining_timeout(started=started, timeout=timeout)
        )
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            workers = tuple(self._workers)
        for worker in workers:
            remaining = _remaining_timeout(started=started, timeout=timeout)
            worker.join(remaining)
        return drained

    def _pending_for_key_locked(self, ordering_key: Hashable | None) -> int:
        if ordering_key is None:
            return self._pending_count
        return self._pending_by_key.get(ordering_key, 0)

    def _start_workers_locked(self) -> None:
        if self._workers:
            return
        if not self._atexit_registered:
            atexit.register(self._close_at_exit)
            self._atexit_registered = True
        for index in range(self.max_workers):
            worker = Thread(
                target=self._worker,
                name=f"memory-ingestion-{index + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _close_at_exit(self) -> None:
        try:
            self.close(timeout=self.shutdown_timeout_seconds)
        except Exception:
            pass

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._ready_keys:
                    if self._stopping and self._pending_count == 0:
                        return
                    self._condition.wait()
                ordering_key = self._ready_keys.popleft()
                queue = self._queues[ordering_key]
                callback = queue.popleft()
                self._active_keys.add(ordering_key)
            try:
                callback()
            except Exception:
                # Ingestion callbacks own structured failure recording. This final
                # guard prevents one unexpected callback defect from killing a
                # shared worker or changing foreground run behavior.
                pass
            finally:
                with self._condition:
                    self._active_keys.discard(ordering_key)
                    self._pending_count -= 1
                    key_pending = self._pending_by_key[ordering_key] - 1
                    if key_pending:
                        self._pending_by_key[ordering_key] = key_pending
                    else:
                        self._pending_by_key.pop(ordering_key, None)
                    queue = self._queues.get(ordering_key)
                    if queue:
                        self._ready_keys.append(ordering_key)
                    else:
                        self._queues.pop(ordering_key, None)
                    self._condition.notify_all()


def _remaining_timeout(*, started: float, timeout: float | None) -> float | None:
    if timeout is None:
        return None
    return max(0.0, float(timeout) - (monotonic() - started))
