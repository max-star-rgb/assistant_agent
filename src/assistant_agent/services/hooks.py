"""Observer-only harness hooks."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from inspect import Parameter, signature
from typing import Protocol

from assistant_agent.schemas.events import AgentEvent
from assistant_agent.services.hook_dispatch import HookDispatchError, build_hook_dispatch_error
from assistant_agent.services.trace_store import TraceEvent


class HookObserver(Protocol):
    """Observer protocol for harness lifecycle events."""

    def on_run_event(self, event: AgentEvent) -> None: ...

    def on_trace_event(self, event: TraceEvent) -> None: ...

    def on_hook_error(self, error: HookDispatchError) -> None: ...


class HookManager:
    """Dispatch observer-only hook events without changing runtime behavior."""

    def __init__(
        self,
        observers: Iterable[object] = (),
        *,
        continue_on_error: bool = True,
    ) -> None:
        self.observers = list(observers)
        self.continue_on_error = continue_on_error
        self._errors: list[HookDispatchError] = []

    @property
    def errors(self) -> list[HookDispatchError]:
        return list(self._errors)

    def add_observer(self, observer: object) -> None:
        self.observers.append(observer)

    def on_run_event(self, event: AgentEvent) -> None:
        self._dispatch("on_run_event", event)

    def on_trace_event(self, event: TraceEvent) -> None:
        self._dispatch("on_trace_event", event)

    def on_hook_error(self, error: HookDispatchError) -> None:
        self._dispatch_hook_error(error)

    def _dispatch(self, method_name: str, event: AgentEvent | TraceEvent) -> None:
        for index, observer in enumerate(self.observers):
            method = getattr(observer, method_name, None)
            if method is None:
                continue
            try:
                method(event)
            except Exception as exc:
                error = build_hook_dispatch_error(
                    target=observer,
                    target_index=index,
                    operation=method_name,
                    event=event,
                    exc=exc,
                )
                self._errors.append(error)
                self._dispatch_hook_error(error)
                if not self.continue_on_error:
                    raise

    def _dispatch_hook_error(self, error: HookDispatchError) -> None:
        for index, observer in enumerate(self.observers):
            method = getattr(observer, "on_hook_error", None)
            if method is None:
                continue
            try:
                method(error)
            except Exception as exc:
                self._errors.append(
                    build_hook_dispatch_error(
                        target=observer,
                        target_index=index,
                        operation="on_hook_error",
                        event=None,
                        exc=exc,
                    )
                )
                if not self.continue_on_error:
                    raise


class HookEventSink:
    """EventSink adapter that forwards AgentEvent records to HookManager."""

    def __init__(self, manager: HookManager) -> None:
        self.manager = manager

    def emit(self, event: AgentEvent) -> None:
        self.manager.on_run_event(event)


class HookTraceStore:
    """TraceStore adapter that forwards trace writes to HookManager."""

    def __init__(self, manager: HookManager) -> None:
        self.manager = manager

    def append(self, event: TraceEvent) -> None:
        self.manager.on_trace_event(event)

    def list_by_run(self, run_id: str) -> list[TraceEvent]:
        return []

    def list_by_trace(self, trace_id: str) -> list[TraceEvent]:
        return []

    def node_path(self, run_id: str) -> list[str]:
        return []

    def list_by_user(self, user_id: str) -> list[TraceEvent]:
        return []

    def delete_by_user(self, user_id: str) -> int:
        return 0

    def close(self, *, timeout: float = 1.0) -> bool:
        closed = True
        for observer in reversed(self.manager.observers):
            method = getattr(observer, "close", None)
            if not callable(method):
                method = getattr(observer, "shutdown", None)
            if not callable(method):
                continue
            result = _call_lifecycle(method, timeout=timeout)
            if result is False:
                closed = False
        return closed


def _call_lifecycle(method: Callable[..., object], *, timeout: float) -> bool:
    if _accepts_timeout(method):
        result = method(timeout=timeout)
    else:
        result = method()
    return result is not False


def _accepts_timeout(method: Callable[..., object]) -> bool:
    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in parameters or any(
        parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
