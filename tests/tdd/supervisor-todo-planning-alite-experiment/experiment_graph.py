from __future__ import annotations

import json
import operator
from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import MessagesState
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command


class PlanningTodo(TypedDict):
    todo_id: str
    content: str
    status: Literal["pending", "completed"]


class WorkerResult(TypedDict):
    todo_id: str
    status: Literal["succeeded", "blocked"]
    summary: str


class WorkerWrite(TypedDict):
    task_call_id: str
    result: WorkerResult


def merge_worker_results(
    left: dict[str, WorkerResult] | None,
    right: dict[str, WorkerResult] | None,
) -> dict[str, WorkerResult]:
    merged = dict(left or {})
    for todo_id, result in (right or {}).items():
        previous = merged.get(todo_id)
        if (
            previous is not None
            and previous["status"] == "succeeded"
            and previous != result
        ):
            raise ValueError(f"conflicting worker result {todo_id}")
        merged[todo_id] = result
    return merged


class ExperimentPlanningState(MessagesState):
    todos: list[PlanningTodo]
    worker_results: Annotated[dict[str, WorkerResult], merge_worker_results]
    worker_writes: Annotated[list[WorkerWrite], operator.add]
    loaded_skills: list[str]
    trusted_context: NotRequired[dict[str, str]]
    join_count: int


def replace_todos(
    current: list[PlanningTodo],
    replacement: list[PlanningTodo],
    *,
    worker_results: dict[str, WorkerResult],
) -> list[PlanningTodo]:
    del worker_results
    replacement_by_id: dict[str, PlanningTodo] = {}
    for item in replacement:
        todo_id = item["todo_id"]
        if todo_id in replacement_by_id:
            raise ValueError(f"duplicate todo {todo_id}")
        replacement_by_id[todo_id] = item

    for item in current:
        if item["status"] != "completed":
            continue
        todo_id = item["todo_id"]
        candidate = replacement_by_id.get(todo_id)
        if candidate is None:
            raise ValueError(f"completed todo {todo_id} cannot be removed")
        if candidate["status"] != "completed" or candidate["content"] != item["content"]:
            raise ValueError(f"completed todo {todo_id} cannot be changed")
    return list(replacement)


def create_write_todos_tool() -> BaseTool:
    @tool("write_todos")
    def write_todos(
        todos: list[PlanningTodo],
        runtime: ToolRuntime,
    ) -> Command:
        """Replace pending todos while preserving completed work."""

        updated = replace_todos(
            list(runtime.state.get("todos", ())),
            todos,
            worker_results=dict(runtime.state.get("worker_results", {})),
        )
        return Command(
            update={
                "todos": updated,
                "messages": [
                    ToolMessage(
                        content=json.dumps(
                            {
                                "updated_todo_ids": [
                                    item["todo_id"] for item in updated
                                ]
                            },
                            sort_keys=True,
                        ),
                        name="write_todos",
                        tool_call_id=runtime.tool_call_id or "missing-tool-call-id",
                    )
                ],
            }
        )

    return write_todos
