import hashlib
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = "scripts/gateway_view.py"


def test_gateway_lifecycle_jsonl_store_persists_prompt_safe_record(tmp_path: Path) -> None:
    from assistant_agent.gateway.observability import (
        GatewayLifecycleEvent,
        JsonlGatewayLifecycleStore,
    )

    event_path = tmp_path / "gateway_events.jsonl"
    store = JsonlGatewayLifecycleStore(event_path)
    store.append(
        GatewayLifecycleEvent(
            type="gateway.run.cancel_requested",
            user_id="user-secret-123",
            session_id="session-secret-456",
            run_id="run_gateway_123456",
            turn_id="turn_gateway_abcdef",
            payload={
                "trace_id": "trace_runtime_999999",
                "reason": "client supplied text with sk-secret",
                "source": "client",
                "queue_depth": 2,
                "unsafe_payload": "do not persist",
            },
        )
    )

    [record] = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["schema_version"] == "gateway_lifecycle_event_v1"
    assert record["component"] == "gateway"
    assert record["event"] == "gateway.run.cancel_requested"
    assert record["run_id"] == "run_gateway_123456"
    assert record["turn_id"] == "turn_gateway_abcdef"
    assert record["trace_id"] == "trace_runtime_999999"
    assert record["user_id"].startswith("sha256:")
    assert record["session_id"].startswith("sha256:")
    assert "user-secret-123" not in json.dumps(record)
    assert "session-secret-456" not in json.dumps(record)
    assert record["attributes"] == {
        "queue_depth": 2,
        "reason": "client_supplied",
        "source": "client",
    }


def test_gateway_view_tail_renders_jsonl_timeline(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_jsonl(
        event_path,
        [
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:00.000Z",
                "component": "gateway",
                "event": "gateway.server.starting",
                "run_id": None,
                "turn_id": None,
                "trace_id": None,
                "user_id": None,
                "session_id": None,
                "attributes": {"host": "127.0.0.1", "port": 8089},
            },
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:01.000Z",
                "component": "gateway",
                "event": "gateway.session.acquired",
                "run_id": None,
                "turn_id": None,
                "trace_id": None,
                "user_id": "sha256:user",
                "session_id": "sha256:session",
                "attributes": {"created": True},
            },
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:02.000Z",
                "component": "gateway",
                "event": "gateway.run.started",
                "run_id": "run_gateway_123456",
                "turn_id": "turn_gateway_abcdef",
                "trace_id": "trace_runtime_999999",
                "user_id": "sha256:user",
                "session_id": "sha256:session",
                "attributes": {"queue_depth": 2},
            },
        ],
    )

    result = _run_gateway_view("--event-path", str(event_path), "--tail", "10")

    assert result.returncode == 0, result.stderr
    assert "Gateway timeline" in result.stdout
    assert "gateway.server.starting" in result.stdout
    assert "server starting host=127.0.0.1 port=8089" in result.stdout
    assert "gateway.session.acquired" in result.stdout
    assert "session acquired created=True" in result.stdout
    assert "gateway.run.started" in result.stdout
    assert "run=run_gateway_123456" in result.stdout
    assert "turn=turn_gateway_abcdef" in result.stdout
    assert "trace=trace_runtime_999999" in result.stdout
    assert "queue_depth=2" in result.stdout


