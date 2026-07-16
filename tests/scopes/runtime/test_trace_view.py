import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant_agent.services.trace_store import JsonlTraceStore, TraceEvent


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = "scripts/trace_view.py"


def test_trace_view_last_outputs_latest_local_run(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_view("last", "--trace-path", str(trace_path), "--errors")

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("run run_new trace trace_new status=completed events=4")
    assert "llm.chat.finished" in result.stdout
    assert "react.decision" in result.stdout
    assert "run_old" not in result.stdout


def test_trace_view_follow_outputs_current_latest_run_once(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--follow",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0
    assert "run run_new trace trace_new status=completed events=4" in result.stdout
    assert "run_old" not in result.stdout


def _run_trace_view(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_sample_trace(path: Path) -> None:
    store = JsonlTraceStore(path)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    for event in (
        _event("trace_old", "run_old", "run.started", created_at=base_time),
        _event(
            "trace_old",
            "run_old",
            "run.completed",
            status="completed",
            created_at=base_time + timedelta(milliseconds=10),
        ),
        _event(
            "trace_new",
            "run_new",
            "run.started",
            created_at=base_time + timedelta(seconds=1),
        ),
        _event(
            "trace_new",
            "run_new",
            "llm.chat.finished",
            status="succeeded",
            provider="mock",
            model="mock-chat",
            latency_ms=42,
            created_at=base_time + timedelta(seconds=1, milliseconds=42),
        ),
        _event(
            "trace_new",
            "run_new",
            "react.decision",
            status="succeeded",
            created_at=base_time + timedelta(seconds=1, milliseconds=45),
            attributes={"decision_type": "tool_call", "iteration": 1},
        ),
        _event(
            "trace_new",
            "run_new",
            "run.completed",
            status="completed",
            created_at=base_time + timedelta(seconds=1, milliseconds=50),
        ),
    ):
        store.append(event)


def _event(
    trace_id: str,
    run_id: str,
    canonical_event: str,
    *,
    status: str = "started",
    provider: str | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    created_at: datetime,
    attributes: dict[str, object] | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id=trace_id,
        run_id=run_id,
        node_name="runtime",
        event_type="observability",
        canonical_event=canonical_event,
        status=status,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        attributes=attributes or {},
        created_at=created_at,
    )
