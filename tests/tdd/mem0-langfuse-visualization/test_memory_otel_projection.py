from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Lock
from types import SimpleNamespace

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from assistant_agent.memory.mem0.models import Mem0MemoryChange
from assistant_agent.memory.trace_content import (
    MemoryIngestionTraceContent,
    get_default_memory_trace_content_store,
)
from assistant_agent.observability.otel_exporter import (
    OtlpHttpTextExporterConfig,
    TextOtelTraceObserver,
    _OtelSdkSpanBridge,
)
from assistant_agent.observability.otel_mapping import (
    build_late_text_otel_span_spec,
    build_text_otel_span_specs,
)
from assistant_agent.observability.trace_store import TraceEvent
from assistant_agent.observability.turn_summary import ASSISTANT_TURN_SUMMARY_EVENT


class RecordingExporter:
    def __init__(self) -> None:
        self.batches = []

    def export(self, spans) -> None:
        self.batches.append(list(spans))


def _summary_event() -> TraceEvent:
    return TraceEvent(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
        user_id="user-sentinel",
        session_id="session-sentinel",
        node_name="runtime",
        event_type="observability",
        canonical_event=ASSISTANT_TURN_SUMMARY_EVENT,
        status="completed",
        output_summary={
            "turn_summary": {
                "trace_id": "trace-sentinel",
                "run_id": "run-sentinel",
                "user_id": "user-sentinel",
                "session_id": "session-sentinel",
                "terminal_status": "completed",
            }
        },
        created_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
    )


def _memory_event() -> TraceEvent:
    summary = {
        "memory_count": 1,
        "change_counts": {"ADD": 1},
        "memory_ids": ["memory-sentinel"],
        "source_turn": "source-turn-sentinel",
        "errors": [],
    }
    return TraceEvent(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
        user_id="user-sentinel",
        session_id="session-sentinel",
        node_name="post_response_memory_ingestion",
        event_type="observability",
        canonical_event="memory.ingestion.finished",
        observation_type="span",
        observation_name="memory.turn_ingestion",
        status="succeeded",
        latency_ms=1200,
        span_id="memory-span-sentinel",
        attributes=summary,
        output_summary=summary,
        created_at=datetime(2026, 8, 4, 8, 0, 2, tzinfo=timezone.utc),
    )


def test_observer_exports_late_memory_span_without_reexporting_root() -> None:
    exporter = RecordingExporter()
    observer = TextOtelTraceObserver(exporter, enabled=True)

    observer.on_trace_event(_summary_event())
    observer.on_trace_event(_memory_event())

    assert len(exporter.batches) == 2
    assert len(exporter.batches[1]) == 1
    span = exporter.batches[1][0]
    assert span.name == "memory.turn_ingestion"
    assert span.trace_id == "trace-sentinel"
    assert span.parent_span_id == "c41c7edb748a4749"
    assert span.attributes["langfuse.session.id"] == "session-sentinel"
    output = json.loads(span.attributes["langfuse.observation.output"])
    assert output["memory_ids"] == ["memory-sentinel"]
    assert output["change_counts"] == {"ADD": 1}
    assert output["content_exported"] is False
    assert "changes" not in output


def test_late_memory_span_includes_overlay_when_explicitly_allowed() -> None:
    get_default_memory_trace_content_store().put(
        MemoryIngestionTraceContent(
            trace_id="trace-sentinel",
            run_id="run-sentinel",
            user_id="user-sentinel",
            session_id="session-sentinel",
            source_turn="source-turn-sentinel",
            user_text="我更喜欢用中文交流。",
            assistant_text="好的，我会优先使用中文。",
            changes=[
                Mem0MemoryChange(
                    memory_id="memory-sentinel",
                    memory="用户偏好使用中文",
                    event="ADD",
                )
            ],
        )
    )
    exporter = RecordingExporter()
    observer = TextOtelTraceObserver(
        exporter,
        enabled=True,
        include_memory_content=True,
    )

    observer.on_trace_event(_summary_event())
    observer.on_trace_event(_memory_event())

    output = json.loads(
        exporter.batches[1][0].attributes["langfuse.observation.output"]
    )
    input_payload = json.loads(
        exporter.batches[1][0].attributes["langfuse.observation.input"]
    )
    assert input_payload["messages"] == [
        {"role": "user", "content": "我更喜欢用中文交流。"},
        {"role": "assistant", "content": "好的，我会优先使用中文。"},
    ]
    assert output["content_exported"] is True
    assert output["changes"] == [
        {
            "memory_id": "memory-sentinel",
            "memory": "用户偏好使用中文",
            "event": "ADD",
        }
    ]


