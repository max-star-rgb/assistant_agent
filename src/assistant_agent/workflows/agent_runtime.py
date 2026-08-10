"""Bounded bridge from Workflow work items to the existing Agent runtime."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from assistant_agent.runtime.requests import AssistantMode
from assistant_agent.workflows.context import WorkflowContextManifest


class AgentWorkItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    work_item_id: str
    attempt_id: str
    user_id: str
    agent_id: str
    session_id: str
    objective: str = Field(min_length=1, max_length=10_000)
    work_item_kind: str = Field(default="generic", min_length=1, max_length=120)
    assistant_mode: AssistantMode = "standard"
    repair_candidate_ids: list[str] = Field(default_factory=list, max_length=128)
    context_manifest: WorkflowContextManifest
    allowed_tool_names: list[str] = Field(default_factory=list)
    workflow_inputs: dict[str, JsonValue] = Field(default_factory=dict)
    max_iterations: int = Field(default=5, ge=1, le=20)


class AgentWorkItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "repair", "blocked", "failed"]
    run_id: str
    summary: str
    artifact_refs: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    repair_work_item_ids: list[str] = Field(default_factory=list)
    model_calls_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)


def render_work_item_prompt(request: AgentWorkItemRequest) -> str:
    lines = [
        "你正在执行一个受限的长期 Workflow work item。",
        f"Work item kind: {request.work_item_kind}",
        f"Work item objective: {request.objective}",
        f"Workflow objective: {request.context_manifest.objective}",
    ]
    if request.context_manifest.constraints:
        lines.append("Constraints:")
        lines.extend(f"- {item}" for item in request.context_manifest.constraints)
    if request.context_manifest.artifacts:
        lines.append("Artifact excerpts:")
        for artifact in request.context_manifest.artifacts:
            lines.append(
                f"[{artifact.artifact_ref} | {artifact.kind} | {artifact.digest}]\n"
                f"{artifact.excerpt}"
            )
    if request.workflow_inputs:
        lines.append(
            "Workflow inputs:\n"
            + json.dumps(request.workflow_inputs, ensure_ascii=False, sort_keys=True)
        )
    lines.append("完成当前 work item 后直接返回结果，不创建新的长期 Workflow。")
    control = (
        '仅当当前步骤无法正常成功时，才把完整最终回复写成严格 JSON：'
        '{"workflow_control":{"status":"blocked|failed","summary":"...",'
        '"unresolved_questions":["..."]}}。不要把控制 JSON 包在 Markdown 中。'
    )
    if request.repair_candidate_ids:
        candidates = ", ".join(request.repair_candidate_ids)
        control += (
            ' 若验证发现必须返工，也可使用 status="repair" 和 '
            f'"repair_work_item_ids"；只允许选择这些祖先步骤：{candidates}。'
        )
    lines.append(control)
    return "\n\n".join(lines)


class _WorkflowControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["repair", "blocked", "failed"]
    summary: str = Field(min_length=1, max_length=4_000)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=64)
    repair_work_item_ids: list[str] = Field(default_factory=list, max_length=128)


class _WorkflowControlEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_control: _WorkflowControl


def parse_work_item_response(
    text: str,
    *,
    run_id: str,
    artifact_refs: list[str],
    model_calls_used: int,
    tool_calls_used: int,
) -> AgentWorkItemResult:
    """Parse the narrow trusted work-item control envelope, with text success fallback."""

    try:
        payload = json.loads(text)
        envelope = _WorkflowControlEnvelope.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return AgentWorkItemResult(
            status="succeeded",
            run_id=run_id,
            summary=text,
            artifact_refs=artifact_refs,
            model_calls_used=model_calls_used,
            tool_calls_used=tool_calls_used,
        )
    control = envelope.workflow_control
    return AgentWorkItemResult(
        status=control.status,
        run_id=run_id,
        summary=control.summary,
        artifact_refs=artifact_refs,
        unresolved_questions=control.unresolved_questions,
        repair_work_item_ids=control.repair_work_item_ids,
        model_calls_used=model_calls_used,
        tool_calls_used=tool_calls_used,
    )