def test_gateway_view_last_outputs_latest_terminal_run(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_jsonl(
        event_path,
        [
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:01:00.000Z",
                "component": "gateway",
                "event": "gateway.run.completed",
                "run_id": "run_cancel",
                "turn_id": "turn_cancel",
                "trace_id": "trace_cancel",
                "user_id": "sha256:user",
                "session_id": "sha256:session",
                "attributes": {"status": "completed"},
            }
        ],
    )

    result = _run_gateway_view("last", "--event-path", str(event_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("gateway run run_cancel trace trace_cancel status=completed events=1")
    assert "gateway.run.completed" in result.stdout
    assert "run=run_cancel" in result.stdout


def test_gateway_view_last_ignores_session_lifecycle_after_latest_run(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_jsonl(
        event_path,
        [
            _gateway_record(
                "gateway.run.started",
                run_id="run_latest",
                trace_id="trace_latest",
                session_id="debug-session",
                created_at="2026-07-16T12:00:00.000Z",
            ),
            _gateway_record(
                "gateway.run.completed",
                run_id="run_latest",
                trace_id="trace_latest",
                session_id="debug-session",
                created_at="2026-07-16T12:00:01.000Z",
                attributes={"status": "completed"},
            ),
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:02.000Z",
                "component": "gateway",
                "event": "gateway.session.destroyed",
                "run_id": None,
                "turn_id": None,
                "trace_id": None,
                "user_id": _digest("user"),
                "session_id": None,
                "attributes": {"active_count": 0},
            },
        ],
    )

    result = _run_gateway_view("last", "--event-path", str(event_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(
        "gateway run run_latest trace trace_latest status=completed events=2"
    )
    assert "gateway.run.started" in result.stdout
    assert "gateway.run.completed" in result.stdout
    assert "gateway.session.destroyed" not in result.stdout


def test_gateway_view_follow_latest_waits_for_new_gateway_run_by_default(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_gateway_session_sample_events(event_path)

    result = _run_gateway_view(
        "last",
        "--event-path",
        str(event_path),
        "--follow",
        "--follow-timeout",
        "0",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_gateway_view_follow_latest_keeps_global_session_visibility_by_default() -> None:
    module = _load_gateway_view_module()
    args = module.build_parser().parse_args(["last", "--follow"])

    assert module._follow_lookup_session_id(args, locked_session_id=None) is None

    locked = module._next_locked_follow_session_id(
        args,
        locked_session_id=None,
        current_session_id="sha256:agentservice",
    )

    assert locked is None
    assert module._follow_lookup_session_id(args, locked_session_id=locked) is None


def test_gateway_view_follow_all_sessions_keeps_global_latest_mode() -> None:
    module = _load_gateway_view_module()
    args = module.build_parser().parse_args(["last", "--follow", "--follow-all-sessions"])

    locked = module._next_locked_follow_session_id(
        args,
        locked_session_id=None,
        current_session_id="sha256:agentservice",
    )

    assert locked is None
    assert module._follow_lookup_session_id(args, locked_session_id="ignored") is None


def test_gateway_view_follow_can_include_existing_latest_run_with_initial_session_separator_by_default(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_gateway_session_sample_events(event_path)

    result = _run_gateway_view(
        "last",
        "--event-path",
        str(event_path),
        "--follow",
        "--follow-include-existing",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(
        f"\n================ SESSION {_digest('other-session')} ================\n"
        "gateway run run_global_latest trace trace_global_latest status=completed events=2"
    )
    assert "gateway.run.started" in result.stdout
    assert "gateway.run.completed" in result.stdout
    assert "run_debug" not in result.stdout


def test_gateway_view_follow_default_prints_separator_after_session_switch() -> None:
    module = _load_gateway_view_module()
    args = module.build_parser().parse_args(["last", "--follow"])

    assert module._should_print_session_separator(
        args,
        printed_any=False,
        session_changed=True,
    ) is True
    assert module._should_print_session_separator(
        args,
        printed_any=True,
        session_changed=True,
    ) is True


def test_gateway_view_follow_latest_collects_all_new_runs_between_polls(tmp_path: Path) -> None:
    module = _load_gateway_view_module()
    event_path = tmp_path / "gateway_events.jsonl"
    _write_gateway_session_sample_events(event_path)
    args = module.build_parser().parse_args(["last", "--event-path", str(event_path), "--follow"])
    source_path, _, line_parser = module._source(args)

    groups = module._follow_event_groups(
        args,
        source_path,
        line_parser=line_parser,
        suppressed_group_ids=set(),
    )

    assert [group[0].fields["run_id"] for group in groups] == [
        "run_debug",
        "run_global_latest",
    ]


def test_gateway_view_follow_latest_skips_session_lifecycle_groups(tmp_path: Path) -> None:
    module = _load_gateway_view_module()
    event_path = tmp_path / "gateway_events.jsonl"
    _write_jsonl(
        event_path,
        [
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:00.000Z",
                "component": "gateway",
                "event": "gateway.session.acquired",
                "run_id": None,
                "turn_id": None,
                "trace_id": None,
                "user_id": _digest("user"),
                "session_id": None,
                "attributes": {"active_count": 1},
            },
            _gateway_record(
                "gateway.run.completed",
                run_id="run_latest",
                trace_id="trace_latest",
                session_id="debug-session",
                created_at="2026-07-16T12:00:01.000Z",
                attributes={"status": "completed"},
            ),
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:02.000Z",
                "component": "gateway",
                "event": "gateway.session.destroyed",
                "run_id": None,
                "turn_id": None,
                "trace_id": None,
                "user_id": _digest("user"),
                "session_id": None,
                "attributes": {"active_count": 0},
            },
        ],
    )
    args = module.build_parser().parse_args(["last", "--event-path", str(event_path), "--follow"])
    source_path, _, line_parser = module._source(args)

    groups = module._follow_event_groups(
        args,
        source_path,
        line_parser=line_parser,
        suppressed_group_ids=set(),
    )

    assert [module._follow_group_id(group) for group in groups] == ["run:run_latest"]


def test_gateway_view_follow_can_show_session_separator(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_gateway_session_sample_events(event_path)

    result = _run_gateway_view(
        "last",
        "--event-path",
        str(event_path),
        "--follow",
        "--follow-include-existing",
        "--show-session-banner",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(
        f"\n================ SESSION {_digest('other-session')} ================\n"
        "gateway run run_global_latest trace trace_global_latest status=completed events=2"
    )


def test_gateway_view_follow_session_id_filters_without_session_separator(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_gateway_session_sample_events(event_path)

    result = _run_gateway_view(
        "last",
        "--event-path",
        str(event_path),
        "--session-id",
        "debug-session",
        "--follow",
        "--follow-include-existing",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(
        "gateway run run_debug trace trace_debug status=completed events=2"
    )
    assert "SESSION " not in result.stdout
    assert "run_global_latest" not in result.stdout


def test_gateway_view_follow_include_existing_waits_for_terminal_run(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_jsonl(
        event_path,
        [
            _gateway_record(
                "gateway.run.started",
                run_id="run_partial",
                trace_id="trace_partial",
                session_id="debug-session",
            ),
        ],
    )

    result = _run_gateway_view(
        "last",
        "--event-path",
        str(event_path),
        "--follow",
        "--follow-include-existing",
        "--follow-timeout",
        "0",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_gateway_view_follow_live_updates_prints_partial_run(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_jsonl(
        event_path,
        [
            _gateway_record(
                "gateway.run.started",
                run_id="run_partial",
                trace_id="trace_partial",
                session_id="debug-session",
            ),
        ],
    )

    result = _run_gateway_view(
        "last",
        "--event-path",
        str(event_path),
        "--follow",
        "--follow-include-existing",
        "--follow-live-updates",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0, result.stderr
    assert "gateway run run_partial trace trace_partial status=running events=1" in result.stdout
    assert "gateway.run.started" in result.stdout


def test_gateway_view_bad_jsonl_line_falls_back_to_raw_summary(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    event_path.write_text("this is not gateway jsonl output\n", encoding="utf-8")

    result = _run_gateway_view("--event-path", str(event_path), "--tail", "5")

    assert result.returncode == 0, result.stderr
    assert "raw log line" in result.stdout
    assert "this is not gateway jsonl output" in result.stdout


def test_gateway_view_legacy_log_path_still_renders_key_value_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "gateway.log"
    log_path.write_text(
        (
            "2026-07-16T12:01:00.000Z level=INFO component=gateway "
            "event=gateway.run.cancel_requested run_id=run_cancel "
            "turn_id=turn_cancel trace_id=trace_cancel "
            "user_id=sha256:user session_id=sha256:session source=client phase=active\n"
        ),
        encoding="utf-8",
    )

    result = _run_gateway_view("--log-path", str(log_path), "--tail", "5")

    assert result.returncode == 0, result.stderr
    assert "gateway.run.cancel_requested" in result.stdout
    assert "cancel requested source=client phase=active" in result.stdout


def _run_gateway_view(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _load_gateway_view_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("gateway_view_test_module", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_gateway_session_sample_events(path: Path) -> None:
    _write_jsonl(
        path,
        [
            _gateway_record(
                "gateway.run.started",
                run_id="run_debug",
                trace_id="trace_debug",
                session_id="debug-session",
                created_at="2026-07-16T12:00:00.000Z",
            ),
            _gateway_record(
                "gateway.run.completed",
                run_id="run_debug",
                trace_id="trace_debug",
                session_id="debug-session",
                created_at="2026-07-16T12:00:01.000Z",
                attributes={"status": "completed"},
            ),
            _gateway_record(
                "gateway.run.started",
                run_id="run_global_latest",
                trace_id="trace_global_latest",
                session_id="other-session",
                created_at="2026-07-16T12:00:02.000Z",
            ),
            _gateway_record(
                "gateway.run.completed",
                run_id="run_global_latest",
                trace_id="trace_global_latest",
                session_id="other-session",
                created_at="2026-07-16T12:00:03.000Z",
                attributes={"status": "completed"},
            ),
        ],
    )


def _gateway_record(
    event: str,
    *,
    run_id: str,
    trace_id: str,
    session_id: str,
    created_at: str = "2026-07-16T12:00:00.000Z",
    attributes: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "gateway_lifecycle_event_v1",
        "created_at": created_at,
        "component": "gateway",
        "event": event,
        "run_id": run_id,
        "turn_id": f"turn_{run_id}",
        "trace_id": trace_id,
        "user_id": _digest("user"),
        "session_id": _digest(session_id),
        "attributes": attributes or {},
    }


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}"
