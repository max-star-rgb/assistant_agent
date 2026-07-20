"""Offline smoke for text trace observers and Langfuse-compatible span output."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.hooks import HookManager, HookTraceStore
from assistant_agent.services.langfuse_scores import LangfuseScoreTraceObserver
from assistant_agent.services.otel_exporter import TextOtelTraceObserver
from assistant_agent.services.trace_store import CompositeTraceStore, InMemoryTraceStore, TraceEvent
from assistant_agent.services.turn_summary import ASSISTANT_TURN_SUMMARY_EVENT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one offline text turn and verify the trace observer chain "
            "without sending data to an external OTLP or Langfuse endpoint."
        )
    )
    parser.add_argument("--text", default="你好", help="Text prompt for the mock turn.")
    parser.add_argument("--user-id", default="smoke_user", help="Local smoke user id.")
    parser.add_argument("--session-id", default="smoke_session", help="Local smoke session id.")
    parser.add_argument(
        "--inject-degraded-evaluation",
        action="store_true",
        help="Inject prompt-safe degraded task metadata before turn summary to exercise score export.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    span_exporter = RecordingSpanExporter()
    score_writer = RecordingScoreWriter()
    primary = InMemoryTraceStore()
    observer_store = _observer_trace_store(
        span_exporter=span_exporter,
        score_writer=score_writer,
        inject_degraded_evaluation=args.inject_degraded_evaluation,
    )
    trace_store = CompositeTraceStore(primary, [observer_store])

    state = AgentGraphRuntime(trace_store=trace_store).run_state(
        UserRequest(
            user_id=args.user_id,
            session_id=args.session_id,
            text=args.text,
            metadata={"source": "text_observability_smoke"},
        )
    )
    trace_store.close(timeout=1.0)
    events = primary.list_by_run(state.run_id)
    spans = [span for batch in span_exporter.batches for span in batch]
    scores = [score for batch in score_writer.batches for score in batch]

    output = {
        "status": state.status,
        "run_id": state.run_id,
        "trace_id": state.trace_id,
        "response_present": state.response is not None,
        "trace_event_count": len(events),
        "otel": {
            "batch_count": len(span_exporter.batches),
            "span_count": len(spans),
            "span_names": [span.name for span in spans],
            "root_attributes": _root_attributes(spans),
        },
        "langfuse_scores": {
            "batch_count": len(score_writer.batches),
            "score_count": len(scores),
            "score_names": [score.name for score in scores],
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0 if state.status != "failed" and spans else 1


class RecordingSpanExporter:
    """In-memory TextOtelSpanExporter used by the smoke script."""

    def __init__(self) -> None:
        self.batches: list[list[Any]] = []

    def export(self, spans: Sequence[Any]) -> None:
        self.batches.append(list(spans))

    def flush(self) -> bool:
        return True

    def shutdown(self) -> bool:
        return True


class RecordingScoreWriter:
    """In-memory LangfuseScoreWriter used by the smoke script."""

    def __init__(self) -> None:
        self.batches: list[list[Any]] = []

    def write_scores(self, scores: Sequence[Any]) -> None:
        self.batches.append(list(scores))

    def flush(self) -> bool:
        return True

    def shutdown(self) -> bool:
        return True


class EvaluationInjectingTraceStore(HookTraceStore):
    """Inject one structured evaluation before the terminal turn summary."""

    def __init__(self, manager: HookManager, *, enabled: bool) -> None:
        super().__init__(manager)
        self.enabled = enabled
        self.injected_run_ids: set[str] = set()

    def append(self, event: TraceEvent) -> None:
        if (
            self.enabled
            and event.canonical_event == ASSISTANT_TURN_SUMMARY_EVENT
            and event.run_id not in self.injected_run_ids
        ):
            self.injected_run_ids.add(event.run_id)
            self.manager.on_trace_event(_degraded_evaluation_event(event))
        self.manager.on_trace_event(event)


def _observer_trace_store(
    *,
    span_exporter: RecordingSpanExporter,
    score_writer: RecordingScoreWriter,
    inject_degraded_evaluation: bool,
) -> HookTraceStore:
    manager = HookManager(
        [
            TextOtelTraceObserver(span_exporter, enabled=True),
            LangfuseScoreTraceObserver(score_writer, enabled=True),
        ]
    )
    return EvaluationInjectingTraceStore(manager, enabled=inject_degraded_evaluation)


def _degraded_evaluation_event(summary_event: TraceEvent) -> TraceEvent:
    return TraceEvent(
        trace_id=summary_event.trace_id,
        run_id=summary_event.run_id,
        user_id=summary_event.user_id,
        session_id=summary_event.session_id,
        node_name="text_observability_smoke",
        event_type="observability",
        canonical_event="turn.evaluation",
        status="completed",
        attributes={
            "task_outcome": "degraded",
            "prerequisites": ["location"],
            "unresolved_prerequisites": ["location"],
            "clarification_too_late": True,
        },
        created_at=datetime.now(timezone.utc),
    )


def _root_attributes(spans: list[Any]) -> dict[str, Any]:
    if not spans:
        return {}
    root = spans[0]
    attributes = getattr(root, "attributes", {}) or {}
    keys = (
        "langfuse.trace.name",
        "assistant_agent.modality",
        "assistant_agent.terminal_status",
        "langfuse.trace.metadata.task_outcome",
        "langfuse.trace.metadata.execution_status",
    )
    return {key: attributes[key] for key in keys if key in attributes}


if __name__ == "__main__":
    raise SystemExit(main())
