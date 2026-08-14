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
    def __init__(self, proposal, *, final_response: str = "综合完成") -> None:
        self.proposal = proposal
        self.final_response = final_response
        self.planner_calls = 0
        self.finalizer_calls = 0
        self.finalizer_messages = []

    def with_structured_output(self, schema):
        if schema is NativePlanProposal:

            def plan(_messages):
                self.planner_calls += 1
                return self.proposal

            return RunnableLambda(plan)
        raise AssertionError(schema)

    async def ainvoke(self, messages):
        self.finalizer_calls += 1
        self.finalizer_messages = messages
        return AIMessage(content=self.final_response)


def _fast_agent(calls: list[tuple[str, str | None]]):
    async def answer(state: FastAgentState):
        objective = str(state["messages"][-1].content)
        calls.append((objective, state.get("execution_mode")))
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
            context=AssistantRunContext(),
        )
    )


def test_planning_graph_passes_dependency_results_to_the_next_worker_wave() -> None:
    """Catches dependencies acting only as barriers without carrying their output."""

    calls: list[tuple[str, str | None]] = []
    fast_agent = _fast_agent(calls)
    model = PlanningModel(_proposal())
    graph = build_planning_graph(model, fast_agent)

    result = _invoke(graph)

    assert graph.name == "AssistantPlanningGraph"
    assert model.planner_calls == 1
    assert calls[0] == ("完成 research", "planning")
    write_prompt, write_mode = calls[1]
    assert write_mode == "planning"
    assert write_prompt.startswith("完成 write")
    assert "result:完成 research" in write_prompt
    assert [item.work_item_id for item in result["worker_results"]] == [
        "research",
        "write",
    ]
    assert isinstance(result["messages"][-1], AIMessage)


def test_planning_graph_uses_send_for_parallel_root_workers() -> None:
    proposal = _proposal().model_copy(
        update={
            "nodes": (_node("one"), _node("two")),
        }
    )
    calls: list[tuple[str, str | None]] = []
    graph = build_planning_graph(
        PlanningModel(proposal),
        _fast_agent(calls),
    )

    result = _invoke(graph)

    assert Counter(calls) == Counter(
        [("完成 one", "planning"), ("完成 two", "planning")]
    )
    assert {item.work_item_id for item in result["worker_results"]} == {
        "one",
        "two",
    }


def test_planning_graph_uses_the_model_to_synthesize_ordered_results() -> None:
    """Catches finalize reverting to mechanical concatenation of worker output."""

    model = PlanningModel(_proposal(), final_response="最终综合结果")
    graph = build_planning_graph(model, _fast_agent([]))

    result = _invoke(graph)

    assert model.finalizer_calls == 1
    assert result["messages"][-1].content == "最终综合结果"
    final_input = str(model.finalizer_messages[-1].content)
    assert "请完成报告" in final_input
    assert final_input.index('"work_item_id": "research"') < final_input.index(
        '"work_item_id": "write"'
    )


def test_mock_provider_emits_the_minimal_native_plan_contract() -> None:
    """Catches mock composition retaining removed planning contract fields."""

    graph = build_planning_graph(
        create_chat_model(ProviderConfig(provider_mode="mock")),
        _fast_agent([]),
    )

    result = _invoke(graph)

    assert [item.work_item_id for item in result["worker_results"]] == ["answer"]
    assert isinstance(result["messages"][-1], AIMessage)
    assert str(result["messages"][-1].content).strip()


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
