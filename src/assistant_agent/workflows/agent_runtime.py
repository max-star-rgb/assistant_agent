"""Bounded bridge from Workflow work items to the existing Agent runtime."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from assistant_agent.runtime.requests import AssistantMode
from assistant_agent.workflows.context import WorkflowContextManifest
from assistant_agent.workflows.models import WorkflowConstraintBinding
from assistant_agent.workflows.models import WorkflowPlanProposal


class AgentWorkItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_type: str = Field(min_length=1, max_length=80)
    workflow_trace_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
    )
    work_item_id: str
    attempt_id: str
    display_title: str = Field(min_length=1, max_length=240)
    user_id: str
    agent_id: str
    session_id: str
    objective: str = Field(min_length=1, max_length=10_000)
    work_item_kind: str = Field(default="generic", min_length=1, max_length=120)
    attempt_number: int = Field(default=1, ge=1, le=20)
    previous_error_code: str | None = Field(default=None, max_length=160)
    previous_result_summary: str = Field(default="", max_length=4_000)
    acceptance_contract: dict[str, JsonValue] = Field(default_factory=dict)
    assigned_constraints: list[WorkflowConstraintBinding] = Field(
        default_factory=list,
        max_length=64,
    )
    assistant_mode: AssistantMode = "standard"
    repair_candidate_ids: list[str] = Field(default_factory=list, max_length=128)
    context_manifest: WorkflowContextManifest
    workflow_deliverables: list[str] = Field(default_factory=list, max_length=32)
    workflow_constraints: list[str] = Field(default_factory=list, max_length=64)
    allowed_tool_names: list[str] = Field(default_factory=list)
    workflow_inputs: dict[str, JsonValue] = Field(default_factory=dict)
    max_iterations: int = Field(default=5, ge=1, le=20)
    agent_role: Literal["planner", "worker"] = "worker"


class AgentWorkItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "repair", "blocked", "failed"]
    run_id: str
    trace_id: str | None = None
    summary: str
    content: str = Field(default="", max_length=100_000)
    error_code: str | None = Field(default=None, max_length=160)
    artifact_refs: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    repair_work_item_ids: list[str] = Field(default_factory=list)
    model_calls_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    agent_role: Literal["planner", "worker"] = "worker"
    plan_proposal: WorkflowPlanProposal | None = None


def render_work_item_prompt(request: AgentWorkItemRequest) -> str:
    lines = [
        "你正在执行一个受限的长期 Workflow work item。",
        f"Work item kind: {request.work_item_kind}",
        f"Work item objective: {request.objective}",
        f"Workflow objective: {request.context_manifest.objective}",
    ]
    if request.attempt_number > 1:
        lines.append(
            "Previous attempt feedback:\n"
            + json.dumps(
                {
                    "attempt_number": request.attempt_number,
                    "error_code": request.previous_error_code,
                    "summary": request.previous_result_summary,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n纠正上一尝试暴露的问题；不要重复返回相同的无效结果。"
        )
    if request.agent_role == "planner":
        if request.workflow_deliverables:
            lines.append(
                "Workflow deliverables:\n"
                + json.dumps(request.workflow_deliverables, ensure_ascii=False)
            )
        if request.workflow_constraints:
            lines.append(
                "Workflow constraints:\n"
                + json.dumps(request.workflow_constraints, ensure_ascii=False)
            )
        if request.workflow_inputs:
            lines.append(
                "Workflow inputs:\n"
                + json.dumps(
                    request.workflow_inputs,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        lines.append(
            "你是当前 durable Plan-and-Execute execution 的主规划 Agent。"
            "只返回严格 JSON，不要执行研究，也不要包裹 Markdown："
            '{"workflow_plan":{"workstreams":[{"seed_id":"...",'
            '"kind":"...","display_title":"...","objective":"...",'
            '"depends_on":[],"acceptance_contract":{}}],'
            '"constraint_bindings":[{"constraint_id":"...",'
            '"statement":"...","owner_work_item_ids":["..."],'
            '"verifier_work_item_id":"...","severity":"required|advisory"}]}}。'
            "步骤只承担自己的 acceptance_contract；最终交付约束绑定到负责产出或验证它的步骤。"
        )
        return "\n\n".join(lines)
    if request.assigned_constraints:
        lines.append(
            "Assigned workflow constraints:\n"
            + json.dumps(
                [
                    item.model_dump(mode="json")
                    for item in request.assigned_constraints
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    if request.acceptance_contract:
        lines.append(
            "Work item acceptance contract:\n"
            + json.dumps(request.acceptance_contract, ensure_ascii=False, sort_keys=True)
        )
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
    verifier_ids = [
        item.constraint_id
        for item in request.assigned_constraints
        if item.verifier_work_item_id == request.work_item_id
    ]
    if verifier_ids:
        control = (
            "当前步骤是结构化约束 verifier。验证成功也必须把完整最终回复写成严格 JSON："
            '{"workflow_control":{"status":"verified","summary":"...",'
            '"content":"可选的完整交付物","verified_constraint_ids":["..."]}}。'
            "verified_constraint_ids 必须完整覆盖："
            + json.dumps(verifier_ids, ensure_ascii=False)
            + "。不要把控制 JSON 包在 Markdown 中。"
        )
    else:
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


class _WorkflowPlanEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_plan: WorkflowPlanProposal


def parse_workflow_plan_response(
    text: str,
    *,
    run_id: str,
    trace_id: str | None,
    model_calls_used: int,
    tool_calls_used: int,
) -> AgentWorkItemResult:
    """Parse the main planner's strict durable DAG envelope."""

    try:
        proposal = _WorkflowPlanEnvelope.model_validate_json(text).workflow_plan
    except ValueError:
        return AgentWorkItemResult(
            status="failed",
            run_id=run_id,
            trace_id=trace_id,
            summary="Planner did not return a valid structured workflow plan.",
            error_code="workflow_plan_invalid",
            model_calls_used=model_calls_used,
            tool_calls_used=tool_calls_used,
            agent_role="planner",
        )
    return AgentWorkItemResult(
        status="succeeded",
        run_id=run_id,
        trace_id=trace_id,
        summary=f"Planned {len(proposal.workstreams)} workflow items.",
        model_calls_used=model_calls_used,
        tool_calls_used=tool_calls_used,
        agent_role="planner",
        plan_proposal=proposal,
    )


