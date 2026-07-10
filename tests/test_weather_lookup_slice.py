import json
from pathlib import Path

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.tool_history import ToolHistoryStore
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.tools.cli import main as tools_cli
from assistant_agent.tools.loader import load_local_tools, register_local_tools
from assistant_agent.tools.registry import ToolRegistry


def test_weather_lookup_slice_runs_realtime_safe_local_tool_through_executor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_weather_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    load_result = load_local_tools(["weather_slice_tools"])
    registry = ToolRegistry()
    register_local_tools(registry, load_result.tools)
    spec = registry.list_specs()[0]
    policy_view = ToolPolicyInterpreter().view_for_spec(spec)

    assert load_result.issues == []
    assert spec.name == "weather.lookup"
    assert spec.policy is not None
    assert policy_view.auto_executable is True
    assert policy_view.side_effect_level == "external_read"
    assert policy_view.dependency_mode == "independent"
    assert policy_view.realtime_safety == "safe"
    assert policy_view.realtime_mode == "inline"
    assert policy_view.sends_data_external is True
    assert policy_view.redact_in_trace is True
    assert policy_view.toolset == "personal.readonly"
    assert policy_view.enabled_by_default is False

    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="上海天气怎么样",
        metadata={"realtime": {"run_id": "run-1"}},
    )
    state = AgentState.from_request(request, run_id="run-1")
    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="weather.lookup",
            tool_input={"location": "Shanghai"},
        ),
        registry=registry,
        request=request,
        state=state,
    )
    sink = ListEventSink()
    history = ToolHistoryStore(tmp_path / "tool_calls.jsonl")

    result = ToolExecutor(registry=registry, event_sink=sink, tool_history=history).run_tool(
        state,
        "step-1",
        "weather.lookup",
        {"location": "Shanghai"},
    )
    observation = observation_from_tool_result(result)
    started = next(event for event in sink.events if event.type == "tool_started")
    finished = next(event for event in sink.events if event.type == "tool_finished")
    history_record = [record for record in history.read_all() if record.status == "succeeded"][0]

    assert validation.accepted is True
    assert result.success is True
    assert result.data["summary"] == "Weather for Shanghai: clear, 26 C."
    assert observation.summary == "Weather for Shanghai: clear, 26 C."
    assert observation.structured_output["temperature_c"] == 26
    assert "mock-weather://raw/shanghai" not in str(observation)
    assert started.payload["pre_tool_call"]["risk_gate"]["level"] == "auto"
    assert started.payload["pre_tool_call"]["side_effect"]["level"] == "external_read"
    assert finished.payload["post_tool_call"]["side_effect"]["level"] == "external_read"
    assert history_record.output_summary["summary"] == "Weather lookup succeeded."
    assert history_record.audit_payload == {"provider": "mock_weather", "redacted": True}
    assert history_record.raw_data_ref == "mock-weather://raw/shanghai"
    assert "mock-weather://raw/shanghai" not in str(history_record.output_summary)


def test_tools_simulate_cli_runs_weather_lookup_through_governed_executor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_weather_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    exit_code = tools_cli(
        [
            "simulate",
            "--module",
            "weather_slice_tools",
            "--tool",
            "weather.lookup",
            "--input",
            json.dumps({"location": "Shanghai"}),
            "--text",
            "上海天气怎么样",
            "--realtime",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["schema_version"] == "local_tools_simulate_v1"
    assert output["validation"]["accepted"] is True
    assert output["result"]["success"] is True
    assert output["result"]["model_observation"]["temperature_c"] == 26
    assert output["post_tool_call"]["risk_gate"]["level"] == "auto"
    assert "mock-weather://raw/shanghai" not in str(output["observation"])


def _write_weather_module(tmp_path: Path) -> None:
    (tmp_path / "weather_slice_tools.py").write_text(
        '''
from pydantic import BaseModel, Field

from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    DataPolicy,
    ExecutionPolicy,
    RealtimeToolPolicy,
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
    VisibilityPolicy,
)
from assistant_agent.tools.decorators import tool


class WeatherInput(BaseModel):
    location: str = Field(min_length=1)


@tool(
    name="weather.lookup",
    description="Look up current weather from the configured weather provider.",
    input_schema=WeatherInput,
    execution=ToolExecutionPolicy(
        dependency_mode="independent",
        resource_reads=["weather.current"],
        realtime_safety="safe",
    ),
    policy=ToolPolicyMetadata(
        risk="external_read",
        realtime=RealtimeToolPolicy(mode="inline"),
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=3, retry_count=0, max_result_chars=1200),
        data=DataPolicy(sends_data_external=True, redact_in_trace=True),
        visibility=VisibilityPolicy(toolset="personal.readonly", enabled_by_default=False),
    ),
)
def weather_lookup(input, context):
    location = input.location.strip()
    normalized = location.lower()
    raw_ref = "mock-weather://raw/shanghai" if normalized == "shanghai" else "mock-weather://raw/other"
    summary = f"Weather for {location}: clear, 26 C."
    return ToolResult(
        tool_name="weather.lookup",
        success=True,
        data={"summary": summary, "side_effect_level": "external_read"},
        voice_summary=f"{location} 现在晴，26 度。",
        model_observation={
            "summary": summary,
            "location": location,
            "condition": "clear",
            "temperature_c": 26,
        },
        trace_summary={"summary": "Weather lookup succeeded.", "provider": "mock_weather"},
        audit_payload={"provider": "mock_weather", "redacted": True},
        raw_data_ref=raw_ref,
    )


__assistant_tools__ = [weather_lookup]
'''.lstrip(),
        encoding="utf-8",
    )
