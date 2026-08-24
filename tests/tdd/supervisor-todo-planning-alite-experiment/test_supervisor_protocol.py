from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from experiment_graph import (
    ExperimentPlanningState,
    WorkerResult,
    build_experiment_graph,
    classify_supervisor_action,
    create_write_todos_tool,
    merge_worker_results,
    replace_todos,
)
from probes import ScriptedSupervisor


def _initial_state(*, messages: list[object] | None = None) -> dict[str, object]:
    return {
        "messages": list(messages or ()),
        "todos": [],
        "worker_results": {},
        "worker_writes": [],
        "loaded_skills": [],
        "join_count": 0,
    }


def _calls(*calls: tuple[str, dict[str, object], str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
            for name, args, call_id in calls
        ],
    )


def test_completed_todo_and_result_are_monotonic() -> None:
    current = [
        {"todo_id": "A", "content": "alpha", "status": "completed"},
        {"todo_id": "B", "content": "beta", "status": "pending"},
    ]
    results = {
        "A": WorkerResult(todo_id="A", status="succeeded", summary="a-result")
    }

    updated = replace_todos(
        current,
        [
            {"todo_id": "A", "content": "alpha", "status": "completed"},
            {"todo_id": "C", "content": "gamma", "status": "pending"},
        ],
        worker_results=results,
    )

    assert [item["todo_id"] for item in updated] == ["A", "C"]
    assert updated[0]["status"] == "completed"


def test_completed_todo_cannot_be_removed_or_downgraded() -> None:
    current = [{"todo_id": "A", "content": "alpha", "status": "completed"}]
    results = {
        "A": WorkerResult(todo_id="A", status="succeeded", summary="a-result")
    }

    with pytest.raises(ValueError, match="completed todo A"):
        replace_todos(current, [], worker_results=results)
    with pytest.raises(ValueError, match="completed todo A"):
        replace_todos(
            current,
            [{"todo_id": "A", "content": "alpha", "status": "pending"}],
            worker_results=results,
        )


def test_replacement_rejects_duplicate_todo_ids() -> None:
    with pytest.raises(ValueError, match="duplicate todo A"):
        replace_todos(
            [],
            [
                {"todo_id": "A", "content": "one", "status": "pending"},
                {"todo_id": "A", "content": "two", "status": "pending"},
            ],
            worker_results={},
        )


def test_worker_result_merge_allows_blocked_retry_but_freezes_success() -> None:
    success = {"A": WorkerResult(todo_id="A", status="succeeded", summary="one")}
    same = {"A": WorkerResult(todo_id="A", status="succeeded", summary="one")}
    conflict = {"A": WorkerResult(todo_id="A", status="succeeded", summary="two")}
    blocked = {"B": WorkerResult(todo_id="B", status="blocked", summary="blocked")}
    retried = {"B": WorkerResult(todo_id="B", status="succeeded", summary="done")}

    assert merge_worker_results(success, same) == success
    assert merge_worker_results(blocked, retried) == retried
    with pytest.raises(ValueError, match="conflicting worker result A"):
        merge_worker_results(success, conflict)


def test_write_todos_runs_through_standard_tool_node() -> None:
    tool = create_write_todos_tool()
    assert "runtime" not in tool.tool_call_schema.model_json_schema()["properties"]
    builder = StateGraph(ExperimentPlanningState)
    builder.add_node("controls", ToolNode([tool]))
    builder.add_edge(START, "controls")
    builder.add_edge("controls", END)
    graph = builder.compile()
    call_id = "write-todos-call"

    result = graph.invoke(
        _initial_state(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_todos",
                            "args": {
                                "todos": [
                                    {
                                        "todo_id": "A",
                                        "content": "alpha",
                                        "status": "pending",
                                    }
                                ]
                            },
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
    )

    assert result["todos"] == [
        {"todo_id": "A", "content": "alpha", "status": "pending"}
    ]
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert message.name == "write_todos"
    assert message.tool_call_id == call_id


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (AIMessage(content="done"), "final"),
        (_calls(("write_todos", {"todos": []}, "write-1")), "controls"),
        (_calls(("task", {"todo_id": "A"}, "task-a")), "tasks"),
        (
            _calls(
                ("task", {"todo_id": "A"}, "task-a"),
                ("task", {"todo_id": "B"}, "task-b"),
            ),
            "tasks",
        ),
    ],
)
def test_supervisor_action_classification(
    message: AIMessage,
    expected: str,
) -> None:
    assert classify_supervisor_action(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        _calls(
            ("write_todos", {"todos": []}, "write-1"),
            ("task", {"todo_id": "A"}, "task-a"),
        ),
        _calls(("write_todos", {"todos": []}, "write-1"), ("write_todos", {"todos": []}, "write-2")),
        _calls(("unknown", {}, "unknown-1")),
        _calls(
            ("task", {"todo_id": "A"}, "task-a-1"),
            ("task", {"todo_id": "A"}, "task-a-2"),
        ),
    ],
)
def test_supervisor_action_rejects_ambiguous_calls(message: AIMessage) -> None:
    with pytest.raises(ValueError, match="supervisor action"):
        classify_supervisor_action(message)


def test_supervisor_controls_then_finishes_without_create_agent() -> None:
    supervisor = ScriptedSupervisor(
        [
            _calls(
                (
                    "write_todos",
                    {
                        "todos": [
                            {
                                "todo_id": "A",
                                "content": "alpha",
                                "status": "pending",
                            }
                        ]
                    },
                    "write-1",
                )
            ),
            AIMessage(content="final-sentinel"),
        ]
    )

    result = build_experiment_graph(supervisor).invoke(_initial_state())

    assert [item["todo_id"] for item in result["todos"]] == ["A"]
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].tool_calls == []
    assert supervisor.bound_tool_names == {"task", "write_todos"}
    assert supervisor.calls == 2
    assert supervisor.create_agent_calls == 0
