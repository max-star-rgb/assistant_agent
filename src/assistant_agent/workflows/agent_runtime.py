"""Bounded bridge from Workflow work items to the existing Agent runtime."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from assistant_agent.runtime.requests import AssistantMode
from assistant_agent.workflows.context import WorkflowContextManifest
from assistant_agent.workflows.models import WorkflowConstraintBinding
from assistant_agent.workflows.models import WorkflowPlannerProposal
from assistant_agent.workflows.models import WorkflowPlanProposal
from assistant_agent.workflows.models import WorkflowPlanV2Proposal
from assistant_agent.workflows.models import WorkflowStepAcceptanceContract


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
    acceptance_contract: WorkflowStepAcceptanceContract | dict[str, JsonValue] = Field(
        default_factory=dict
    )
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
    plan_proposal: WorkflowPlannerProposal | None = None


def render_workflow_planner_prompt(
    *,
    workflow_objective: str,
    workflow_deliverables: list[str] | tuple[str, ...],
    workflow_constraints: list[str] | tuple[str, ...],
    workflow_inputs: dict[str, JsonValue],
    planning_objective: str,
    attempt_number: int = 1,
    previous_error_code: str | None = None,
    previous_result_summary: str = "",
) -> str:
    """Compile trusted Workflow inputs into the strict planner envelope prompt."""

    planner_input = {
        "workflow": {
            "objective": workflow_objective,
            "deliverables": workflow_deliverables,
            "constraints": workflow_constraints,
            "inputs": workflow_inputs,
        },
        "planning_work_item": {
            "objective": planning_objective,
            "attempt_number": attempt_number,
            "previous_attempt": (
                {
                    "error_code": previous_error_code,
                    "summary": previous_result_summary,
                }
                if attempt_number > 1
                else None
            ),
        },
    }
    return "\n\n".join(
        [
            "角色\n你是 durable Plan-and-Execute Workflow 的主规划 Agent。"
            "你的唯一职责是生成一个可由 controller 接纳和执行的计划版本。",
            "执行边界\n"
            "- 只规划，不执行研究、编码、检索或其他业务任务。\n"
            "- 不调用工具，不创建递归 planner 节点，不在 JSON 外输出解释。\n"
            "- 普通 ReAct 决策发生在各执行节点内部，不写入 DAG 控制边。",
            "可信工作流输入\n"
            + json.dumps(planner_input, ensure_ascii=False, sort_keys=True),
            "DAG 规则\n"
            "- 生成一个静态有向无环图；depends_on 只能引用同一计划中的 node_id。\n"
            "- 无依赖节点可并行；需要汇总多个结果的节点必须显式依赖所有上游节点。\n"
            "- 每个 requested deliverable 必须恰好绑定一个 terminal producer。\n"
            "- 不为简单任务制造无意义节点；每个节点必须能独立重试并产生一个下游可用 artifact。",
            "验收与责任规则\n"
            "- 每个节点必须声明一个类型化 output 和至少一个仅由该节点负责的 criterion。\n"
            "- 根据用户目标自主提出完成任务所需的可验证 workflow constraint；"
            "不得使用 Runtime 隐含的固定业务阈值。\n"
            "- 可信输入中的 workflow constraint 必须逐条原文保留并绑定 owner；"
            "required constraint 必须指定 verifier。\n"
            "- verifier 必须等于 owner 或位于所有 owner 的下游；不要用节点名称暗示责任。",
            "唯一允许的输出 JSON Schema\n"
            + json.dumps(
                _WorkflowPlanV2Envelope.model_json_schema(),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "输出要求\n只返回一个符合上述 schema 的 JSON object；"
            "不得包裹 Markdown，不得增加 schema 未声明字段。",
        ]
    )


def render_work_item_prompt(request: AgentWorkItemRequest) -> str:
    lines = (
        []
        if request.agent_role == "planner"
        else [
            "你正在执行一个受限的长期 Workflow work item。",
            f"Work item kind: {request.work_item_kind}",
            f"Work item objective: {request.objective}",
            f"Workflow objective: {request.context_manifest.objective}",
        ]
    )
    if request.attempt_number > 1 and request.agent_role != "planner":
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
        return render_workflow_planner_prompt(
            workflow_objective=request.context_manifest.objective,
            workflow_deliverables=request.workflow_deliverables,
            workflow_constraints=request.workflow_constraints,
            workflow_inputs=request.workflow_inputs,
            planning_objective=request.objective,
            attempt_number=request.attempt_number,
            previous_error_code=request.previous_error_code,
            previous_result_summary=request.previous_result_summary,
        )
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
    acceptance_contract_model = (
        request.acceptance_contract
        if isinstance(request.acceptance_contract, WorkflowStepAcceptanceContract)
        else WorkflowStepAcceptanceContract.model_validate(
            request.acceptance_contract
        )
        if request.acceptance_contract
        else None
    )
    if acceptance_contract_model is not None:
        lines.append(
            "Work item acceptance contract:\n"
            + json.dumps(
                acceptance_contract_model.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
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
    acceptance_ids = (
        [item.criterion_id for item in acceptance_contract_model.criteria]
        if acceptance_contract_model is not None
        else []
    )
    if verifier_ids or acceptance_ids:
        success_status = "verified" if verifier_ids else "succeeded"
        control = (
            "当前步骤必须以结构化结果证明自己的验收契约。"
            "成功时把完整最终回复写成严格 JSON："
            '{"workflow_control":{"status":"'
            + success_status
            + '","summary":"...","content":"完整交付物",'
            '"acceptance_evidence":[{"criterion_id":"...",'
            '"evidence":"artifact 中可核对的完成证据"}],'
            '"verified_constraint_ids":["..."]}}。'
            "acceptance_evidence 必须精确覆盖："
            + json.dumps(acceptance_ids, ensure_ascii=False)
            + "；verified_constraint_ids 必须精确覆盖："
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


class _WorkflowPlanV1Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_plan: WorkflowPlanProposal


class _WorkflowPlanV2Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_plan: WorkflowPlanV2Proposal


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
        payload = json.loads(text)
        workflow_plan = payload.get("workflow_plan") if isinstance(payload, dict) else None
        if (
            isinstance(workflow_plan, dict)
            and workflow_plan.get("schema_version") == "workflow_plan_v2"
        ):
            proposal: WorkflowPlannerProposal = (
                _WorkflowPlanV2Envelope.model_validate(payload).workflow_plan
            )
        else:
            proposal = _WorkflowPlanV1Envelope.model_validate(payload).workflow_plan
    except (json.JSONDecodeError, TypeError, ValueError):
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
    item_count = (
        len(proposal.nodes)
        if isinstance(proposal, WorkflowPlanV2Proposal)
        else len(proposal.workstreams)
    )
    return AgentWorkItemResult(
        status="succeeded",
        run_id=run_id,
        trace_id=trace_id,
        summary=f"Planned {item_count} workflow items.",
        model_calls_used=model_calls_used,
        tool_calls_used=tool_calls_used,
        agent_role="planner",
        plan_proposal=proposal,
    )


class _WorkflowCriterionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1, max_length=120)
    evidence: str = Field(min_length=1, max_length=4_000)


class _WorkflowControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "verified", "repair", "blocked", "failed"]
    summary: str = Field(min_length=1, max_length=4_000)
    content: str = Field(default="", max_length=100_000)
    acceptance_evidence: list[_WorkflowCriterionEvidence] = Field(
        default_factory=list,
        max_length=64,
    )
    verified_constraint_ids: list[str] = Field(default_factory=list, max_length=64)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=64)
    repair_work_item_ids: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> "_WorkflowControl":
        criterion_ids = [item.criterion_id for item in self.acceptance_evidence]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance evidence criterion ids must be unique")
        if len(self.verified_constraint_ids) != len(
            set(self.verified_constraint_ids)
        ):
            raise ValueError("verified constraint ids must be unique")
        return self


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
    required_acceptance_ids: list[str] | None = None,
) -> AgentWorkItemResult:
    """Parse the narrow trusted work-item control envelope, with text success fallback."""

    required_ids = set(required_verification_ids or [])
    required_criteria = set(required_acceptance_ids or [])
    try:
        payload = json.loads(text)
        envelope = _WorkflowControlEnvelope.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        if required_criteria:
            return AgentWorkItemResult(
                status="failed",
                run_id=run_id,
                trace_id=trace_id,
                summary="Worker did not return structured acceptance evidence.",
                error_code="acceptance_result_missing",
                artifact_refs=artifact_refs,
                model_calls_used=model_calls_used,
                tool_calls_used=tool_calls_used,
            )
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
    if control.status in {"succeeded", "verified"}:
        evidence_ids = {
            item.criterion_id for item in control.acceptance_evidence
        }
        if evidence_ids != required_criteria:
            return AgentWorkItemResult(
                status="failed",
                run_id=run_id,
                trace_id=trace_id,
                summary="Worker result did not cover its acceptance contract.",
                error_code="acceptance_incomplete",
                artifact_refs=artifact_refs,
                model_calls_used=model_calls_used,
                tool_calls_used=tool_calls_used,
            )
        if required_ids and control.status != "verified":
            return AgentWorkItemResult(
                status="failed",
                run_id=run_id,
                trace_id=trace_id,
                summary="Constraint verifier did not return verified status.",
                error_code="verification_result_missing",
                artifact_refs=artifact_refs,
                model_calls_used=model_calls_used,
                tool_calls_used=tool_calls_used,
            )
        missing_ids = required_ids.difference(control.verified_constraint_ids)
        unexpected_ids = set(control.verified_constraint_ids).difference(required_ids)
        if missing_ids or unexpected_ids:
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
