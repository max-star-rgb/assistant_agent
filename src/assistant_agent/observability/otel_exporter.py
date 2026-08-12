"""Fail-open text trace export observer for OpenTelemetry-compatible span specs."""

from __future__ import annotations

import importlib
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timezone
from hashlib import sha256
from inspect import Parameter, signature
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from assistant_agent.observability.otel_mapping import (
    OtelTraceProjectionContext,
    OtelSpanSpec,
    build_late_text_otel_span_spec,
    build_text_otel_span_specs,
    langfuse_trace_id,
    text_otel_projection_context,
)
from assistant_agent.observability.trace_content_policy import (
    local_trace_content_enabled,
    local_memory_trace_content_enabled,
)
from assistant_agent.observability.langfuse_config import (
    default_langfuse_trace_endpoint,
    langfuse_authorization_headers,
)
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.observability.trace_store import TraceEvent, redact_trace_event
from assistant_agent.observability.turn_summary import ASSISTANT_TURN_SUMMARY_EVENT


DEFAULT_MAX_BUFFERED_RUNS = 256
DEFAULT_MAX_EVENTS_PER_RUN = 1024
DEFAULT_EXPORT_QUEUE_CAPACITY = 1024
DEFAULT_EXPORT_CLOSE_TIMEOUT_SECONDS = 1.0
DEFAULT_OTEL_EXPORT_TIMEOUT_SECONDS = 5.0
DEFAULT_OTEL_SERVICE_NAME = "assistant-agent-local"
DEFAULT_OTEL_TRACER_NAME = "assistant_agent.text_otel"
ASSISTANT_AGENT_OTEL_EXPORT_ENABLED_ENV = "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED"
ASSISTANT_AGENT_OTEL_EXPORT_ENDPOINT_ENV = "ASSISTANT_AGENT_OTEL_EXPORT_ENDPOINT"
ASSISTANT_AGENT_OTEL_EXPORT_HEADERS_ENV = "ASSISTANT_AGENT_OTEL_EXPORT_HEADERS"
ASSISTANT_AGENT_OTEL_EXPORT_TIMEOUT_ENV = "ASSISTANT_AGENT_OTEL_EXPORT_TIMEOUT"
ASSISTANT_AGENT_OTEL_EXPORT_QUEUE_CAPACITY_ENV = "ASSISTANT_AGENT_OTEL_EXPORT_QUEUE_CAPACITY"
ASSISTANT_AGENT_OTEL_SERVICE_NAME_ENV = "ASSISTANT_AGENT_OTEL_SERVICE_NAME"
OTEL_EXPORTER_OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTEL_EXPORTER_OTLP_HEADERS_ENV = "OTEL_EXPORTER_OTLP_HEADERS"
OTEL_EXPORTER_OTLP_TIMEOUT_ENV = "OTEL_EXPORTER_OTLP_TIMEOUT"
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
OTEL_EXPORTER_OTLP_TRACES_HEADERS_ENV = "OTEL_EXPORTER_OTLP_TRACES_HEADERS"
OTEL_EXPORTER_OTLP_TRACES_TIMEOUT_ENV = "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"
OTEL_SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_RAW_CONTENT_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:raw_)?(?:prompt|query|input|output|response|content|message)\b\s*[:=]\s*([^\s,;]+)"
)


