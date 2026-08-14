from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
import pytest

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.root_graph import build_assistant_root_graph
from assistant_agent.native_agent.state import FastAgentState, PlanningState


class _Memory:
    backend_id = "probe"

    def __init__(self) -> None:
        self.events = []

    async def recall(self, **_kwargs: Any):
        self.events.append("recall")
        return ("memory-sentinel",)

    async def commit(self, **_kwargs: Any):
        self.events.append("commit")


def _branch(schema, name):
    def answer(_state):
        return {"messages": [AIMessage(content="answer-sentinel")]}

    builder = StateGraph(schema, context_schema=AssistantRunContext)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    return builder.compile(name=name)


@pytest.mark.core_invariant("MEMORY-001")
def test_parent_graph_owns_one_recall_and_commit_for_each_mode() -> None:
    async def run(mode):
        backend = _Memory()
        graph = build_assistant_root_graph(
            memory_backend=backend,
            fast_agent=_branch(FastAgentState, "AssistantFastAgent"),
            planning_graph=_branch(PlanningState, "AssistantPlanningGraph"),
        )
        result = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "execution_mode": mode,
            },
            context=AssistantRunContext(
                user_id="user-sentinel",
                tenant_id="tenant-sentinel",
            ),
        )
        return backend.events, result

    async def run_all():
        return await asyncio.gather(run("fast"), run("planning"))

    results = asyncio.run(run_all())

    assert all(events == ["recall", "commit"] for events, _result in results)
    assert all(result["memory_context"] == ("memory-sentinel",) for _events, result in results)
