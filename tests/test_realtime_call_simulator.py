import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path("scripts/run_realtime_call_simulator.py")


def test_realtime_call_simulator_import_is_safe() -> None:
    module = _load_module()

    assert hasattr(module, "main")


def test_realtime_call_simulator_parser_defaults() -> None:
    module = _load_module("realtime_call_simulator_parser_test")

    args = module.build_parser().parse_args([])

    assert args.scenario == "basic"
    assert args.user_id == "text_realtime_sim_user"
    assert args.session_id is None
    assert args.quiet is False


def test_realtime_call_simulator_all_scenario_expands_to_phase1_order() -> None:
    module = _load_module("realtime_call_simulator_scenarios_test")

    assert module._selected_scenarios("all") == (
        "basic",
        "interrupt",
        "hangup",
        "cancel",
        "tool_interrupt",
    )


def test_realtime_call_simulator_basic_scenario_is_text_only() -> None:
    module = _load_module("realtime_call_simulator_basic_test")

    summaries = module.run_simulator(
        scenario="basic",
        user_id="sim-user",
        session_id="sim-basic",
        text="你好，测试文本实时通话",
        quiet=True,
    )

    assert len(summaries) == 1
    summary = summaries[0].to_dict()
    assert summary["status"] == "passed"
    assert summary["frames"] == [
        "call.ready",
        "run.started",
        "stream.chunk",
        "run.end",
        "call.hangup_ack",
    ]
    assert summary["terminal_reasons"] == ["completed"]
    assert summary["hangup_cancelled_active_run"] is False
    assert summary["requests"][0]["text"] == "你好，测试文本实时通话"
    assert summary["requests"][0]["audio_id"] is None
    assert summary["requests"][0]["image_ids"] == []
    assert summary["requests"][0]["video_ids"] == []
    assert summary["requests"][0]["source"] == "realtime_media_websocket"
    assert summary["requests"][0]["source_detail"] == "text_realtime_simulator"


def test_realtime_call_simulator_all_scenarios_pass() -> None:
    module = _load_module("realtime_call_simulator_all_test")

    summaries = module.run_simulator(
        scenario="all",
        user_id="sim-user",
        session_id=None,
        text="继续回答第二个文本 turn",
        quiet=True,
    )

    by_name = {summary.scenario: summary.to_dict() for summary in summaries}
    assert set(by_name) == {"basic", "interrupt", "hangup", "cancel", "tool_interrupt"}
    assert by_name["basic"]["terminal_reasons"] == ["completed"]
    assert set(by_name["interrupt"]["terminal_reasons"]) == {"cancelled", "completed"}
    assert by_name["hangup"]["terminal_reasons"] == ["cancelled"]
    assert by_name["hangup"]["hangup_cancelled_active_run"] is True
    assert by_name["cancel"]["terminal_reasons"] == ["cancelled"]
    assert by_name["cancel"]["hangup_cancelled_active_run"] is False
    assert set(by_name["tool_interrupt"]["terminal_reasons"]) == {"cancelled", "completed"}
    assert by_name["tool_interrupt"]["hangup_cancelled_active_run"] is False
    assert not any("stale tool result" in text for text in by_name["tool_interrupt"]["final_texts"])
    assert all(summary["status"] == "passed" for summary in by_name.values())


def _load_module(name: str = "realtime_call_simulator_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
