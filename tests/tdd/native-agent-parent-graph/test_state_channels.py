"""RED/GREEN coverage for the native parent graph state boundary."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import WorkerResult
from assistant_agent.native_agent.state import (
    AssistantRootInput,
    AssistantRootState,
    PlanningState,
)


def test_root_state_messages_use_native_append_semantics() -> None:
    """Catches replacing AgentState's messages reducer with overwrite semantics."""

    def append_reply(_state: AssistantRootState) -> dict[str, object]:
        return {"messages": [AIMessage(content="answer")]}

    builder = StateGraph(AssistantRootState)
    builder.add_node("reply", append_reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    graph = builder.compile()

    result = graph.invoke(
        {
            "messages": [HumanMessage(content="question")],
            "execution_mode": "fast",
        }
    )

    assert [message.content for message in result["messages"]] == [
        "question",
        "answer",
    ]


def test_root_input_rejects_unstructured_execution_mode() -> None:
    """Catches accepting a mode outside the product's structured enum."""

    with pytest.raises(ValidationError):
        AssistantRootInput(
            messages=[HumanMessage(content="question")],
            execution_mode="decide_from_text",
        )


def test_planning_state_uses_native_list_reducer_for_parallel_worker_results() -> None:
    """Catches losing either worker result when parallel branches update one channel."""

    def first(_state: PlanningState) -> dict[str, object]:
        return {
            "worker_results": [WorkerResult(work_item_id="node-a", content="a")]
        }

    def second(_state: PlanningState) -> dict[str, object]:
        return {
            "worker_results": [WorkerResult(work_item_id="node-b", content="b")]
        }

    builder = StateGraph(PlanningState)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge(START, "second")
    builder.add_edge("first", END)
    builder.add_edge("second", END)

    result = builder.compile().invoke(
        {"messages": [HumanMessage(content="question")], "memory_context": ()}
    )

    assert {item.work_item_id for item in result["worker_results"]} == {
        "node-a",
        "node-b",
    }


def test_run_context_rejects_unknown_runtime_objects() -> None:
    """Catches leaking clients or callbacks into the serializable run context."""

    with pytest.raises(ValidationError):
        AssistantRunContext(
            provider_client=object(),
        )
