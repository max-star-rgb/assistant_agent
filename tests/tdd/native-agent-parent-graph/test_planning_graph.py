"""RED/GREEN coverage for the explicit native planning StateGraph."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from assistant_agent.config import ProviderConfig
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
)
from assistant_agent.native_agent.planning_graph import (
    NativePlanAdmissionError,
    admit_native_plan,
    build_planning_graph,
)
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.state import FastAgentState


def _node(node_id: str, *, depends_on: tuple[str, ...] = ()) -> NativePlanNode:
    return NativePlanNode(
        node_id=node_id,
        objective=f"完成 {node_id}",
        depends_on=depends_on,
    )


def _proposal() -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v1",
        nodes=(_node("research"), _node("write", depends_on=("research",))),
    )


class PlanningModel:
    def __init__(self, proposal) -> None:
        self.proposal = proposal
        self.planner_calls = 0

    def with_structured_output(self, schema):
        if schema is NativePlanProposal:
            def plan(_messages):
                self.planner_calls += 1
                return self.proposal

            return RunnableLambda(plan)
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
    model = PlanningModel(_proposal())
    graph = build_planning_graph(model, fast_agent)

    result = _invoke(graph)

    assert graph.name == "AssistantPlanningGraph"
    assert model.planner_calls == 1
    assert calls == ["完成 research", "完成 write"]
    assert [item.work_item_id for item in result["worker_results"]] == [
        "research",
        "write",
    ]
    assert result["messages"][-1].content.startswith("规划任务已完成")


def test_planning_graph_uses_send_for_parallel_root_workers() -> None:
    proposal = _proposal().model_copy(
        update={
            "nodes": (_node("one"), _node("two")),
        }
    )
    calls: list[str] = []
    graph = build_planning_graph(
        PlanningModel(proposal),
        _fast_agent(calls),
    )

    result = _invoke(graph)

    assert Counter(calls) == Counter(["完成 one", "完成 two"])
    assert {item.work_item_id for item in result["worker_results"]} == {
        "one",
        "two",
    }
    assert result["messages"][-1].content.index("[one]") < result[
        "messages"
    ][-1].content.index("[two]")


def test_mock_provider_emits_the_minimal_native_plan_contract() -> None:
    """Catches mock composition retaining removed planning contract fields."""

    graph = build_planning_graph(
        create_chat_model(ProviderConfig(provider_mode="mock")),
        _fast_agent([]),
    )

    result = _invoke(graph)

    assert [item.work_item_id for item in result["worker_results"]] == ["answer"]
    assert result["messages"][-1].content.startswith("规划任务已完成")


def test_native_plan_admission_rejects_cycles() -> None:
    proposal = _proposal().model_copy(
        update={
            "nodes": (
                _node("one", depends_on=("two",)),
                _node("two", depends_on=("one",)),
            )
        }
    )

    with pytest.raises(NativePlanAdmissionError, match="cycle"):
        admit_native_plan(proposal)
