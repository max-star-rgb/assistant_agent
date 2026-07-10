from pathlib import Path

from assistant_agent.tools.cli import main


def test_tools_validate_cli_accepts_governed_local_tool(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_module(
        tmp_path,
        "valid_tools.py",
        policy="""
    policy=ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=3),
    ),
""",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    exit_code = main(["validate", "--module", "valid_tools"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert '"tool_count": 1' in output
    assert '"weather.lookup"' in output
    assert '"issues": []' in output


def test_tools_validate_cli_reports_missing_policy(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_module(tmp_path, "missing_policy_tools.py", policy="")
    monkeypatch.syspath_prepend(str(tmp_path))

    exit_code = main(["validate", "--module", "missing_policy_tools"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "missing_policy" in output
    assert "weather.lookup" in output


def _write_module(tmp_path: Path, module_name: str, *, policy: str) -> None:
    (tmp_path / module_name).write_text(
        f'''
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
{policy})
def weather_lookup(input, context):
    return {{"summary": f"Weather for {{input.location}}: clear"}}


__assistant_tools__ = [weather_lookup]
'''.lstrip(),
        encoding="utf-8",
    )
