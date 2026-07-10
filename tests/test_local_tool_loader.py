from pathlib import Path

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.tools.loader import load_local_tools, register_local_tools
from assistant_agent.tools.registry import ToolRegistry


def test_explicit_loader_registers_decorated_tool_without_core_registry_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_weather_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    load_result = load_local_tools(["local_weather_tools"])
    registry = ToolRegistry()
    register_local_tools(registry, load_result.tools)

    assert load_result.issues == []
    assert registry.list() == ["weather.lookup"]
    spec = registry.list_specs()[0]
    assert spec.name == "weather.lookup"
    assert spec.policy is not None
    assert spec.policy.risk == "external_read"

    request = UserRequest(user_id="u1", session_id="s1", text="check weather")
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
    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        "weather.lookup",
        {"location": "Shanghai"},
    )

    assert validation.accepted is True
    assert result.success is True
    assert result.data == {"summary": "Weather for Shanghai: clear"}


def test_loader_reports_missing_explicit_tool_list(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "empty_tools.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    load_result = load_local_tools(["empty_tools"])

    assert load_result.tools == []
    assert load_result.issues[0].code == "missing_tool_list"


def _write_weather_module(tmp_path: Path) -> None:
    (tmp_path / "local_weather_tools.py").write_text(
        '''
from pydantic import BaseModel

from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    ExecutionPolicy,
    ToolPolicyMetadata,
)
from assistant_agent.tools.decorators import tool


class WeatherInput(BaseModel):
    location: str


@tool(
    name="weather.lookup",
    description="Look up weather.",
    input_schema=WeatherInput,
    policy=ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=3),
    ),
)
def weather_lookup(input, context):
    return {"summary": f"Weather for {input.location}: clear"}


__assistant_tools__ = [weather_lookup]
'''.lstrip(),
        encoding="utf-8",
    )
