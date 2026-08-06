from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_agent.observability.runtime_audit import storage as storage_module
from assistant_agent.observability.runtime_audit.cli import (
    _parser,
    _resolve_bundle_path,
    main,
)
from assistant_agent.observability.runtime_audit.collector import collect_runtime_audit
from assistant_agent.observability.runtime_audit.daily_window import (
    pending_audit_dates,
    previous_day_window,
    window_for_date,
)
from assistant_agent.observability.runtime_audit.daily_models import DailyAuditAttempt
from assistant_agent.observability.runtime_audit.langfuse_source import LangfuseSdkAuditSource
from assistant_agent.observability.runtime_audit.models import LangfuseTraceSnapshot
from assistant_agent.observability.runtime_audit.storage import RuntimeAuditArtifactStore


def test_daily_artifacts_keep_codex_json_internal_and_publish_one_markdown(tmp_path: Path) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    attempt = DailyAuditAttempt(
        attempt_id="runtime_audit_20260806_0015",
        audit_date=date(2026, 8, 5),
        status="succeeded",
        bundle_path="/tmp/bundle.json",
        codex_output_path="/tmp/codex.json",
    )
    attempt_path = store.write_attempt(attempt)
    report_path = store.write_daily_report(date(2026, 8, 5), "# 日报", replace=True)
    assert attempt_path == store.state_dir / "attempts" / f"{attempt.attempt_id}.json"
    assert store.codex_json_path(attempt.attempt_id).parent == store.state_dir / "attempts"
    assert report_path == store.reports_dir / "2026-08-05.md"
    assert list(store.reports_dir.glob("*.json")) == []


def test_failed_rerun_does_not_replace_successful_daily_report(tmp_path: Path) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    path = store.write_daily_report(date(2026, 8, 5), "成功日报", replace=True)
    store.write_failed_daily_report_if_absent(date(2026, 8, 5), "失败日报")
    assert path.read_text(encoding="utf-8").strip() == "成功日报"


def test_rolling_markdown_stays_internal_and_cli_reports_its_actual_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    bundle = SimpleNamespace(audit_run_id="runtime_audit_20260806_0015")
    rolling_path = store.write_deterministic_report(bundle, "内部报告")

    assert rolling_path == (
        store.attempts_dir / "runtime_audit_20260806_0015.deterministic.md"
    )
    assert list(store.reports_dir.glob("*.md")) == []

    monkeypatch.setattr(
        "assistant_agent.observability.runtime_audit.cli._collect",
        lambda *args, **kwargs: (Path("/tmp/bundle.json"), rolling_path, bundle),
    )

    assert main(["--no-env-file", "--repo-root", str(tmp_path), "run", "--skip-codex"]) == 0
    assert json.loads(capsys.readouterr().out)["report_path"] == str(rolling_path)


def test_concurrent_failed_publish_cannot_replace_successful_daily_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    failure_ready = threading.Event()
    allow_failed_publish = threading.Event()
    original_publish = storage_module._atomic_write_if_absent

    def blocked_failed_publish(path: Path, content: str) -> bool:
        failure_ready.set()
        assert allow_failed_publish.wait(timeout=5)
        return original_publish(path, content)

    monkeypatch.setattr(storage_module, "_atomic_write_if_absent", blocked_failed_publish)
    failed_writer = threading.Thread(
        target=store.write_failed_daily_report_if_absent,
        args=(date(2026, 8, 5), "失败日报"),
    )
    failed_writer.start()
    assert failure_ready.wait(timeout=5)

    successful_path = store.write_daily_report(date(2026, 8, 5), "成功日报", replace=True)
    allow_failed_publish.set()
    failed_writer.join(timeout=5)

    assert not failed_writer.is_alive()
    assert successful_path.read_text(encoding="utf-8").strip() == "成功日报"


def test_legacy_watermark_is_not_a_completed_daily_audit(tmp_path: Path) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    store.watermark_path.parent.mkdir(parents=True)
    store.watermark_path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_agent_runtime_audit_watermark_v1",
                "audit_run_id": "runtime_audit_20260806_0015",
                "last_window_end": "2026-08-05T16:00:00Z",
                "bundle_path": "/tmp/legacy-bundle.json",
            }
        ),
        encoding="utf-8",
    )

    assert store.last_completed_date() is None

    watermark_path = store.mark_day_completed(
        date(2026, 8, 5),
        attempt_id="runtime_audit_20260806_0015",
        bundle_path="/tmp/bundle.json",
    )

    assert watermark_path == store.watermark_path
    assert json.loads(watermark_path.read_text(encoding="utf-8")) == {
        "schema_version": "assistant_agent_runtime_audit_watermark_v2",
        "last_completed_date": "2026-08-05",
        "last_attempt_id": "runtime_audit_20260806_0015",
        "bundle_path": "/tmp/bundle.json",
    }
    assert store.last_completed_date() == date(2026, 8, 5)


def test_bundle_resolution_prefers_latest_pointer_then_legacy_watermark(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    store = RuntimeAuditArtifactStore(repo_root / "runtime_audit")
    store.latest_bundle_path.parent.mkdir(parents=True)
    store.latest_bundle_path.write_text(
        json.dumps({"bundle_path": "latest.json"}), encoding="utf-8"
    )
    store.watermark_path.write_text(
        json.dumps({"bundle_path": "legacy.json"}), encoding="utf-8"
    )

    assert _resolve_bundle_path(None, store=store, repo_root=repo_root) == (
        repo_root / "latest.json"
    )

    store.latest_bundle_path.unlink()

    assert _resolve_bundle_path(None, store=store, repo_root=repo_root) == (
        repo_root / "legacy.json"
    )


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
