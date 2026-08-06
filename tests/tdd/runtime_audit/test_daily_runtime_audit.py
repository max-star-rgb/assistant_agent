from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_agent.observability.runtime_audit.cli import _parser
from assistant_agent.observability.runtime_audit.collector import collect_runtime_audit
from assistant_agent.observability.runtime_audit.daily_window import (
    pending_audit_dates,
    previous_day_window,
    window_for_date,
)
from assistant_agent.observability.runtime_audit.langfuse_source import LangfuseSdkAuditSource
from assistant_agent.observability.runtime_audit.models import LangfuseTraceSnapshot


def test_previous_day_uses_shanghai_calendar_boundaries() -> None:
    """Would fail if daily audits derived boundaries from UTC rather than Shanghai dates."""

    window = previous_day_window(datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc))

    assert window.audit_date == date(2026, 8, 5)
    assert window.start_utc == datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    assert window.end_utc == datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def test_explicit_date_uses_shanghai_calendar_boundaries() -> None:
    """Would fail if explicit dates did not use the same Shanghai day boundaries."""

    window = window_for_date(date(2026, 8, 5))

    assert window.audit_date == date(2026, 8, 5)
    assert window.start_utc == datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    assert window.end_utc == datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def test_pending_days_backfill_without_historical_first_run() -> None:
    """Would fail if first runs or missed dates selected an incorrect audit range."""

    assert pending_audit_dates(
        yesterday=date(2026, 8, 5), last_completed=None
    ) == [date(2026, 8, 5)]
    assert pending_audit_dates(
        yesterday=date(2026, 8, 5),
        last_completed=date(2026, 8, 2),
    ) == [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]


def test_run_defaults_to_previous_calendar_day_and_date_conflicts_with_window_hours() -> None:
    """Would fail if run retained a rolling default or accepted competing window choices."""

    parser = _parser()
    args = parser.parse_args(["run"])

    assert args.date is None
    assert args.window_hours is None
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--date", "2026-08-05", "--window-hours", "2"])


def test_collection_excludes_window_end_from_remote_and_local_evidence(tmp_path: Path) -> None:
    """Would fail if the midnight record were counted in both adjacent daily audits."""

    window_start = datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=1)
    local_trace_path = tmp_path / "graph_trace.jsonl"
    local_trace_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "trace_id": trace_id,
                    "run_id": f"run-{trace_id}",
                    "node_name": "runtime",
                    "event_type": "observability",
                    "canonical_event": "run.completed",
                    "status": "completed",
                    "created_at": created_at.isoformat(),
                }
            )
            for trace_id, created_at in (
                ("local-start", window_start),
                ("local-end", window_end),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    class Source:
        def list_traces(self, **_: datetime) -> list[LangfuseTraceSnapshot]:
            return [
                LangfuseTraceSnapshot(
                    trace_id="remote-start",
                    name="assistant.turn",
                    timestamp=window_start,
                    observations=[],
                    scores=[],
                ),
                LangfuseTraceSnapshot(
                    trace_id="remote-end",
                    name="assistant.turn",
                    timestamp=window_end,
                    observations=[],
                    scores=[],
                ),
            ]

    bundle = collect_runtime_audit(
        source=Source(),
        local_trace_path=local_trace_path,
        window_start=window_start,
        window_end=window_end,
        collected_at=window_end,
    )

    assert [trace.trace_id for trace in bundle.traces] == ["remote-start"]
    assert [manifest.trace_id for manifest in bundle.local_manifests] == ["local-start"]


def test_langfuse_adapter_excludes_fetched_trace_at_window_end() -> None:
    """Would fail if an inclusive Langfuse query leaked the next day's first trace."""

    window_start = datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=1)

    class TraceApi:
        def list(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                data=[SimpleNamespace(id="trace-start"), SimpleNamespace(id="trace-end")],
                meta=SimpleNamespace(total_pages=1),
            )

        def get(self, trace_id: str) -> dict[str, object]:
            timestamp = window_start if trace_id == "trace-start" else window_end
            return {
                "id": trace_id,
                "name": "assistant.turn",
                "timestamp": timestamp,
                "observations": [],
                "scores": [],
            }

    class ScoresApi:
        def get_many_v3(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(data=[], meta=SimpleNamespace(cursor=None))

    source = LangfuseSdkAuditSource(
        SimpleNamespace(api=SimpleNamespace(trace=TraceApi(), scores_v3=ScoresApi()))
    )

    traces = source.list_traces(window_start=window_start, window_end=window_end)

    assert [trace.trace_id for trace in traces] == ["trace-start"]
