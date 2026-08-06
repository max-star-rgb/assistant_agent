from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from assistant_agent.observability.otel_exporter import (
    OtlpHttpTextExporterConfig,
    _OtelSdkSpanBridge,
)
from assistant_agent.observability.otel_mapping import OtelSpanSpec


def _bridge(memory_exporter: InMemorySpanExporter) -> _OtelSdkSpanBridge:
    def import_for_bridge(name: str):
        if name == "opentelemetry.exporter.otlp.proto.http.trace_exporter":
            return SimpleNamespace(OTLPSpanExporter=lambda **kwargs: memory_exporter)
        return importlib.import_module(name)

    return _OtelSdkSpanBridge(
        OtlpHttpTextExporterConfig(
            enabled=True,
            endpoint="http://127.0.0.1:3000/api/public/otel/v1/traces",
        ),
        import_module=import_for_bridge,
    )


def _spec(
    *,
    name: str,
    span_id: str,
    parent_span_id: str | None,
    offset_ms: int,
) -> OtelSpanSpec:
    started_at = datetime(2026, 8, 6, 3, 21, 31, tzinfo=timezone.utc)
    return OtelSpanSpec(
        trace_id="a" * 32,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=name,
        start_time=started_at + timedelta(milliseconds=offset_ms),
        end_time=started_at + timedelta(milliseconds=offset_ms + 10),
        status="ok",
    )


def test_sdk_bridge_exports_a_true_root_without_a_synthetic_parent() -> None:
    memory_exporter = InMemorySpanExporter()
    bridge = _bridge(memory_exporter)
    ambient = otel_trace.set_span_in_context(
        otel_trace.NonRecordingSpan(
            otel_trace.SpanContext(
                trace_id=int("b" * 32, 16),
                span_id=int("9" * 16, 16),
                is_remote=False,
                trace_flags=otel_trace.TraceFlags(1),
                trace_state=otel_trace.TraceState(),
            )
        )
    )
    token = otel_context.attach(ambient)
    try:
        bridge.export(
            [
                _spec(
                    name="vision.runtime",
                    span_id="1" * 16,
                    parent_span_id=None,
                    offset_ms=0,
                )
            ]
        )
    finally:
        otel_context.detach(token)

    [root] = memory_exporter.get_finished_spans()
    assert root.parent is None


def test_sdk_bridge_resolves_parent_even_when_child_spec_comes_first() -> None:
    memory_exporter = InMemorySpanExporter()
    bridge = _bridge(memory_exporter)
    root_id = "1" * 16
    tool_id = "2" * 16
    vlm_id = "3" * 16

    bridge.export(
        [
            _spec(
                name="vision.runtime",
                span_id=root_id,
                parent_span_id=None,
                offset_ms=0,
            ),
            _spec(
                name="vlm.infer",
                span_id=vlm_id,
                parent_span_id=tool_id,
                offset_ms=1,
            ),
            _spec(
                name="realtime_video_observe",
                span_id=tool_id,
                parent_span_id=root_id,
                offset_ms=2,
            ),
        ]
    )

    spans = {span.name: span for span in memory_exporter.get_finished_spans()}
    assert spans["vlm.infer"].parent is not None
    assert spans["vlm.infer"].parent.span_id == spans[
        "realtime_video_observe"
    ].context.span_id
