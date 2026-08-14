"""RED/GREEN coverage for the explicit native planning StateGraph."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import VerificationResult
from assistant_agent.native_agent.planning_graph import (
    NativePlanAdmissionError,
    admit_native_plan,
    build_planning_graph,
)
from assistant_agent.native_agent.state import FastAgentState
from assistant_agent.workflows.models import (
    WorkflowAcceptanceCriterion,
    WorkflowArtifactContract,
    WorkflowDeliverableBindingProposal,
    WorkflowPlanNodeV2,
    WorkflowPlanV2Proposal,
    WorkflowStepAcceptanceContract,
)


def _node(node_id: str, *, depends_on: tuple[str, ...] = ()) -> WorkflowPlanNodeV2:
    return WorkflowPlanNodeV2(
        node_id=node_id,
        display_title=node_id,
        objective=f"完成 {node_id}",
        depends_on=list(depends_on),
        acceptance_contract=WorkflowStepAcceptanceContract(
            schema_version="workflow_step_acceptance_v2",
            output=WorkflowArtifactContract(
                artifact_type="text",
                description=f"{node_id} output",
            ),
            criteria=[
                WorkflowAcceptanceCriterion(
                    criterion_id=f"{node_id}_done",
                    statement=f"{node_id} is complete",
                )
            ],
        ),
    )


def _proposal() -> WorkflowPlanV2Proposal:
    return WorkflowPlanV2Proposal(
        schema_version="workflow_plan_v2",
        nodes=[_node("research"), _node("write", depends_on=("research",))],
        deliverable_bindings=[
            WorkflowDeliverableBindingProposal(
                deliverable="report",
                producer_node_id="write",
            )
        ],
    )


class PlanningModel:
    def __init__(self, proposal, verifications) -> None:
        self.proposal = proposal
        self.verifications = list(verifications)
        self.planner_calls = 0
        self.verifier_calls = 0

    def with_structured_output(self, schema):
        if schema is WorkflowPlanV2Proposal:
            def plan(_messages):
                self.planner_calls += 1
                return self.proposal

            return RunnableLambda(plan)
        if schema is VerificationResult:
            def verify(_messages):
                self.verifier_calls += 1
                return self.verifications.pop(0)

            return RunnableLambda(verify)
        raise AssertionError(schema)


def _fast_agent(calls: list[str]):
    async def answer(state: FastAgentState):
        objective = str(state["messages"][-1].content)
        calls.append(objective)
        return {"messages": [AIMessage(content=f"result:{objective}")]}

    builder = StateGraph(FastAgentState, context_schema=AssistantRunContext)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    return builder.compile(name="AssistantFastAgent")


def _invoke(graph):
    return asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="请完成报告")],
                "memory_context": ("偏好中文",),
            },
            context=AssistantRunContext(user_id="user-1", tenant_id="tenant-1"),
        )
    )


def test_planning_graph_admits_dag_and_reuses_fast_agent_by_wave() -> None:
    calls: list[str] = []
    fast_agent = _fast_agent(calls)
    model = PlanningModel(
        _proposal(),
        [VerificationResult(status="passed", reason="ok")],
    )
    graph = build_planning_graph(model, fast_agent)

    result = _invoke(graph)

    assert graph.name == "AssistantPlanningGraph"
    assert model.planner_calls == 1
    assert model.verifier_calls == 1
    assert calls == ["完成 research", "完成 write"]
    assert set(result["worker_results"]) == {"research", "write"}
    assert result["messages"][-1].content.startswith("规划任务已完成")


def test_planning_graph_uses_send_for_parallel_root_workers() -> None:
    proposal = _proposal().model_copy(
        update={
            "nodes": [_node("one"), _node("two")],
            "deliverable_bindings": [
                WorkflowDeliverableBindingProposal(
                    deliverable="report",
                    producer_node_id="two",
                )
            ],
        }
    )
    calls: list[str] = []
    graph = build_planning_graph(
        PlanningModel(
            proposal,
            [VerificationResult(status="passed", reason="ok")],
        ),
        _fast_agent(calls),
    )

    result = _invoke(graph)

    assert Counter(calls) == Counter(["完成 one", "完成 two"])
    assert result["completed_work_item_ids"] == ("one", "two")


def test_planning_graph_repairs_only_requested_worker_with_higher_revision() -> None:
    calls: list[str] = []
    graph = build_planning_graph(
        PlanningModel(
            _proposal(),
            [
                VerificationResult(
                    status="repair",
                    repair_work_item_ids=("write",),
                    reason="补充引用",
                ),
                VerificationResult(status="passed", reason="ok"),
            ],
        ),
        _fast_agent(calls),
        max_repairs=2,
    )

    result = _invoke(graph)

    assert calls == ["完成 research", "完成 write", "完成 write\n修复要求：补充引用"]
    assert result["worker_results"]["write"].revision == 1
    assert result["repair_count"] == 1


def test_planning_graph_stops_when_repair_limit_is_reached() -> None:
    calls: list[str] = []
    graph = build_planning_graph(
        PlanningModel(
            _proposal(),
            [
                VerificationResult(
                    status="repair",
                    repair_work_item_ids=("write",),
                    reason="still incomplete",
                )
            ],
        ),
        _fast_agent(calls),
        max_repairs=0,
    )

    result = _invoke(graph)

    assert calls == ["完成 research", "完成 write"]
    assert "未通过验证" in result["messages"][-1].content


def test_native_plan_admission_rejects_cycles() -> None:
    proposal = _proposal().model_copy(
        update={
            "nodes": [
                _node("one", depends_on=("two",)),
                _node("two", depends_on=("one",)),
            ]
        }
    )

    with pytest.raises(NativePlanAdmissionError, match="cycle"):
        admit_native_plan(proposal)
