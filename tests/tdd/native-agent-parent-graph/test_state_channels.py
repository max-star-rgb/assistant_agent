"""RED/GREEN coverage for the native parent graph state boundary."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import PlanningArtifact, WorkerResult
from assistant_agent.native_agent.state import (
    AssistantRootInput,
    AssistantRootState,
    PlanningState,
    merge_artifacts,
    merge_sorted_ids,
    merge_worker_results,
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


def test_worker_result_merge_is_idempotent_for_equal_content() -> None:
    result = WorkerResult(work_item_id="node-a", content="same")

    merged = merge_worker_results({"node-a": result}, {"node-a": result})

    assert merged == {"node-a": result}


def test_worker_result_conflict_fails_closed() -> None:
    """Catches silently overwriting parallel results with the same stable ID."""

    left = {"node-a": WorkerResult(work_item_id="node-a", content="v1")}
    right = {"node-a": WorkerResult(work_item_id="node-a", content="v2")}

    with pytest.raises(ValueError, match="worker result conflict: node-a"):
        merge_worker_results(left, right)


def test_artifact_conflict_fails_closed() -> None:
    left = {"artifact-a": PlanningArtifact(artifact_id="artifact-a", content="v1")}
    right = {"artifact-a": PlanningArtifact(artifact_id="artifact-a", content="v2")}

    with pytest.raises(ValueError, match="artifact conflict: artifact-a"):
        merge_artifacts(left, right)


def test_completed_ids_merge_to_deterministic_order() -> None:
    assert merge_sorted_ids(("node-b", "node-a"), ("node-c", "node-a")) == (
        "node-a",
        "node-b",
        "node-c",
    )


def test_planning_state_binds_worker_result_reducer_to_graph_channel() -> None:
    """Catches declaring the merge function without wiring it to StateGraph."""

    def first(_state: PlanningState) -> dict[str, object]:
        return {
            "worker_results": {
                "node-a": WorkerResult(work_item_id="node-a", content="a")
            }
        }

    def second(_state: PlanningState) -> dict[str, object]:
        return {
            "worker_results": {
                "node-b": WorkerResult(work_item_id="node-b", content="b")
            }
        }

    builder = StateGraph(PlanningState)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)

    result = builder.compile().invoke(
        {"messages": [HumanMessage(content="question")], "memory_context": ()}
    )

    assert set(result["worker_results"]) == {"node-a", "node-b"}


def test_run_context_rejects_unknown_runtime_objects() -> None:
    """Catches leaking clients or callbacks into the serializable run context."""

    with pytest.raises(ValidationError):
        AssistantRunContext(
            user_id="user-1",
            tenant_id="tenant-1",
            provider_client=object(),
        )
