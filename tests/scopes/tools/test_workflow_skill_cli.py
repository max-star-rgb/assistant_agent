import json
from pathlib import Path

from assistant_agent.services.tool_workflow_skill_cli import main


def test_workflow_skill_cli_validates_explicit_manifest_and_tools(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    manifest_path = _write_manifest(tmp_path)
    _write_tool_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    exit_code = main(
        [
            "validate",
            "--manifest",
            str(manifest_path),
            "--module",
            "workflow_cli_tools",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["schema_version"] == "workflow_skill_cli_validate_v1"
    assert output["accepted"] is True
    assert output["workflow_id"] == "lookup_flow"
    assert output["issues"] == []
    assert "run_skill" not in output["registered_tools"]


def test_workflow_skill_cli_launches_registered_manifest_to_jsonl_store(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    manifest_path = _write_manifest(tmp_path)
    run_store_path = tmp_path / "workflow_runs.jsonl"
    _write_tool_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    exit_code = main(
        [
            "launch",
            "--manifest",
            str(manifest_path),
            "--module",
            "workflow_cli_tools",
            "--workflow",
            "lookup_flow",
            "--text",
            "forecast",
            "--user-id",
            "u1",
            "--session-id",
            "s1",
            "--run-id",
            "run-cli",
            "--run-store",
            str(run_store_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["schema_version"] == "workflow_skill_cli_launch_v1"
    assert output["result"]["status"] == "succeeded"
    assert output["summary"]["run_id"] == "run-cli"
    assert output["summary"]["workflow_id"] == "lookup_flow"
    assert output["summary"]["attempt_count"] == 1
    assert "step_results" not in output["summary"]
    assert run_store_path.exists()

    exit_code = main(
        [
            "summary",
            "--run-store",
            str(run_store_path),
            "--run-id",
            "run-cli",
        ]
    )
    summary_output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary_output["schema_version"] == "workflow_skill_cli_summary_v1"
    assert summary_output["summary"]["status"] == "succeeded"


def test_workflow_skill_cli_rejects_unregistered_workflow_id(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    manifest_path = _write_manifest(tmp_path)
    _write_tool_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    exit_code = main(
        [
            "launch",
            "--manifest",
            str(manifest_path),
            "--module",
            "workflow_cli_tools",
            "--workflow",
            "missing_flow",
            "--text",
            "forecast",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["result"]["status"] == "validation_failed"
    assert output["result"]["issues"][0]["code"] == "workflow_not_registered"


def _write_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "workflow.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_skill_v1",
                "name": "lookup_flow",
                "type": "workflow",
                "permissions": ["tool:workflow.lookup"],
                "steps": [
                    {
                        "id": "lookup",
                        "tool": "workflow.lookup",
                        "input": {"query": "{{ user.request }}"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_tool_module(tmp_path: Path) -> None:
    (tmp_path / "workflow_cli_tools.py").write_text(
        '''
from pydantic import BaseModel, Field

from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    ExecutionPolicy,
    ToolPolicyMetadata,
)
from assistant_agent.tools.decorators import tool


class LookupInput(BaseModel):
    query: str = Field(min_length=1)


@tool(
    name="workflow.lookup",
    description="Read-only workflow lookup.",
    input_schema=LookupInput,
    policy=ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=3, retry_count=0),
    ),
)
def lookup(input, context):
    return {"summary": f"lookup:{input.query}"}


__assistant_tools__ = [lookup]
'''.lstrip(),
        encoding="utf-8",
    )
