"""Explicit planning StateGraph whose workers reuse the fast create_agent graph."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, Send

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    NativePlanProposal,
    WorkerResult,
)
from assistant_agent.native_agent.state import PlanningState, WorkerState


class NativePlanAdmissionError(ValueError):
    """A structured proposal violates deterministic local DAG rules."""


def admit_native_plan(proposal: NativePlanProposal) -> NativePlanProposal:
    """Validate graph references and acyclicity."""

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
    return proposal


def build_planning_graph(
    model: Any,
    fast_agent: Any,
):
    """Build a minimal planner/wave-worker/finalize graph around one fast graph."""

    if getattr(fast_agent, "name", None) != "AssistantFastAgent":
        raise ValueError("planning workers require the shared AssistantFastAgent")
    structured_planner = model.with_structured_output(NativePlanProposal)

    async def planner_node(state: PlanningState) -> dict[str, Any]:
        proposal = await structured_planner.ainvoke(
            [
                SystemMessage(content=_PLANNER_PROMPT),
                HumanMessage(content=_last_human_text(state)),
            ]
        )
        admitted = admit_native_plan(
            proposal
            if isinstance(proposal, NativePlanProposal)
            else NativePlanProposal.model_validate(proposal)
        )
        return {
            "plan": admitted,
            "worker_results": [],
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
                "execution_mode": state["execution_mode"],
            },
            context=runtime.context,
        )
        content = _last_ai_text(result)
        worker_result = WorkerResult(
            work_item_id=state["work_item_id"],
            content=content or "worker completed without textual output",
        )
        return {"worker_results": [worker_result]}

    def join_node(_state: PlanningState) -> dict[str, Any]:
        return {}

    def finalize_node(state: PlanningState) -> dict[str, Any]:
        by_id = {item.work_item_id: item for item in state.get("worker_results", ())}
        ordered = [by_id.get(node.node_id) for node in state["plan"].nodes]
        body = "\n\n".join(
            f"[{item.work_item_id}] {item.content}"
            for item in ordered
            if item is not None
        )
        return {"messages": [AIMessage(content=f"规划任务已完成\n\n{body}".rstrip())]}

    def dispatch_ready(state: PlanningState):
        sends = _ready_worker_sends(state)
        if not sends:
            raise NativePlanAdmissionError("admitted plan has no executable root")
        return sends

    def route_after_join(state: PlanningState):
        sends = _ready_worker_sends(state)
        return sends or "finalize"

    builder = StateGraph(PlanningState, context_schema=AssistantRunContext)
    builder.add_node(
        "planner",
        planner_node,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    builder.add_node("worker", worker_node, input_schema=WorkerState)
    builder.add_node("join", join_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "planner")
    builder.add_conditional_edges("planner", dispatch_ready)
    builder.add_edge("worker", "join")
    builder.add_conditional_edges("join", route_after_join)
    builder.add_edge("finalize", END)
    return builder.compile(name="AssistantPlanningGraph")


def _ready_worker_sends(state: PlanningState) -> list[Send]:
    completed = {item.work_item_id for item in state.get("worker_results", ())}
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
                    "execution_mode": "planning",
                    "work_item_id": node.node_id,
                    "objective": node.objective,
                },
            )
        )
    return sends


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
    "把用户目标拆成静态、有限、无环的 native_plan_v1。每个节点只描述一个"
    "可独立交给通用 Agent 执行的目标，并声明必要的 depends_on。"
)


__all__ = [
    "NativePlanAdmissionError",
    "admit_native_plan",
    "build_planning_graph",
]