class _WorkflowControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["verified", "repair", "blocked", "failed"]
    summary: str = Field(min_length=1, max_length=4_000)
    content: str = Field(default="", max_length=100_000)
    verified_constraint_ids: list[str] = Field(default_factory=list, max_length=64)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=64)
    repair_work_item_ids: list[str] = Field(default_factory=list, max_length=128)


class _WorkflowControlEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_control: _WorkflowControl


def parse_work_item_response(
    text: str,
    *,
    run_id: str,
    trace_id: str | None = None,
    artifact_refs: list[str],
    model_calls_used: int,
    tool_calls_used: int,
    required_verification_ids: list[str] | None = None,
) -> AgentWorkItemResult:
    """Parse the narrow trusted work-item control envelope, with text success fallback."""

    required_ids = set(required_verification_ids or [])
    try:
        payload = json.loads(text)
        envelope = _WorkflowControlEnvelope.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        if required_ids:
            return AgentWorkItemResult(
                status="failed",
                run_id=run_id,
                trace_id=trace_id,
                summary="Verifier did not return a structured constraint result.",
                error_code="verification_result_missing",
                artifact_refs=artifact_refs,
                model_calls_used=model_calls_used,
                tool_calls_used=tool_calls_used,
            )
        return AgentWorkItemResult(
            status="succeeded",
            run_id=run_id,
            trace_id=trace_id,
            summary=_bounded_summary(text),
            content=text,
            artifact_refs=artifact_refs,
            model_calls_used=model_calls_used,
            tool_calls_used=tool_calls_used,
        )
    control = envelope.workflow_control
    if control.status == "verified":
        missing_ids = required_ids.difference(control.verified_constraint_ids)
        if missing_ids:
            return AgentWorkItemResult(
                status="failed",
                run_id=run_id,
                trace_id=trace_id,
                summary="Verifier result did not cover every assigned constraint.",
                error_code="verification_incomplete",
                artifact_refs=artifact_refs,
                model_calls_used=model_calls_used,
                tool_calls_used=tool_calls_used,
            )
        return AgentWorkItemResult(
            status="succeeded",
            run_id=run_id,
            trace_id=trace_id,
            summary=control.summary,
            content=control.content or control.summary,
            artifact_refs=artifact_refs,
            model_calls_used=model_calls_used,
            tool_calls_used=tool_calls_used,
        )
    return AgentWorkItemResult(
        status=control.status,
        run_id=run_id,
        trace_id=trace_id,
        summary=control.summary,
        artifact_refs=artifact_refs,
        unresolved_questions=control.unresolved_questions,
        repair_work_item_ids=control.repair_work_item_ids,
        model_calls_used=model_calls_used,
        tool_calls_used=tool_calls_used,
    )


def _bounded_summary(text: str, *, max_chars: int = 4_000) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"
