import json
from pathlib import Path

from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app
from assistant_agent.api.auth import (
    AUTH_HEADER_ENABLED_ENV,
    AUTH_REQUIRE_BOUND_IDENTITY_ENV,
    AUTH_SESSION_ID_HEADER,
    AUTH_USER_ID_HEADER,
)
from assistant_agent.services.trial_access import TRIAL_USER_IDS_ENV


def test_workflow_skill_api_returns_disabled_response_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MULTIMODAL_AGENT_WORKFLOW_SKILLS_ENABLED", raising=False)

    response = TestClient(create_app()).get("/workflow-skills")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "WORKFLOW_SKILLS_DISABLED"


def test_workflow_skill_api_lists_launches_and_queries_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_dir = tmp_path / "workflows"
    manifest_dir.mkdir()
    _write_manifest(manifest_dir / "lookup_flow.json")
    _write_tool_module(tmp_path)
    run_store = tmp_path / "workflow_runs.jsonl"
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILLS_ENABLED", "1")
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILL_MANIFEST_DIR", str(manifest_dir))
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILL_TOOL_MODULES", "workflow_api_tools")
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILL_RUN_STORE", str(run_store))

    client = TestClient(create_app())

    listed = client.get("/workflow-skills")
    launched = client.post(
        "/workflow-skills/lookup_flow/runs",
        json={
            "text": "forecast",
            "user_id": "u1",
            "session_id": "s1",
            "run_id": "run-api-workflow",
        },
    )
    summary = client.get("/workflow-skill-runs/run-api-workflow")
    runs = client.get("/workflow-skills/lookup_flow/runs")

    assert listed.status_code == 200
    assert listed.json()["enabled"] is True
    assert listed.json()["workflows"][0]["workflow_id"] == "lookup_flow"
    assert launched.status_code == 200
    assert launched.json()["status"] == "succeeded"
    assert launched.json()["summary"]["attempt_count"] == 1
    assert summary.status_code == 200
    assert summary.json()["summary"]["run_id"] == "run-api-workflow"
    assert runs.status_code == 200
    assert runs.json()["summaries"][0]["run_id"] == "run-api-workflow"
    serialized = json.dumps(launched.json(), ensure_ascii=False)
    assert "step_results" not in serialized
    assert "lookup:forecast" not in serialized


def test_workflow_skill_api_rejects_unknown_workflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_dir = tmp_path / "workflows"
    manifest_dir.mkdir()
    _write_manifest(manifest_dir / "lookup_flow.json")
    _write_tool_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILLS_ENABLED", "1")
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILL_MANIFEST_DIR", str(manifest_dir))
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILL_TOOL_MODULES", "workflow_api_tools")

    response = TestClient(create_app()).post(
        "/workflow-skills/missing_flow/runs",
        json={"text": "forecast"},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "WORKFLOW_NOT_FOUND"


def test_workflow_skill_api_requires_auth_bound_identity_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_workflow_api(tmp_path, monkeypatch)
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "1")

    response = TestClient(create_app()).get("/workflow-skills")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "IDENTITY_NOT_AUTH_BOUND"


def test_workflow_skill_api_enforces_trial_access(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_workflow_api(tmp_path, monkeypatch)
    monkeypatch.setenv(TRIAL_USER_IDS_ENV, "allowed-user")

    response = TestClient(create_app()).get("/workflow-skills")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "TRIAL_ACCESS_DENIED"


def test_workflow_skill_api_rejects_auth_identity_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_workflow_api(tmp_path, monkeypatch)
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "1")
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "1")

    response = TestClient(create_app()).post(
        "/workflow-skills/lookup_flow/runs",
        headers={
            AUTH_USER_ID_HEADER: "auth-user",
            AUTH_SESSION_ID_HEADER: "auth-session",
        },
        json={
            "text": "forecast",
            "user_id": "other-user",
            "session_id": "other-session",
            "run_id": "run-mismatch",
        },
    )

    assert response.status_code == 403
    assert "auth context" in response.json()["detail"]


def test_workflow_skill_api_rejects_duplicate_run_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_workflow_api(tmp_path, monkeypatch)
    client = TestClient(create_app())
    body = {
        "text": "forecast",
        "user_id": "u1",
        "session_id": "s1",
        "run_id": "duplicate-run",
    }

    first = client.post("/workflow-skills/lookup_flow/runs", json=body)
    duplicate = client.post("/workflow-skills/lookup_flow/runs", json=body)

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "WORKFLOW_RUN_CONFLICT"


def _write_manifest(path: Path) -> None:
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


def _write_tool_module(tmp_path: Path) -> None:
    (tmp_path / "workflow_api_tools.py").write_text(
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


def _configure_workflow_api(tmp_path: Path, monkeypatch) -> None:
    manifest_dir = tmp_path / "workflows"
    manifest_dir.mkdir()
    _write_manifest(manifest_dir / "lookup_flow.json")
    _write_tool_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILLS_ENABLED", "1")
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILL_MANIFEST_DIR", str(manifest_dir))
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILL_TOOL_MODULES", "workflow_api_tools")
    monkeypatch.setenv("MULTIMODAL_AGENT_WORKFLOW_SKILL_RUN_STORE", str(tmp_path / "runs.jsonl"))
