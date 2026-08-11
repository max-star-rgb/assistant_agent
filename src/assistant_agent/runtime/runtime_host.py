"""Ownership boundary for a composed Assistant runtime and its resources."""

from __future__ import annotations

from threading import Lock
from time import monotonic
from typing import Any

from assistant_agent.observability.trace_persistence import close_trace_store


class RuntimeHost:
    """Own one runtime and the trace store assembled for its entry process."""

    def __init__(self, *, runtime: Any, owned_trace_store: Any | None = None) -> None:
        self.runtime = runtime
        self.owned_trace_store = owned_trace_store
        self._close_lock = Lock()
        self._closed = False
        self._close_result = True

    @property
    def trace_store(self) -> Any:
        return self.runtime.trace_store

    def run_state(self, request: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise RuntimeError("runtime host is closed")
        return self.runtime.run_state(request, **kwargs)

    def close(self, *, timeout: float = 1.0) -> bool:
        """Close Runtime services, then flush and close the owned trace store once."""

        with self._close_lock:
            if self._closed:
                return self._close_result
            self._closed = True
            deadline = monotonic() + max(0.0, timeout)
            runtime_closed = _close_runtime(self.runtime)
            trace_closed = close_trace_store(
                self.owned_trace_store,
                timeout=max(0.0, deadline - monotonic()),
            )
            self._close_result = runtime_closed and trace_closed
            return self._close_result


def _close_runtime(runtime: Any) -> bool:
    close = getattr(runtime, "close", None)
    if not callable(close):
        return True
    try:
        result = close()
    except Exception:
        return False
    return result is not False
