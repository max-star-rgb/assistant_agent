import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path("scripts/realtime_media_client.py")


def test_media_client_script_import_is_safe() -> None:
    module = _load_module()

    assert hasattr(module, "main")


def test_media_client_parser_defaults() -> None:
    module = _load_module("realtime_media_client_parser_test")

    args = module.build_parser().parse_args([])

    assert args.server == "http://127.0.0.1:8000"
    assert args.scenario == "basic"
    assert args.user_id == "media_smoke_user"
    assert args.timeout == 45.0
    assert args.strict_cancel is False
    assert args.interactive is False
    assert args.log_dir is None


def test_media_client_builds_media_ws_url() -> None:
    module = _load_module("realtime_media_client_url_test")

    url = module.build_media_ws_url(
        "http://127.0.0.1:8000/base",
        user_id="user 1",
        session_id="session/1",
    )

    assert url.startswith("ws://127.0.0.1:8000/base/ws/realtime/media?")
    assert "user_id=user+1" in url
    assert "session_id=session%2F1" in url
    assert "client=media_service" in url


def test_media_client_rejects_non_web_server_scheme() -> None:
    module = _load_module("realtime_media_client_bad_scheme_test")

    with pytest.raises(module.MediaSmokeError):
        module.build_media_ws_url("ftp://127.0.0.1:8000", user_id="u", session_id="s")


def test_media_client_builds_media_events() -> None:
    module = _load_module("realtime_media_client_events_test")

    start = module.session_start_event(user_id="u", session_id="s")
    transcript = module.transcript_final_event(
        user_id="u",
        session_id="s",
        text="hello",
        interrupt=True,
    )
    cancel = module.run_cancel_event(user_id="u", session_id="s", run_id="run_1")
    end = module.session_end_event(user_id="u", session_id="s")
    ping = module.ping_event(user_id="u", session_id="s")

    assert start["type"] == "session.start"
    assert start["payload"]["config"]["entry"] == "scripted_media_relay"
    assert transcript["type"] == "transcript.final"
    assert transcript["payload"]["text"] == "hello"
    assert transcript["payload"]["interrupt"] is True
    assert transcript["payload"]["metadata"]["source"] == "scripted_media_relay"
    assert cancel["type"] == "run.cancel"
    assert cancel["run_id"] == "run_1"
    assert end["type"] == "session.end"
    assert ping["type"] == "ping"


def test_media_client_parses_operator_commands() -> None:
    module = _load_module("realtime_media_client_operator_commands_test")

    normal = module.parse_operator_command("你好")
    interrupt = module.parse_operator_command("/interrupt 等一下")
    cancel = module.parse_operator_command("/cancel")
    hangup = module.parse_operator_command("/hangup")
    trace = module.parse_operator_command("/trace last")
    report = module.parse_operator_command("/report")
    empty = module.parse_operator_command("  ")

    assert normal.kind == "transcript"
    assert normal.text == "你好"
    assert normal.interrupt is False
    assert interrupt.kind == "transcript"
    assert interrupt.text == "等一下"
    assert interrupt.interrupt is True
    assert cancel.kind == "cancel"
    assert hangup.kind == "hangup"
    assert hangup.should_exit is True
    assert trace.kind == "trace_last"
    assert report.kind == "report"
    assert empty.kind == "noop"


def test_media_client_operator_state_logs_frames_and_reports_session(tmp_path: Path) -> None:
    module = _load_module("realtime_media_client_operator_state_test")
    log_path = tmp_path / "session.jsonl"
    state = module.OperatorSessionState(session_id="s1", log_path=log_path)

    state.record_send({"type": "transcript.final", "payload": {"text": "hello"}})
    state.record_recv({"type": "run.started", "run_id": "run_1"})
    state.record_recv({"type": "stream.chunk", "payload": {"text": "hi"}})
    state.record_recv(
        {
            "type": "run.end",
            "run_id": "run_1",
            "reason": "completed",
            "payload": {"trace_id": "trace_1"},
        }
    )

    report = state.report()
    lines = [module.json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    assert report["session_id"] == "s1"
    assert report["turns"] == 1
    assert report["completed"] == 1
    assert report["cancelled"] == 0
    assert report["last_run_id"] == "run_1"
    assert report["last_trace_id"] == "trace_1"
    assert report["assistant_text"] == "hi"
    assert lines[0]["direction"] == "send"
    assert lines[0]["type"] == "transcript.final"
    assert lines[1]["direction"] == "recv"
    assert lines[1]["type"] == "run.started"


def test_media_client_formats_trace_view_command() -> None:
    module = _load_module("realtime_media_client_trace_command_test")

    command = module.format_trace_view_command("trace_1", server="http://127.0.0.1:8000")

    assert "scripts/trace_view.py trace_1 --server http://127.0.0.1:8000" in command


def test_media_client_all_scenario_expands_to_regression_order() -> None:
    module = _load_module("realtime_media_client_scenarios_test")

    assert module._selected_scenarios("all") == ("ping", "basic", "cancel", "hangup")


def _load_module(name: str = "realtime_media_client_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
