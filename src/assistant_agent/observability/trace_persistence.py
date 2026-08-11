"""Non-blocking trace persistence for the local server process."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any

from assistant_agent.runtime.hooks import HookManager, HookTraceStore
from assistant_agent.observability.langfuse_scores import create_langfuse_score_trace_observer_from_env
from assistant_agent.observability.otel_exporter import (
    create_langsmith_text_otel_trace_observer_from_env,
    create_text_otel_trace_observer_from_env,
)
from assistant_agent.observability.trace_ledger import DailyTraceLedgerStore
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.observability.trace_store import (
    CompositeTraceStore,
    InMemoryTraceStore,
    TraceEvent,
    TraceStore,
)


DEFAULT_TRACE_PATH = Path(".data/trace_ledger")
DEFAULT_TRACE_QUEUE_CAPACITY = 4096
DEFAULT_TRACE_CLOSE_TIMEOUT_SECONDS = 1.0


@dataclass
class _TraceCommand:
    kind: str
    payload: Any = None
    done: Event | None = None
    result: dict[str, Any] = field(default_factory=dict)


class BufferedJsonlTraceStore:
    """Serialize writes on a bounded daemon worker without blocking appenders."""

    def __init__(self, sink: TraceStore, *, capacity: int = DEFAULT_TRACE_QUEUE_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.sink = sink
        self.capacity = capacity
        self._queue: Queue[_TraceCommand] = Queue(maxsize=capacity)
        self._state_lock = Lock()
        self._close_lock = Lock()
        self._dropped_event_count = 0
        self._error_count = 0
        self._last_error: str | None = None
        self._closing = False
        self._closed = False
        self._worker = Thread(
            target=self._run,
            name="assistant-agent-trace-writer",
            daemon=True,
        )
        self._worker.start()

    @property
    def dropped_event_count(self) -> int:
        with self._state_lock:
            return self._dropped_event_count

    @property
    def error_count(self) -> int:
        with self._state_lock:
            return self._error_count

    @property
    def last_error(self) -> str | None:
        with self._state_lock:
            return self._last_error

    @property
    def worker_alive(self) -> bool:
        return self._worker.is_alive()

    def append(self, event: TraceEvent) -> None:
        with self._state_lock:
            closing = self._closing or self._closed
        if closing:
            self._record_drop()
            return
        try:
            self._queue.put_nowait(_TraceCommand("append", event))
        except Full:
            self._record_drop()

    def list_by_run(self, run_id: str) -> list[TraceEvent]:
        self.flush(timeout=DEFAULT_TRACE_CLOSE_TIMEOUT_SECONDS)
        return self.sink.list_by_run(run_id)

    def list_by_trace(self, trace_id: str) -> list[TraceEvent]:
        self.flush(timeout=DEFAULT_TRACE_CLOSE_TIMEOUT_SECONDS)
        return self.sink.list_by_trace(trace_id)

    def node_path(self, run_id: str) -> list[str]:
        self.flush(timeout=DEFAULT_TRACE_CLOSE_TIMEOUT_SECONDS)
        return self.sink.node_path(run_id)

    def list_by_user(self, user_id: str) -> list[TraceEvent]:
        self.flush(timeout=DEFAULT_TRACE_CLOSE_TIMEOUT_SECONDS)
        return self.sink.list_by_user(user_id)

    def delete_by_user(self, user_id: str) -> int:
        command = _TraceCommand("delete_by_user", user_id, done=Event())
        with self._state_lock:
            if self._closing or self._closed:
                raise RuntimeError("trace writer is closing")
        self._queue.put(command)
        command.done.wait()
        if "error" in command.result:
            raise RuntimeError(command.result["error"])
        return int(command.result.get("deleted", 0))

    def flush(self, *, timeout: float = DEFAULT_TRACE_CLOSE_TIMEOUT_SECONDS) -> bool:
        if timeout <= 0:
            return False
        with self._state_lock:
            if self._closed:
                return not self._worker.is_alive()
        return self._flush_control(timeout=timeout)

    def close(self, *, timeout: float = DEFAULT_TRACE_CLOSE_TIMEOUT_SECONDS) -> bool:
        if timeout <= 0:
            return False
        with self._close_lock:
            with self._state_lock:
                if self._closed:
                    return not self._worker.is_alive()
                self._closing = True
            deadline = monotonic() + timeout
            flushed = self._flush_control(timeout=max(0.0, deadline - monotonic()))
            stopped = self._enqueue_control(
                _TraceCommand("stop"),
                timeout=max(0.0, deadline - monotonic()),
            )
            if stopped:
                self._worker.join(timeout=max(0.0, deadline - monotonic()))
            with self._state_lock:
                self._closed = not self._worker.is_alive()
            return flushed and self._closed

    def _flush_control(self, *, timeout: float) -> bool:
        if timeout <= 0:
            return False
        command = _TraceCommand("flush", done=Event())
        deadline = monotonic() + timeout
        if not self._enqueue_control(command, timeout=timeout):
            return False
        return command.done.wait(timeout=max(0.0, deadline - monotonic()))

    def _enqueue_control(self, command: _TraceCommand, *, timeout: float) -> bool:
        if timeout <= 0:
            return False
        try:
            self._queue.put(command, timeout=timeout)
        except Full:
            return False
        return True

    def _run(self) -> None:
        while True:
            try:
                command = self._queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                if command.kind == "append":
                    self.sink.append(command.payload)
                elif command.kind == "delete_by_user":
                    command.result["deleted"] = self.sink.delete_by_user(str(command.payload))
                elif command.kind == "stop":
                    return
            except Exception as exc:  # noqa: BLE001 - background persistence boundary.
                safe_error = sanitize_error_message(exc)
                command.result["error"] = safe_error
                with self._state_lock:
                    self._error_count += 1
                    self._last_error = safe_error
            finally:
                if command.done is not None:
                    command.done.set()
                self._queue.task_done()

    def _record_drop(self) -> None:
        with self._state_lock:
            self._dropped_event_count += 1


def create_server_trace_store(
    *,
    path: Path | str = DEFAULT_TRACE_PATH,
    capacity: int = DEFAULT_TRACE_QUEUE_CAPACITY,
) -> CompositeTraceStore:
    """Create immediate reads with background completeness-ledger persistence."""

    observers = [
        observer
        for observer in (
            create_text_otel_trace_observer_from_env(),
            create_langsmith_text_otel_trace_observer_from_env(),
        )
        if observer is not None
    ]
    return _create_runtime_trace_store(
        path=path,
        capacity=capacity,
        require_otel=False,
        include_score_observer=True,
        otel_observers=observers,
    )


def create_experiment_trace_store(
    *,
    path: Path | str = DEFAULT_TRACE_PATH,
    capacity: int = DEFAULT_TRACE_QUEUE_CAPACITY,
) -> CompositeTraceStore:
    """Compatibility alias for the fail-closed Langfuse Experiment store."""

    return create_langfuse_experiment_trace_store(path=path, capacity=capacity)


def create_langfuse_experiment_trace_store(
    *,
    path: Path | str = DEFAULT_TRACE_PATH,
    capacity: int = DEFAULT_TRACE_QUEUE_CAPACITY,
) -> CompositeTraceStore:
    """Create an export-capable store for a fail-closed Langfuse Experiment."""

    return _create_runtime_trace_store(
        path=path,
        capacity=capacity,
        require_otel=True,
        include_score_observer=False,
    )


def create_langsmith_experiment_trace_store(
    *,
    project_id: str,
    path: Path | str = DEFAULT_TRACE_PATH,
    capacity: int = DEFAULT_TRACE_QUEUE_CAPACITY,
) -> CompositeTraceStore:
    """Create a fail-closed store exporting only to one LangSmith Experiment."""

    observer = create_langsmith_text_otel_trace_observer_from_env(
        project_override=project_id,
        required=True,
    )
    return _create_runtime_trace_store(
        path=path,
        capacity=capacity,
        require_otel=True,
        include_score_observer=False,
        otel_observers=[observer],
        required_backend="LangSmith",
    )


def _create_runtime_trace_store(
    *,
    path: Path | str,
    capacity: int,
    require_otel: bool,
    include_score_observer: bool,
    otel_observers: list[object] | None = None,
    required_backend: str = "Langfuse",
) -> CompositeTraceStore:
    observers = otel_observers
    if observers is None:
        observer = create_text_otel_trace_observer_from_env()
        observers = [observer] if observer is not None else []
    observers = [observer for observer in observers if observer is not None]
    if require_otel and not observers:
        raise RuntimeError(
            f"{required_backend} Experiment requires configured OTel trace export"
        )

    primary = InMemoryTraceStore()
    secondary = BufferedJsonlTraceStore(DailyTraceLedgerStore(path), capacity=capacity)
    secondaries: list[TraceStore] = [secondary]
    for observer in observers:
        secondaries.append(HookTraceStore(HookManager([observer])))
    if include_score_observer:
        score_observer = create_langfuse_score_trace_observer_from_env()
        if score_observer is not None:
            secondaries.append(HookTraceStore(HookManager([score_observer])))
    return CompositeTraceStore(
        primary,
        secondaries,
        read_fallbacks=[secondary],
    )


def close_trace_store(
    trace_store: TraceStore | None,
    *,
    timeout: float = DEFAULT_TRACE_CLOSE_TIMEOUT_SECONDS,
) -> bool:
    """Best-effort bounded close for stores that own background resources."""

    if trace_store is None:
        return True
    close = getattr(trace_store, "close", None)
    if not callable(close):
        return True
    try:
        result = close(timeout=timeout)
    except Exception:
        return False
    return result is not False