@dataclass(frozen=True)
class OtlpHttpTextExporterConfig:
    """Environment-backed configuration for optional OTLP HTTP text trace export."""

    enabled: bool = False
    endpoint: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = DEFAULT_OTEL_EXPORT_TIMEOUT_SECONDS
    service_name: str = DEFAULT_OTEL_SERVICE_NAME
    queue_capacity: int = DEFAULT_EXPORT_QUEUE_CAPACITY
    include_content: bool = True
    include_vlm_input_content: bool = False
    include_memory_content: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "OtlpHttpTextExporterConfig":
        values = os.environ if env is None else env
        endpoint = _trace_endpoint_from_env(values)
        enabled = _truthy_env_value(values.get(ASSISTANT_AGENT_OTEL_EXPORT_ENABLED_ENV))
        headers = _parse_otlp_headers(
            _first_non_empty(
                values,
                ASSISTANT_AGENT_OTEL_EXPORT_HEADERS_ENV,
                OTEL_EXPORTER_OTLP_TRACES_HEADERS_ENV,
                OTEL_EXPORTER_OTLP_HEADERS_ENV,
            )
        ) or langfuse_authorization_headers(values)
        return cls(
            enabled=enabled,
            endpoint=endpoint,
            headers=headers,
            timeout_seconds=_optional_positive_float(
                _first_non_empty(
                    values,
                    ASSISTANT_AGENT_OTEL_EXPORT_TIMEOUT_ENV,
                    OTEL_EXPORTER_OTLP_TRACES_TIMEOUT_ENV,
                    OTEL_EXPORTER_OTLP_TIMEOUT_ENV,
                )
            )
            or DEFAULT_OTEL_EXPORT_TIMEOUT_SECONDS,
            service_name=_first_non_empty(
                values,
                ASSISTANT_AGENT_OTEL_SERVICE_NAME_ENV,
                OTEL_SERVICE_NAME_ENV,
            )
            or DEFAULT_OTEL_SERVICE_NAME,
            queue_capacity=_optional_positive_int(
                values.get(ASSISTANT_AGENT_OTEL_EXPORT_QUEUE_CAPACITY_ENV)
            )
            or DEFAULT_EXPORT_QUEUE_CAPACITY,
            include_content=enabled,
            include_vlm_input_content=(
                enabled
                and local_trace_content_enabled(values)
                and _is_loopback_endpoint(endpoint)
            ),
            include_memory_content=(
                enabled
                and local_memory_trace_content_enabled(values)
                and _is_loopback_endpoint(endpoint)
            ),
        )


@dataclass(frozen=True)
class OtlpHttpExporterSetup:
    """Result of constructing an optional OTLP HTTP exporter."""

    status: Literal["disabled", "ready", "unavailable"]
    exporter: "OtlpHttpTextSpanExporter | None" = None
    reason: str | None = None


@dataclass
class _ExportCommand:
    kind: str
    payload: Any = None
    done: Event | None = None
    result: dict[str, Any] = field(default_factory=dict)


class TextOtelSpanExporter(Protocol):
    """Dependency-free exporter boundary for text OTel span specs."""

    def export(self, spans: Sequence[OtelSpanSpec]) -> bool | None:
        """Export one completed text turn span batch."""


class OtlpSpanBridge(Protocol):
    """Internal bridge boundary around the optional OpenTelemetry SDK."""

    def export(self, spans: Sequence[OtelSpanSpec]) -> None:
        """Export one span batch."""

    def flush(self) -> bool:
        """Flush pending spans."""

    def shutdown(self) -> bool:
        """Shutdown exporter resources."""


class OtlpHttpTextSpanExporter:
    """Text span exporter backed by an optional OTLP HTTP OpenTelemetry bridge."""

    def __init__(self, bridge: OtlpSpanBridge) -> None:
        self.bridge = bridge

    def export(self, spans: Sequence[OtelSpanSpec]) -> bool:
        self.bridge.export(spans)
        return True

    def flush(self) -> bool:
        return self.bridge.flush()

    def shutdown(self) -> bool:
        return self.bridge.shutdown()