def test_memory_content_is_exported_when_ingestion_finishes_before_summary() -> None:
    get_default_memory_trace_content_store().put(
        MemoryIngestionTraceContent(
            trace_id="trace-sentinel",
            run_id="run-sentinel",
            user_id="user-sentinel",
            session_id="session-sentinel",
            source_turn="source-turn-sentinel",
            changes=[
                Mem0MemoryChange(
                    memory_id="memory-sentinel",
                    memory="用户偏好使用中文",
                    event="ADD",
                )
            ],
        )
    )
    exporter = RecordingExporter()
    observer = TextOtelTraceObserver(
        exporter,
        enabled=True,
        include_memory_content=True,
    )

    observer.on_trace_event(_memory_event())
    observer.on_trace_event(_summary_event())

    memory_span = next(
        span
        for span in exporter.batches[0]
        if span.name == "memory.turn_ingestion"
    )
    output = json.loads(
        memory_span.attributes["langfuse.observation.output"]
    )
    assert output["content_exported"] is True
    assert output["changes"][0]["memory"] == "用户偏好使用中文"


def test_concurrent_summary_cannot_drop_memory_completion() -> None:
    class BlockingRootExporter:
        def __init__(self) -> None:
            self.root_started = Event()
            self.release_root = Event()
            self.lock = Lock()
            self.batches = []

        def export(self, spans) -> None:
            batch = list(spans)
            if any(span.name == "agent.runtime" for span in batch):
                self.root_started.set()
                assert self.release_root.wait(timeout=1.0)
            with self.lock:
                self.batches.append(batch)

    exporter = BlockingRootExporter()
    observer = TextOtelTraceObserver(exporter, enabled=True)

    with ThreadPoolExecutor(max_workers=1) as executor:
        summary = executor.submit(observer.on_trace_event, _summary_event())
        assert exporter.root_started.wait(timeout=1.0)
        observer.on_trace_event(_memory_event())
        assert exporter.batches == []
        exporter.release_root.set()
        summary.result(timeout=2.0)

    assert len(exporter.batches) == 2
    assert exporter.batches[0][0].name == "agent.runtime"
    assert exporter.batches[1][0].name == "memory.turn_ingestion"


def test_memory_content_export_requires_explicit_flag_and_loopback_endpoint() -> None:
    base = {
        "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "true",
        "MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT": "1",
    }

    local = OtlpHttpTextExporterConfig.from_env(
        {
            **base,
            "ASSISTANT_AGENT_OTEL_EXPORT_ENDPOINT": (
                "http://127.0.0.1:3000/api/public/otel/v1/traces"
            ),
        }
    )
    remote = OtlpHttpTextExporterConfig.from_env(
        {
            **base,
            "ASSISTANT_AGENT_OTEL_EXPORT_ENDPOINT": (
                "https://langfuse.example/api/public/otel/v1/traces"
            ),
        }
    )
    disabled = OtlpHttpTextExporterConfig.from_env(
        {
            "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "true",
            "ASSISTANT_AGENT_OTEL_EXPORT_ENDPOINT": (
                "http://127.0.0.1:3000/api/public/otel/v1/traces"
            ),
        }
    )

    assert local.include_memory_content is True
    assert remote.include_memory_content is False
    assert disabled.include_memory_content is False


def test_sdk_late_span_parent_matches_exported_runtime_root() -> None:
    memory_exporter = InMemorySpanExporter()

    def import_for_bridge(name: str):
        if name == "opentelemetry.exporter.otlp.proto.http.trace_exporter":
            return SimpleNamespace(
                OTLPSpanExporter=lambda **kwargs: memory_exporter,
            )
        return importlib.import_module(name)

    bridge = _OtelSdkSpanBridge(
        OtlpHttpTextExporterConfig(
            enabled=True,
            endpoint="http://127.0.0.1:3000/api/public/otel/v1/traces",
        ),
        import_module=import_for_bridge,
    )
    bridge.export(build_text_otel_span_specs([_summary_event()]))
    bridge.export([build_late_text_otel_span_spec(_memory_event())])

    spans_by_name = {
        span.name: span for span in memory_exporter.get_finished_spans()
    }
    runtime = spans_by_name["agent.runtime"]
    memory = spans_by_name["memory.turn_ingestion"]
    assert memory.parent is not None
    assert memory.context.trace_id == runtime.context.trace_id
    assert memory.parent.span_id == runtime.context.span_id
