from __future__ import annotations

import json
import operator
from collections.abc import Sequence
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, ToolRuntime
from langgraph.types import Command, Overwrite, Send


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


class WorkerInput(TypedDict):
    todo_id: str
    content: str
    task_call_id: str
    loaded_skills: tuple[str, ...]
    trusted_context: dict[str, str]


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


def create_task_tool() -> BaseTool:
    @tool("task")
    def task(todo_id: str) -> str:
        """Delegate exactly one existing pending Todo to a Worker."""

        del todo_id
        raise AssertionError("task is a graph routing protocol, not an executable tool")

    return task


def classify_supervisor_action(
    message: AIMessage,
) -> Literal["controls", "tasks", "final"]:
    calls = list(message.tool_calls)
    if not calls:
        return "final"
    names = [call["name"] for call in calls]
    if len(calls) == 1 and names == ["write_todos"]:
        return "controls"
    if all(name == "task" for name in names):
        todo_ids: list[str] = []
        for call in calls:
            todo_id = call["args"].get("todo_id")
            if not isinstance(todo_id, str) or not todo_id:
                raise ValueError("invalid supervisor action: empty task todo_id")
            todo_ids.append(todo_id)
        if len(todo_ids) != len(set(todo_ids)):
            raise ValueError("invalid supervisor action: duplicate task todo_id")
        return "tasks"
    raise ValueError("invalid supervisor action: mixed or unknown tool calls")


def _last_ai_message(state: ExperimentPlanningState) -> AIMessage:
    message = state["messages"][-1]
    if not isinstance(message, AIMessage):
        raise ValueError("supervisor action requires a terminal AIMessage")
    return message


def dispatch_tasks(state: ExperimentPlanningState) -> list[Send]:
    message = _last_ai_message(state)
    pending = {
        item["todo_id"]: item
        for item in state["todos"]
        if item["status"] == "pending"
    }
    sends: list[Send] = []
    for call in message.tool_calls:
        todo_id = str(call["args"]["todo_id"])
        if todo_id not in pending:
            raise ValueError(f"task references non-pending todo {todo_id}")
        sends.append(
            Send(
                "worker",
                WorkerInput(
                    todo_id=todo_id,
                    content=pending[todo_id]["content"],
                    task_call_id=call["id"],
                    loaded_skills=tuple(state.get("loaded_skills", ())),
                    trusted_context=dict(state.get("trusted_context", {})),
                ),
            )
        )
    return sends


def _validated_worker_result(value: object) -> WorkerResult:
    if not isinstance(value, dict):
        raise TypeError("worker structured response must be an object")
    todo_id = value.get("todo_id")
    status = value.get("status")
    summary = value.get("summary")
    if not isinstance(todo_id, str) or not todo_id:
        raise ValueError("worker result requires todo_id")
    if status not in {"succeeded", "blocked"}:
        raise ValueError("worker result has invalid status")
    if not isinstance(summary, str) or not summary:
        raise ValueError("worker result requires summary")
    return WorkerResult(todo_id=todo_id, status=status, summary=summary)


def join_workers(state: ExperimentPlanningState) -> dict[str, object]:
    writes = sorted(
        state["worker_writes"],
        key=lambda item: item["result"]["todo_id"],
    )
    results = {item["result"]["todo_id"]: item["result"] for item in writes}
    completed = {
        todo_id
        for todo_id, result in results.items()
        if result["status"] == "succeeded"
    }
    todos: list[PlanningTodo] = [
        PlanningTodo(
            todo_id=item["todo_id"],
            content=item["content"],
            status=(
                "completed" if item["todo_id"] in completed else item["status"]
            ),
        )
        for item in state["todos"]
    ]
    messages = [
        ToolMessage(
            content=json.dumps(item["result"], sort_keys=True),
            name="task",
            tool_call_id=item["task_call_id"],
        )
        for item in writes
    ]
    return {
        "todos": todos,
        "worker_results": results,
        "worker_writes": Overwrite([]),
        "messages": messages,
        "join_count": state.get("join_count", 0) + 1,
    }


def build_experiment_graph(
    supervisor_model: Any,
    worker_model: Any | None = None,
    *,
    read_probe_tool: BaseTool | None = None,
    checkpointer: Any | None = None,
):
    write_todos = create_write_todos_tool()
    task = create_task_tool()
    bound_supervisor = supervisor_model.bind_tools([write_todos, task])
    worker_agent = (
        create_agent(
            model=worker_model,
            tools=[read_probe_tool] if read_probe_tool is not None else [],
            response_format=ToolStrategy(WorkerResult),
            name="planning_worker_experiment",
        )
        if worker_model is not None
        else None
    )

    def supervisor_node(state: ExperimentPlanningState) -> dict[str, Sequence[AIMessage]]:
        response = bound_supervisor.invoke(state["messages"])
        if not isinstance(response, AIMessage):
            raise TypeError("supervisor model must return AIMessage")
        return {"messages": [response]}

    def route_after_supervisor(state: ExperimentPlanningState):
        action = classify_supervisor_action(_last_ai_message(state))
        if action == "controls":
            return "controls"
        if action == "final":
            return END
        return dispatch_tasks(state)

    async def worker_wrapper(worker_input: WorkerInput) -> dict[str, object]:
        if worker_agent is None:
            raise RuntimeError("worker agent is not configured")
        private_payload = {
            "todo_id": worker_input["todo_id"],
            "content": worker_input["content"],
            "loaded_skills": list(worker_input["loaded_skills"]),
            "trusted_context": worker_input["trusted_context"],
        }
        result = await worker_agent.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content=json.dumps(private_payload, sort_keys=True)
                    )
                ]
            }
        )
        worker_result = _validated_worker_result(result.get("structured_response"))
        if worker_result["todo_id"] != worker_input["todo_id"]:
            raise ValueError("worker result todo_id mismatch")
        return {
            "worker_writes": [
                WorkerWrite(
                    task_call_id=worker_input["task_call_id"],
                    result=worker_result,
                )
            ]
        }

    builder = StateGraph(ExperimentPlanningState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("controls", ToolNode([write_todos]))
    if worker_agent is not None:
        builder.add_node("worker", worker_wrapper)
        builder.add_node("join", join_workers)
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", route_after_supervisor)
    builder.add_edge("controls", "supervisor")
    if worker_agent is not None:
        builder.add_edge("worker", "join")
        builder.add_edge("join", "supervisor")
    return builder.compile(checkpointer=checkpointer)
