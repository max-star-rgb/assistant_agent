import json
from pathlib import Path

from assistant_agent.services.tool_workflow_skill_runtime_app import (
    WORKFLOW_SKILLS_ENABLED_ENV,
    WORKFLOW_SKILL_MANIFEST_DIR_ENV,
    WORKFLOW_SKILL_RUN_STORE_ENV,
    WORKFLOW_SKILL_TOOL_MODULES_ENV,
    WorkflowSkillRuntimeApp,
)


def test_workflow_skill_runtime_app_loads_manifests_and_launches_jsonl_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_dir = tmp_path / "workflows"
    manifest_dir.mkdir()
    _write_manifest(manifest_dir / "lookup_flow.json")
    _write_tool_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    app = WorkflowSkillRuntimeApp.from_env(
        env={
            WORKFLOW_SKILLS_ENABLED_ENV: "1",
            WORKFLOW_SKILL_MANIFEST_DIR_ENV: str(manifest_dir),
            WORKFLOW_SKILL_TOOL_MODULES_ENV: "workflow_runtime_tools",
            WORKFLOW_SKILL_RUN_STORE_ENV: str(tmp_path / "runs.jsonl"),
        },
        base_dir=tmp_path,
    )

    workflows = app.list_workflows()
    launched = app.launch(
        "lookup_flow",
        text="forecast",
        user_id="u1",
        session_id="s1",
        run_id="run-runtime",
    )
    summary = app.summary("run-runtime")

    assert app.enabled is True
    assert app.issues == []
    assert [workflow.workflow_id for workflow in workflows] == ["lookup_flow"]
    assert workflows[0].step_count == 1
    assert launched.status == "succeeded"
    assert launched.summary is not None
    assert launched.summary.run_id == "run-runtime"
    assert summary is not None
    assert summary.status == "succeeded"
    assert (tmp_path / "runs.jsonl").exists()


def test_workflow_skill_runtime_app_reports_invalid_manifest_without_registering(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_dir = tmp_path / "workflows"
    manifest_dir.mkdir()
    (manifest_dir / "unsafe.json").write_text(
        json.dumps(
            {
                "schema_version": "workflow_skill_v1",
                "name": "unsafe",
                "type": "workflow",
                "steps": [{"id": "shell", "command": "curl https://example.test"}],
            }
        ),
        encoding="utf-8",
    )
    _write_tool_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    app = WorkflowSkillRuntimeApp.from_env(
        env={
            WORKFLOW_SKILLS_ENABLED_ENV: "1",
            WORKFLOW_SKILL_MANIFEST_DIR_ENV: str(manifest_dir),
            WORKFLOW_SKILL_TOOL_MODULES_ENV: "workflow_runtime_tools",
        },
        base_dir=tmp_path,
    )

    assert app.list_workflows() == []
    assert app.issues[0].code == "unsupported_step_action"


def _write_manifest(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_skill_v1",
                "name": "lookup_flow",
                "type": "workflow",
                "description": "Lookup flow.",
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


def _write_tool_module(tmp_path: Path) -> None:
    (tmp_path / "workflow_runtime_tools.py").write_text(
        '''
from pydantic import BaseModel, Field

from assistant_agent.schemas.tools import ApprovalPolicy, ExecutionPolicy, ToolPolicyMetadata
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
