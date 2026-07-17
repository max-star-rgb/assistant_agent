from pydantic import BaseModel

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ApprovalPolicy, ExecutionPolicy, ToolPolicyMetadata, ToolResult
from assistant_agent.services.tool_workflow_skill import (
    WorkflowSkillRunner,
    validate_workflow_skill_manifest,
)
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class LookupInput(BaseModel):
    query: str


class LookupTool(MockTool):
    name = "workflow.lookup"
    description = "Read-only workflow lookup."
    input_schema = LookupInput
    output_schema = LookupInput
    policy = ToolPolicyMetadata(
        risk="external_read",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(retry_count=0),
    )

    def __init__(self) -> None:
        self.calls = 0

    def _run(self, input: LookupInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"query": input.query, "summary": f"lookup:{input.query}"},
        )


class FlakyLookupTool(LookupTool):
    name = "workflow.flaky_lookup"

    def _run(self, input: LookupInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="provider_timeout: transient timeout",
            )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"query": input.query, "summary": "recovered"},
        )


class WriteTool(LookupTool):
    name = "workflow.write"
    policy = ToolPolicyMetadata(
        risk="external_write",
        approval=ApprovalPolicy(mode="always"),
        execution=ExecutionPolicy(retry_count=0, idempotency="none"),
    )


def test_workflow_skill_runner_executes_governed_tool_through_executor() -> None:
    tool = LookupTool()
    registry = ToolRegistry()
    registry.register(tool)
    manifest = {
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
    state = AgentState.from_request(
        UserRequest(user_id="u1", session_id="s1", text="weather"),
        run_id="run-1",
    )

    result = WorkflowSkillRunner(registry=registry).run(manifest, state)

    assert result.success is True
    assert result.status == "succeeded"
    assert tool.calls == 1
    assert [call.tool_name for call in state.tool_calls] == ["workflow.lookup"]
    assert result.attempts[0].status == "succeeded"
    assert result.attempts[0].workflow_id == "lookup_flow"
    assert result.attempts[0].step_id == "lookup"


def test_workflow_skill_step_retry_reinvokes_safe_read_only_step() -> None:
    tool = FlakyLookupTool()
    registry = ToolRegistry()
    registry.register(tool)
    manifest = {
        "schema_version": "workflow_skill_v1",
        "name": "retry_flow",
        "type": "workflow",
        "permissions": ["tool:workflow.flaky_lookup"],
        "steps": [
            {
                "id": "lookup",
                "tool": "workflow.flaky_lookup",
                "input": {"query": "headlines"},
                "retry": {"max_retries": 1},
            }
        ],
    }
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="news"))

    result = WorkflowSkillRunner(registry=registry).run(manifest, state)

    assert result.success is True
    assert tool.calls == 2
    assert [attempt.status for attempt in result.attempts] == ["failed", "succeeded"]
    assert result.attempts[0].retry_count == 0
    assert result.attempts[1].retry_count == 0


def test_workflow_skill_validator_rejects_unsupported_step_actions() -> None:
    registry = ToolRegistry()
    registry.register(LookupTool())

    result = validate_workflow_skill_manifest(
        {
            "schema_version": "workflow_skill_v1",
            "name": "unsafe_flow",
            "type": "workflow",
            "steps": [{"id": "shell", "command": "curl https://example.test"}],
        },
        registry=registry,
    )

    assert result.accepted is False
    assert result.issues[0].code == "unsupported_step_action"


def test_workflow_skill_validator_rejects_unknown_tool_and_permission_vocabulary() -> None:
    result = validate_workflow_skill_manifest(
        {
            "schema_version": "workflow_skill_v1",
            "name": "bad_permissions",
            "type": "workflow",
            "permissions": ["shell:run"],
            "steps": [{"id": "lookup", "tool": "missing.lookup"}],
        },
        registry=ToolRegistry(),
    )

    codes = [issue.code for issue in result.issues]
    assert "invalid_permission" in codes
    assert "unknown_tool" in codes


def test_workflow_skill_validator_requires_idempotency_for_mutating_step_retry() -> None:
    registry = ToolRegistry()
    registry.register(WriteTool())

    result = validate_workflow_skill_manifest(
        {
            "schema_version": "workflow_skill_v1",
            "name": "write_retry",
            "type": "workflow",
            "permissions": ["tool:workflow.write"],
            "steps": [
                {
                    "id": "write",
                    "tool": "workflow.write",
                    "input": {"query": "book"},
                    "retry": {"max_retries": 1},
                }
            ],
        },
        registry=registry,
    )

    assert result.accepted is False
    assert result.issues[0].code == "step_retry_requires_idempotency"
