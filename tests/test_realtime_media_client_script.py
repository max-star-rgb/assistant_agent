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
    transcript = module.transcript_final_event(user_id="u", session_id="s", text="hello")
    cancel = module.run_cancel_event(user_id="u", session_id="s", run_id="run_1")
    end = module.session_end_event(user_id="u", session_id="s")
    ping = module.ping_event(user_id="u", session_id="s")

    assert start["type"] == "session.start"
    assert start["payload"]["config"]["entry"] == "scripted_media_relay"
    assert transcript["type"] == "transcript.final"
    assert transcript["payload"]["text"] == "hello"
    assert cancel["type"] == "run.cancel"
    assert cancel["run_id"] == "run_1"
    assert end["type"] == "session.end"
    assert ping["type"] == "ping"


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