def create_otlp_http_text_span_exporter(
    config: OtlpHttpTextExporterConfig | None = None,
    *,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> OtlpHttpExporterSetup:
    """Build the optional OTLP HTTP exporter without importing OTel unless enabled."""

    resolved_config = config or OtlpHttpTextExporterConfig.from_env()
    if not resolved_config.enabled:
        return OtlpHttpExporterSetup(status="disabled")
    if not resolved_config.endpoint:
        return OtlpHttpExporterSetup(
            status="unavailable",
            reason="OTLP HTTP exporter endpoint is required",
        )
    try:
        bridge = _OtelSdkSpanBridge(resolved_config, import_module=import_module)
    except ModuleNotFoundError as exc:
        missing_name = exc.name or str(exc)
        return OtlpHttpExporterSetup(
            status="unavailable",
            reason=(
                "OpenTelemetry optional dependencies are not installed; "
                f"install assistant_agent[observability] to enable OTLP export ({missing_name})"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - optional observability must fail open.
        return OtlpHttpExporterSetup(
            status="unavailable",
            reason=_sanitize_exporter_error(exc),
        )
    return OtlpHttpExporterSetup(status="ready", exporter=OtlpHttpTextSpanExporter(bridge))


def create_text_otel_trace_observer_from_env() -> TextOtelTraceObserver | None:
    """Create an enabled text OTel observer when env configuration is ready."""

    config = OtlpHttpTextExporterConfig.from_env()
    return create_text_otel_trace_observer(config)


def create_text_otel_trace_observer(
    config: OtlpHttpTextExporterConfig,
) -> TextOtelTraceObserver | None:
    """Create one text OTel observer from an explicit backend configuration."""

    setup = create_otlp_http_text_span_exporter(config)
    if setup.status != "ready" or setup.exporter is None:
        return None
    return TextOtelTraceObserver(
        BufferedTextOtelSpanExporter(setup.exporter, capacity=config.queue_capacity),
        enabled=True,
        include_content=config.include_content,
        include_vlm_input_content=config.include_vlm_input_content,
        include_memory_content=config.include_memory_content,
    )


class _OtelSdkSpanBridge:
    """Lazy OpenTelemetry SDK adapter for completed dependency-free span specs."""

    def __init__(
        self,
        config: OtlpHttpTextExporterConfig,
        *,
        import_module: Callable[[str], Any] = importlib.import_module,
    ) -> None:
        trace_module = import_module("opentelemetry.trace")
        context_module = import_module("opentelemetry.context")
        resource_module = import_module("opentelemetry.sdk.resources")
        sdk_trace_module = import_module("opentelemetry.sdk.trace")
        sdk_export_module = import_module("opentelemetry.sdk.trace.export")
        otlp_export_module = import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")

        resource = resource_module.Resource.create({"service.name": config.service_name})
        self._id_generator = _SpecSpanIdGenerator()
        provider = sdk_trace_module.TracerProvider(
            resource=resource,
            id_generator=self._id_generator,
        )
        exporter_kwargs: dict[str, Any] = {
            "endpoint": config.endpoint,
            "headers": config.headers,
        }
        if config.timeout_seconds is not None:
            exporter_kwargs["timeout"] = config.timeout_seconds
        exporter = otlp_export_module.OTLPSpanExporter(**exporter_kwargs)
        processor = sdk_export_module.SimpleSpanProcessor(exporter)
        provider.add_span_processor(processor)

        self._trace_module = trace_module
        self._context_module = context_module
        self._provider = provider
        self._tracer = provider.get_tracer(DEFAULT_OTEL_TRACER_NAME)

    def export(self, spans: Sequence[OtelSpanSpec]) -> None:
        contexts_by_span_id: dict[str, Any] = {}
        created_spans: list[tuple[OtelSpanSpec, Any]] = []
        for spec in _parent_first_span_specs(spans):
            if spec.parent_span_id in contexts_by_span_id:
                parent_context = contexts_by_span_id[spec.parent_span_id]
            elif spec.parent_span_id:
                parent_context = _otel_trace_parent_context(
                    self._trace_module,
                    trace_id=langfuse_trace_id(spec.trace_id),
                    parent_span_id=spec.parent_span_id,
                )
            else:
                parent_context = self._context_module.Context()
            with self._id_generator.use_ids(
                span_id=_otel_spec_span_id(spec.span_id),
                trace_id=int(langfuse_trace_id(spec.trace_id), 16),
            ):
                span = self._tracer.start_span(
                    spec.name,
                    context=parent_context,
                    start_time=_datetime_to_epoch_ns(spec.start_time),
                    attributes=_otel_attribute_values(spec.attributes),
                )
            _set_otel_status(self._trace_module, span, spec.status)
            contexts_by_span_id[spec.span_id] = self._trace_module.set_span_in_context(span)
            created_spans.append((spec, span))
        for spec, span in sorted(created_spans, key=lambda item: item[0].end_time):
            span.end(end_time=_datetime_to_epoch_ns(spec.end_time))

    def flush(self) -> bool:
        result = self._provider.force_flush()
        return result is not False

    def shutdown(self) -> bool:
        result = self._provider.shutdown()
        return result is not False


class _SpecSpanIdGenerator:
    """Give the OTel SDK the stable span ID declared by each span spec."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._next_span_id: int | None = None
        self._next_trace_id: int | None = None
        self._generated_trace_id_random = True

    @contextmanager
    def use_ids(self, *, span_id: int, trace_id: int):
        with self._lock:
            self._next_span_id = span_id
            self._next_trace_id = trace_id
            try:
                yield
            finally:
                self._next_span_id = None
                self._next_trace_id = None

    def generate_span_id(self) -> int:
        span_id = self._next_span_id
        self._next_span_id = None
        return span_id or _random_nonzero_id(bits=64)

    def generate_trace_id(self) -> int:
        trace_id = self._next_trace_id
        self._next_trace_id = None
        self._generated_trace_id_random = trace_id is None
        return trace_id or _random_nonzero_id(bits=128)

    def is_trace_id_random(self) -> bool:
        return self._generated_trace_id_random


def _parent_first_span_specs(spans: Sequence[OtelSpanSpec]) -> list[OtelSpanSpec]:
    """Order one export batch so an in-batch parent is created before its child."""

    remaining = list(spans)
    batch_span_ids = {spec.span_id for spec in remaining}
    created_span_ids: set[str] = set()
    ordered: list[OtelSpanSpec] = []
    while remaining:
        ready = [
            spec
            for spec in remaining
            if spec.parent_span_id is None
            or spec.parent_span_id not in batch_span_ids
            or spec.parent_span_id in created_span_ids
        ]
        if not ready:
            ordered.extend(remaining)
            break
        for spec in ready:
            ordered.append(spec)
            created_span_ids.add(spec.span_id)
            remaining.remove(spec)
    return ordered


class BufferedTextOtelSpanExporter:
    """Send span batches through a bounded background queue."""

    def __init__(
        self,
        sink: TextOtelSpanExporter,
        *,
        capacity: int = DEFAULT_EXPORT_QUEUE_CAPACITY,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.sink = sink
        self.capacity = capacity
        self._queue: Queue[_ExportCommand] = Queue(maxsize=capacity)
        self._state_lock = Lock()
        self._close_lock = Lock()
        self._dropped_batch_count = 0
        self._error_count = 0
        self._last_error: str | None = None
        self._closing = False
        self._closed = False
        self._close_result: bool | None = None
        self._worker = Thread(
            target=self._run,
            name="assistant-agent-otel-exporter",
            daemon=True,
        )
        self._worker.start()

    @property
    def dropped_batch_count(self) -> int:
        with self._state_lock:
            return self._dropped_batch_count

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

    def export(self, spans: Sequence[OtelSpanSpec]) -> bool:
        with self._state_lock:
            closing = self._closing or self._closed
        if closing:
            self._record_drop()
            return False
        try:
            self._queue.put_nowait(_ExportCommand("export", list(spans)))
        except Full:
            self._record_drop()
            return False
        return True

    def flush(self, *, timeout: float = DEFAULT_EXPORT_CLOSE_TIMEOUT_SECONDS) -> bool:
        if timeout <= 0:
            return False
        with self._state_lock:
            if self._closed:
                return not self._worker.is_alive()
        command = _ExportCommand("flush", done=Event())
        deadline = monotonic() + timeout
        if not self._enqueue_control(command, timeout=timeout):
            return False
        completed = command.done.wait(timeout=max(0.0, deadline - monotonic()))
        return completed and "error" not in command.result

    def close(self, *, timeout: float = DEFAULT_EXPORT_CLOSE_TIMEOUT_SECONDS) -> bool:
        if timeout <= 0:
            return False
        with self._close_lock:
            with self._state_lock:
                if self._closed:
                    return self._close_result is True
                self._closing = True
            deadline = monotonic() + timeout
            flushed = self.flush(timeout=max(0.0, deadline - monotonic()))
            stop_command = _ExportCommand("stop", done=Event())
            stop_enqueued = self._enqueue_control(
                stop_command,
                timeout=max(0.0, deadline - monotonic()),
            )
            stop_completed = False
            if stop_enqueued:
                stop_completed = stop_command.done.wait(
                    timeout=max(0.0, deadline - monotonic())
                )
                self._worker.join(timeout=max(0.0, deadline - monotonic()))
            with self._state_lock:
                self._closed = not self._worker.is_alive()
                self._close_result = (
                    flushed
                    and stop_completed
                    and "error" not in stop_command.result
                    and self._closed
                )
                return self._close_result

    def shutdown(self, *, timeout: float = DEFAULT_EXPORT_CLOSE_TIMEOUT_SECONDS) -> bool:
        return self.close(timeout=timeout)

    def _run(self) -> None:
        while True:
            try:
                command = self._queue.get(timeout=0.25)
            except Empty:
                continue
            should_stop = command.kind == "stop"
            try:
                if command.kind == "export":
                    self.sink.export(command.payload)
                elif command.kind == "flush":
                    if self._flush_sink() is False:
                        raise RuntimeError("OTel exporter sink flush returned false")
                elif command.kind == "stop":
                    if self._close_sink() is False:
                        raise RuntimeError("OTel exporter sink close returned false")
            except Exception as exc:
                safe_error = _sanitize_exporter_error(exc)
                command.result["error"] = safe_error
                self._record_error(safe_error)
            finally:
                if command.done is not None:
                    command.done.set()
                self._queue.task_done()
            if should_stop:
                return

    def _enqueue_control(self, command: _ExportCommand, *, timeout: float) -> bool:
        if timeout <= 0:
            return False
        try:
            self._queue.put(command, timeout=timeout)
        except Full:
            return False
        return True

    def _flush_sink(self) -> bool | None:
        method = getattr(self.sink, "flush", None)
        if callable(method):
            return method()
        return True

    def _close_sink(self) -> bool | None:
        method = getattr(self.sink, "shutdown", None)
        if not callable(method):
            method = getattr(self.sink, "close", None)
        if callable(method):
            return method()
        return True

    def _record_drop(self) -> None:
        with self._state_lock:
            self._dropped_batch_count += 1

    def _record_error(self, safe_error: str) -> None:
        with self._state_lock:
            self._error_count += 1
            self._last_error = safe_error


class TextOtelTraceObserver:
    """Hook observer that exports one text OTel span batch per completed turn."""

    def __init__(
        self,
        exporter: TextOtelSpanExporter,
        *,
        enabled: bool = False,
        max_buffered_runs: int = DEFAULT_MAX_BUFFERED_RUNS,
        max_events_per_run: int = DEFAULT_MAX_EVENTS_PER_RUN,
        continue_on_error: bool = True,
        include_content: bool = False,
        include_vlm_input_content: bool = False,
        include_memory_content: bool = False,
    ) -> None:
        if max_buffered_runs <= 0:
            raise ValueError("max_buffered_runs must be positive")
        if max_events_per_run <= 0:
            raise ValueError("max_events_per_run must be positive")
        self.exporter = exporter
        self.enabled = enabled
        self.max_buffered_runs = max_buffered_runs
        self.max_events_per_run = max_events_per_run
        self.continue_on_error = continue_on_error
        self.include_content = include_content
        self.include_vlm_input_content = include_vlm_input_content
        self.include_memory_content = include_memory_content
        self._lock = Lock()
        self._events_by_run: dict[str, list[TraceEvent]] = {}
        self._exporting_run_ids: set[str] = set()
        self._exported_run_ids: set[str] = set()
        self._dropped_run_ids: set[str] = set()
        self._pending_late_events: dict[str, list[TraceEvent]] = {}
        self._projection_context_by_run: dict[
            str, OtelTraceProjectionContext
        ] = {}
        self._runtime_root_span_id_by_run: dict[str, str] = {}
        self._projection_required_run_ids: set[str] = set()
        self._suppressed_run_ids: set[str] = set()
        self._errors: list[str] = []
        self._exported_run_count = 0
        self._dropped_run_count = 0
        self._dropped_event_count = 0

    @property
    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    @property
    def error_count(self) -> int:
        with self._lock:
            return len(self._errors)

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._errors[-1] if self._errors else None

    @property
    def exported_run_count(self) -> int:
        with self._lock:
            return self._exported_run_count

    @property
    def dropped_run_count(self) -> int:
        with self._lock:
            return self._dropped_run_count

    @property
    def dropped_event_count(self) -> int:
        with self._lock:
            return self._dropped_event_count

    @property
    def buffered_run_count(self) -> int:
        with self._lock:
            return len(self._events_by_run)

    def on_trace_event(self, event: TraceEvent) -> None:
        """Receive one trace event and export when the turn summary arrives."""

        if not self.enabled:
            return
        exported_event = event if self.include_content else redact_trace_event(event)
        route, events_to_export = self._buffer_event(exported_event)
        if route == "late_event":
            self._export_late_event(exported_event)
        elif route == "batch" and events_to_export is not None:
            self._export_events(events_to_export)
            for pending_event in self._complete_run_export(exported_event.run_id):
                self._export_late_event(pending_event)

    def _export_late_event(self, event: TraceEvent) -> None:
        try:
            with self._lock:
                if event.run_id in self._suppressed_run_ids:
                    return
                projection_context = self._projection_context_by_run.get(
                    event.run_id
                )
                runtime_parent_span_id = self._runtime_root_span_id_by_run.get(
                    event.run_id
                )
                projection_required = (
                    event.run_id in self._projection_required_run_ids
                )
            if projection_required and projection_context is None:
                return
            memory_content = None
            if (
                self.include_memory_content
                and event.canonical_event == "memory.ingestion.finished"
            ):
                from assistant_agent.memory.trace_content import (
                    get_default_memory_trace_content_store,
                )

                memory_content = get_default_memory_trace_content_store().get(
                    trace_id=event.trace_id,
                    run_id=event.run_id,
                )
            span = build_late_text_otel_span_spec(
                event,
                memory_content=memory_content,
                projection_context=projection_context,
                runtime_parent_span_id=runtime_parent_span_id,
            )
            self.exporter.export([span])
        except Exception as exc:
            self._record_error(exc)
            if not self.continue_on_error:
                raise

    def flush(self, *, timeout: float = DEFAULT_EXPORT_CLOSE_TIMEOUT_SECONDS) -> bool:
        """Flush the underlying exporter when it supports flush."""

        return self._call_exporter_lifecycle("flush", timeout=timeout)

    def close(self, *, timeout: float = DEFAULT_EXPORT_CLOSE_TIMEOUT_SECONDS) -> bool:
        """Shutdown or close the underlying exporter when supported."""

        if hasattr(self.exporter, "shutdown"):
            return self._call_exporter_lifecycle("shutdown", timeout=timeout)
        return self._call_exporter_lifecycle("close", timeout=timeout)

    def _buffer_event(
        self,
        event: TraceEvent,
    ) -> tuple[Literal["buffered", "batch", "late_event", "ignored"], list[TraceEvent] | None]:
        with self._lock:
            run_id = event.run_id
            if run_id in self._dropped_run_ids:
                return "ignored", None
            if run_id in self._exporting_run_ids:
                if _is_late_exportable_event(event):
                    pending = self._pending_late_events.setdefault(run_id, [])
                    if len(pending) >= self.max_events_per_run:
                        pending.pop(0)
                        self._dropped_event_count += 1
                    pending.append(event)
                return "buffered", None
            if run_id in self._exported_run_ids:
                if _is_late_exportable_event(event):
                    return "late_event", None
                return "ignored", None
            if run_id not in self._events_by_run:
                self._ensure_run_capacity_locked()
                self._events_by_run[run_id] = []
            events = self._events_by_run[run_id]
            if len(events) >= self.max_events_per_run:
                events.pop(0)
                self._dropped_event_count += 1
            events.append(event)
            if not _is_batch_terminal_event(event):
                return "buffered", None
            events_to_export = list(events)
            del self._events_by_run[run_id]
            self._exporting_run_ids.add(run_id)
            return "batch", events_to_export

    def _complete_run_export(self, run_id: str) -> list[TraceEvent]:
        with self._lock:
            self._exporting_run_ids.discard(run_id)
            self._exported_run_ids.add(run_id)
            return self._pending_late_events.pop(run_id, [])

    def _ensure_run_capacity_locked(self) -> None:
        while len(self._events_by_run) >= self.max_buffered_runs:
            dropped_run_id = next(iter(self._events_by_run))
            del self._events_by_run[dropped_run_id]
            self._dropped_run_ids.add(dropped_run_id)
            self._dropped_run_count += 1

    def _export_events(self, events: list[TraceEvent]) -> None:
        projection_context = text_otel_projection_context(events)
        if projection_context is not None:
            with self._lock:
                self._projection_required_run_ids.add(events[0].run_id)
        try:
            conversation = self._trace_conversation(events) if self.include_content else None
            memory_content = (
                self._memory_content(events)
                if self.include_memory_content
                else None
            )
            spans = build_text_otel_span_specs(
                events,
                conversation=conversation,
                memory_content=memory_content,
            )
            if not spans:
                with self._lock:
                    self._suppressed_run_ids.add(events[0].run_id)
                return
            if spans:
                accepted = self.exporter.export(spans)
                if accepted is False:
                    return
        except Exception as exc:
            self._record_error(exc)
            if not self.continue_on_error:
                raise
            return
        with self._lock:
            self._runtime_root_span_id_by_run[events[0].run_id] = spans[0].span_id
            if projection_context is not None:
                self._projection_context_by_run[events[0].run_id] = (
                    projection_context
                )
            self._exported_run_count += 1

    def _trace_conversation(self, events: list[TraceEvent]):
        identity = next(
            (event for event in events if event.user_id and event.session_id and event.trace_id),
            None,
        )
        if identity is None:
            return None
        from assistant_agent.observability.trace_conversation import get_default_trace_conversation_store

        return get_default_trace_conversation_store().get(
            user_id=str(identity.user_id),
            session_id=str(identity.session_id),
            trace_id=identity.trace_id,
            limit=4000,
            include_llm_inputs=True,
            include_llm_outputs=True,
            include_vlm_inputs=self.include_vlm_input_content,
            include_vlm_outputs=True,
            include_tool_observations=True,
            include_tool_results=True,
        )

    @staticmethod
    def _memory_content(events: list[TraceEvent]):
        event = next(
            (
                item
                for item in events
                if item.canonical_event == "memory.ingestion.finished"
            ),
            None,
        )
        if event is None:
            return None
        from assistant_agent.memory.trace_content import (
            get_default_memory_trace_content_store,
        )

        return get_default_memory_trace_content_store().get(
            trace_id=event.trace_id,
            run_id=event.run_id,
        )

    def _call_exporter_lifecycle(self, method_name: str, *, timeout: float) -> bool:
        method = getattr(self.exporter, method_name, None)
        if not callable(method):
            return True
        try:
            result = method(timeout=timeout) if _accepts_timeout(method) else method()
        except Exception as exc:
            self._record_error(exc)
            if not self.continue_on_error:
                raise
            return False
        return result is not False

    def _record_error(self, exc: BaseException) -> None:
        safe_error = _sanitize_exporter_error(exc)
        with self._lock:
            self._errors.append(safe_error)


def _truthy_env_value(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY_ENV_VALUES


def _is_late_exportable_event(event: TraceEvent) -> bool:
    canonical_event = event.canonical_event or ""
    return (
        canonical_event == "memory.ingestion.finished"
        or canonical_event == "response.delivered"
        or canonical_event.startswith("visual_reminder.")
    )


def _is_batch_terminal_event(event: TraceEvent) -> bool:
    return event.canonical_event in {
        ASSISTANT_TURN_SUMMARY_EVENT,
        "vision.observation.summary",
    }


def _is_loopback_endpoint(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    host = urlparse(endpoint).hostname
    return host in {"localhost", "127.0.0.1", "::1"}


def _accepts_timeout(method: Callable[..., object]) -> bool:
    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in parameters or any(
        parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _first_non_empty(values: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


def _trace_endpoint_from_env(values: Mapping[str, str]) -> str | None:
    trace_endpoint = _first_non_empty(
        values,
        ASSISTANT_AGENT_OTEL_EXPORT_ENDPOINT_ENV,
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT_ENV,
    )
    if trace_endpoint is not None:
        return trace_endpoint
    generic_endpoint = _first_non_empty(values, OTEL_EXPORTER_OTLP_ENDPOINT_ENV)
    if generic_endpoint is None:
        return default_langfuse_trace_endpoint(values)
    if generic_endpoint.rstrip("/").endswith("/v1/traces"):
        return generic_endpoint
    return f"{generic_endpoint.rstrip('/')}/v1/traces"


def _parse_otlp_headers(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    headers: dict[str, str] = {}
    for part in value.split(","):
        key, separator, header_value = part.partition("=")
        if not separator:
            continue
        header_name = key.strip()
        if not header_name:
            continue
        headers[header_name] = header_value.strip()
    return headers


def _optional_positive_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _optional_positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _datetime_to_epoch_ns(value: Any) -> int:
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1_000_000_000)


def _otel_spec_span_id(value: str) -> int:
    if len(value) == 16 and value == value.lower():
        try:
            parsed = int(value, 16)
        except ValueError:
            parsed = 0
        if parsed != 0:
            return parsed
    parsed = int.from_bytes(sha256(value.encode("utf-8")).digest()[:8], "big")
    return parsed or 1


def _random_nonzero_id(*, bits: int) -> int:
    value = 0
    while value == 0:
        value = secrets.randbits(bits)
    return value


def _otel_attribute_values(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in attributes.items()
        if _otel_attribute_value(value)
    }


def _otel_attribute_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int | float):
        return True
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return all(_otel_attribute_value(item) for item in value)
    return False


def _set_otel_status(trace_module: Any, span: Any, status: str) -> None:
    if status == "unset":
        return
    status_class = getattr(trace_module, "Status", None)
    status_code = getattr(trace_module, "StatusCode", None)
    if status_class is None or status_code is None:
        return
    code = status_code.ERROR if status == "error" else status_code.OK
    span.set_status(status_class(code))


def _otel_trace_parent_context(
    trace_module: Any,
    *,
    trace_id: str,
    parent_span_id: str | None = None,
) -> Any:
    span_context = trace_module.SpanContext(
        trace_id=int(trace_id, 16),
        span_id=int(parent_span_id, 16) if parent_span_id else 1,
        is_remote=True,
        trace_flags=trace_module.TraceFlags(1),
        trace_state=trace_module.TraceState(),
    )
    return trace_module.set_span_in_context(trace_module.NonRecordingSpan(span_context))


def _sanitize_exporter_error(value: object) -> str:
    safe_error = sanitize_error_message(value)
    return _RAW_CONTENT_ASSIGNMENT_RE.sub(_redact_content_assignment, safe_error)


def _redact_content_assignment(match: re.Match[str]) -> str:
    return match.group(0).split(match.group(1), 1)[0] + "[redacted]"
