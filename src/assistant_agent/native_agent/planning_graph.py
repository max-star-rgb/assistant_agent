"""Explicit planning StateGraph whose workers reuse the fast create_agent graph."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, Send

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    PlanningArtifact,
    VerificationResult,
    WorkerResult,
)
from assistant_agent.native_agent.state import PlanningState, WorkerState
from assistant_agent.workflows.models import WorkflowPlanV2Proposal


class NativePlanAdmissionError(ValueError):
    """A structured proposal violates deterministic local DAG rules."""


def admit_native_plan(proposal: WorkflowPlanV2Proposal) -> WorkflowPlanV2Proposal:
    """Validate graph references, acyclicity and terminal deliverable ownership."""

    node_ids = [node.node_id for node in proposal.nodes]
    known = set(node_ids)
    incoming = {node_id: 0 for node_id in known}
    outgoing = {node_id: [] for node_id in known}
    for node in proposal.nodes:
        if node.node_id == "plan":
            raise NativePlanAdmissionError("reserved node id: plan")
        for dependency in node.depends_on:
            if dependency == node.node_id:
                raise NativePlanAdmissionError("self dependency")
            if dependency not in known:
                raise NativePlanAdmissionError("unknown dependency")
            incoming[node.node_id] += 1
            outgoing[dependency].append(node.node_id)
    pending = [node_id for node_id, count in incoming.items() if count == 0]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        for child in outgoing[current]:
            incoming[child] -= 1
            if incoming[child] == 0:
                pending.append(child)
    if visited != len(known):
        raise NativePlanAdmissionError("workflow plan contains a cycle")
    terminal = {node_id for node_id, children in outgoing.items() if not children}
    for binding in proposal.deliverable_bindings:
        if binding.producer_node_id not in known:
            raise NativePlanAdmissionError("unknown deliverable producer")
        if binding.producer_node_id not in terminal:
            raise NativePlanAdmissionError("deliverable producer must be terminal")
    for binding in proposal.constraint_bindings:
        if not set(binding.owner_node_ids).issubset(known):
            raise NativePlanAdmissionError("unknown constraint owner")
        if binding.verifier_node_id not in known:
            raise NativePlanAdmissionError("unknown constraint verifier")
    return proposal


def build_planning_graph(
    model: Any,
    fast_agent: Any,
    *,
    max_repairs: int = 2,
):
    """Build planner/wave-worker/verifier topology around one shared fast graph."""

    if getattr(fast_agent, "name", None) != "AssistantFastAgent":
        raise ValueError("planning workers require the shared AssistantFastAgent")
    if max_repairs < 0:
        raise ValueError("max_repairs must be non-negative")
    structured_planner = model.with_structured_output(WorkflowPlanV2Proposal)
    structured_verifier = model.with_structured_output(VerificationResult)

    async def planner_node(state: PlanningState) -> dict[str, Any]:
        proposal = await structured_planner.ainvoke(
            [
                SystemMessage(content=_PLANNER_PROMPT),
                HumanMessage(content=_last_human_text(state)),
            ]
        )
        admitted = admit_native_plan(
            proposal
            if isinstance(proposal, WorkflowPlanV2Proposal)
            else WorkflowPlanV2Proposal.model_validate(proposal)
        )
        return {
            "plan": admitted,
            "repair_count": 0,
            "worker_results": {},
            "completed_work_item_ids": (),
            "artifacts": {},
        }

    async def worker_node(
        state: WorkerState,
        runtime: Runtime[AssistantRunContext],
    ) -> dict[str, Any]:
        result = await fast_agent.ainvoke(
            {
                "messages": [HumanMessage(content=state["objective"])],
                "memory_context": tuple(state.get("memory_context", ())),
                "memory_status": state.get("memory_status", "empty"),
            },
            context=runtime.context,
        )
        content = _last_ai_text(result)
        worker_result = WorkerResult(
            work_item_id=state["work_item_id"],
            revision=int(state.get("revision", 0)),
            content=content or "worker completed without textual output",
        )
        return {
            "worker_results": {worker_result.work_item_id: worker_result},
            "completed_work_item_ids": (worker_result.work_item_id,),
        }

    def join_node(_state: PlanningState) -> dict[str, Any]:
        return {}

    async def verifier_node(state: PlanningState) -> dict[str, Any]:
        result = await structured_verifier.ainvoke(
            [
                SystemMessage(content=_VERIFIER_PROMPT),
                HumanMessage(content=_verification_payload(state)),
            ]
        )
        verification = (
            result
            if isinstance(result, VerificationResult)
            else VerificationResult.model_validate(result)
        )
        known = {node.node_id for node in state["plan"].nodes}
        if not set(verification.repair_work_item_ids).issubset(known):
            raise NativePlanAdmissionError("verifier requested unknown repair item")
        return {"verification": verification}

    def repair_node(state: PlanningState) -> dict[str, Any]:
        return {"repair_count": int(state.get("repair_count", 0)) + 1}

    def finalize_node(state: PlanningState) -> dict[str, Any]:
        verification = state.get("verification")
        passed = verification is not None and verification.status == "passed"
        heading = "规划任务已完成" if passed else "规划任务未通过验证"
        ordered = [
            state.get("worker_results", {}).get(node.node_id)
            for node in state["plan"].nodes
        ]
        body = "\n\n".join(
            f"[{item.work_item_id}] {item.content}" for item in ordered if item is not None
        )
        artifacts = _deliverable_artifacts(state)
        return {
            "messages": [AIMessage(content=f"{heading}\n\n{body}".rstrip())],
            "artifacts": artifacts,
        }

    def dispatch_ready(state: PlanningState):
        sends = _ready_worker_sends(state)
        if not sends:
            raise NativePlanAdmissionError("admitted plan has no executable root")
        return sends

    def route_after_join(state: PlanningState):
        sends = _ready_worker_sends(state)
        return sends or "verifier"

    def route_after_verifier(state: PlanningState) -> str:
        verification = state["verification"]
        if verification.status == "passed":
            return "finalize"
        if (
            verification.status == "repair"
            and verification.repair_work_item_ids
            and int(state.get("repair_count", 0)) < max_repairs
        ):
            return "repair"
        return "finalize"

    def dispatch_repairs(state: PlanningState):
        verification = state["verification"]
        by_id = {node.node_id: node for node in state["plan"].nodes}
        revision = int(state.get("repair_count", 0))
        return [
            Send(
                "worker",
                {
                    "messages": [],
                    "memory_context": tuple(state.get("memory_context", ())),
                    "work_item_id": work_item_id,
                    "objective": (
                        f"{by_id[work_item_id].objective}\n"
                        f"修复要求：{verification.reason}"
                    ),
                    "revision": revision,
                },
            )
            for work_item_id in verification.repair_work_item_ids
        ]

    builder = StateGraph(PlanningState, context_schema=AssistantRunContext)
    builder.add_node(
        "planner",
        planner_node,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    builder.add_node("worker", worker_node, input_schema=WorkerState)
    builder.add_node("join", join_node)
    builder.add_node(
        "verifier",
        verifier_node,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    builder.add_node("repair", repair_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "planner")
    builder.add_conditional_edges("planner", dispatch_ready)
    builder.add_edge("worker", "join")
    builder.add_conditional_edges("join", route_after_join)
    builder.add_conditional_edges("verifier", route_after_verifier)
    builder.add_conditional_edges("repair", dispatch_repairs)
    builder.add_edge("finalize", END)
    return builder.compile(name="AssistantPlanningGraph")


def _ready_worker_sends(state: PlanningState) -> list[Send]:
    completed = set(state.get("completed_work_item_ids", ()))
    sends = []
    for node in state["plan"].nodes:
        if node.node_id in completed or not set(node.depends_on).issubset(completed):
            continue
        sends.append(
            Send(
                "worker",
                {
                    "messages": [],
                    "memory_context": tuple(state.get("memory_context", ())),
                    "work_item_id": node.node_id,
                    "objective": node.objective,
                    "revision": 0,
                },
            )
        )
    return sends


def _verification_payload(state: PlanningState) -> str:
    payload = {
        "plan": state["plan"].model_dump(mode="json"),
        "worker_results": {
            key: value.model_dump(mode="json")
            for key, value in sorted(state.get("worker_results", {}).items())
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _deliverable_artifacts(state: PlanningState) -> dict[str, PlanningArtifact]:
    results = state.get("worker_results", {})
    artifacts = {}
    for binding in state["plan"].deliverable_bindings:
        result = results.get(binding.producer_node_id)
        if result is None:
            continue
        artifact_id = "deliverable_" + hashlib.sha256(
            binding.deliverable.encode()
        ).hexdigest()[:16]
        artifacts[artifact_id] = PlanningArtifact(
            artifact_id=artifact_id,
            content=result.content,
        )
    return artifacts


def _last_human_text(state: PlanningState) -> str:
    for message in reversed(state.get("messages", ())):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _last_ai_text(state: Any) -> str:
    for message in reversed(state.get("messages", ())):
        if isinstance(message, AIMessage) and str(message.content).strip():
            return str(message.content).strip()
    return ""


_PLANNER_PROMPT = (
    "把用户目标拆成静态、有限、无环的 workflow_plan_v2。每个节点只描述一个"
    "可独立交给通用 Agent 执行的目标，并声明显式 depends_on。"
)
_VERIFIER_PROMPT = (
    "验证全部 worker 结果是否满足计划。返回 passed、repair 或 failed；repair 只能"
    "列出需要重新执行的稳定 work_item_id。"
)


__all__ = [
    "NativePlanAdmissionError",
    "admit_native_plan",
    "build_planning_graph",
]
