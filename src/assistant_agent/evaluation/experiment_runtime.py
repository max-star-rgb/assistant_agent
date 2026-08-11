"""Shared production Runtime composition for Langfuse Experiment tasks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from opentelemetry import trace as otel_trace

from assistant_agent.observability.trace_context import RuntimeTraceContext
from assistant_agent.runtime.runtime_host import RuntimeHost


RuntimeBuilder = Callable[[Any], Any]
TraceStoreFactory = Callable[[], Any]
TraceContextProvider = Callable[[], RuntimeTraceContext | None]


class ExperimentRuntimeHost:
    """Run one Experiment item below the active Langfuse task span."""

    def __init__(
        self,
        host: RuntimeHost,
        *,
        trace_context_provider: TraceContextProvider,
    ) -> None:
        self._host = host
        self._trace_context_provider = trace_context_provider

    @property
    def trace_store(self) -> Any:
        return self._host.trace_store

    def run_state(self, request: Any) -> Any:
        trace_context = self._trace_context_provider()
        if trace_context is None:
            raise RuntimeError("Langfuse Experiment task has no active OTel parent span")
        return self._host.run_state(request, trace_context=trace_context)

    def close(self, *, timeout: float = 1.0) -> bool:
        return self._host.close(timeout=timeout)


def current_runtime_trace_context() -> RuntimeTraceContext | None:
    """Return the valid W3C identity of the current OTel observation."""

    span_context = otel_trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return RuntimeTraceContext(
        trace_id=f"{span_context.trace_id:032x}",
        parent_span_id=f"{span_context.span_id:016x}",
    )


def create_experiment_runtime_host(
    runtime_builder: RuntimeBuilder,
    *,
    trace_store_factory: TraceStoreFactory | None = None,
    trace_context_provider: TraceContextProvider = current_runtime_trace_context,
) -> ExperimentRuntimeHost:
    """Compose one item Runtime with required export and owned lifecycle."""

    if trace_store_factory is None:
        from assistant_agent.observability.trace_persistence import (
            create_experiment_trace_store,
        )

        trace_store_factory = create_experiment_trace_store
    trace_store = trace_store_factory()
    try:
        runtime = runtime_builder(trace_store)
    except BaseException:
        from assistant_agent.observability.trace_persistence import close_trace_store

        close_trace_store(trace_store)
        raise
    return ExperimentRuntimeHost(
        RuntimeHost(runtime=runtime, owned_trace_store=trace_store),
        trace_context_provider=trace_context_provider,
    )
