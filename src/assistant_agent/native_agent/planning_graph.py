"""Supervisor-driven Todo planning graph built from native LangGraph primitives."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from langchain.agents.middleware.todo import WRITE_TODOS_SYSTEM_PROMPT
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, ToolRuntime
from langgraph.runtime import Runtime
from langgraph.types import Command, Overwrite, Send
from pydantic import Field

from assistant_agent.native_agent.context import (
    PLANNING_WORKING_MEMORY_MARKER_KEY,
    PLANNING_WORKING_MEMORY_MARKER_VALUE,
    PLANNING_WORKING_MEMORY_MESSAGE_NAME,
    AssistantRunContext,
)
from assistant_agent.native_agent.fast_agent import memory_context_message
from assistant_agent.native_agent.models import PlanningTodo, WorkerResult, WorkerWrite
from assistant_agent.native_agent.providers import planning_supervisor_model_view
from assistant_agent.native_agent.runtime_facts import trusted_runtime_facts_message
from assistant_agent.native_agent.state import (
    PlanningState,
    WorkerState,
    merge_worker_results,
)
from assistant_agent.skills.loading import (
    SkillCatalog,
    default_repo_root,
    load_repo_skill_descriptors,
    read_registered_skill_reference,
)
from assistant_agent.native_agent.tool_exposure import discoverable_skill_descriptors
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)


_CONTROL_TOOL_NAMES = frozenset(
    {LOAD_SKILL_TOOL_NAME, LOAD_SKILL_REFERENCE_TOOL_NAME, "write_todos"}
)
_SUPERVISOR_CONTEXT_LIMIT = 48_000
_SUPERVISOR_PARENT_TOKEN_LIMIT = 16_000
_REFERENCE_CONTEXT_LIMIT = 40_000
_MAX_TODOS = 64


def replace_todos(
    current: Sequence[PlanningTodo | Mapping[str, object]],
    replacement: Sequence[PlanningTodo | Mapping[str, object]],
) -> list[dict[str, object]]:
    """Replace future work without silently rewriting completed work."""

    current_items = [PlanningTodo.model_validate(item) for item in current]
    replacement_items = [PlanningTodo.model_validate(item) for item in replacement]
    if len(replacement_items) > _MAX_TODOS:
        raise ValueError(f"too many todos: maximum is {_MAX_TODOS}")
    by_id: dict[str, PlanningTodo] = {}
    for item in replacement_items:
        if item.todo_id in by_id:
            raise ValueError(f"duplicate todo {item.todo_id}")
        by_id[item.todo_id] = item
    current_by_id = {item.todo_id: item for item in current_items}
    for item in replacement_items:
        previous = current_by_id.get(item.todo_id)
        if item.status == "completed" and (
            previous is None or previous.status != "completed"
        ):
            raise ValueError(f"todo {item.todo_id} can only be completed by join")
    for item in current_items:
        if item.status != "completed":
            continue
        candidate = by_id.get(item.todo_id)
        if candidate is None:
            raise ValueError(f"completed todo {item.todo_id} cannot be removed")
        if candidate != item:
            raise ValueError(f"completed todo {item.todo_id} cannot be changed")
    return [item.model_dump(mode="json") for item in replacement_items]


def create_write_todos_tool() -> BaseTool:
    """Create the standard control Tool that owns Todo replacement."""

    @tool("write_todos")
    def write_todos(
        todos: Annotated[list[PlanningTodo], Field(max_length=_MAX_TODOS)],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> Command:
        """替换待办事项列表，同时原样保留所有已完成的待办事项。"""

        updated = replace_todos(runtime.state.get("todos", ()), todos)
        retained_ids = {str(item["todo_id"]) for item in updated}
        current_todos = {
            item.todo_id: item
            for raw in runtime.state.get("todos", ())
            for item in (PlanningTodo.model_validate(raw),)
        }
        updated_todos = {
            item.todo_id: item
            for raw in updated
            for item in (PlanningTodo.model_validate(raw),)
        }
        retained_results = {
            todo_id: result
            for todo_id, result in runtime.state.get("worker_results", {}).items()
            if todo_id in retained_ids
            and (current := current_todos.get(todo_id)) is not None
            and current == updated_todos[todo_id]
        }
        return Command(
            update={
                "todos": updated,
                "worker_results": Overwrite(retained_results),
                "messages": [
                    ToolMessage(
                        content=json.dumps(
                            {"updated_todo_ids": [str(item["todo_id"]) for item in updated]},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        name="write_todos",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    write_todos.metadata = {"effect": "read", "source": "builtin"}
    return write_todos


def create_task_tool() -> BaseTool:
    """Create only the model-visible schema for dynamic Worker delegation."""

    @tool("task")
    def task(todo_id: str) -> str:
        """将一个现有的待处理 Todo 委派给 Worker 执行。"""

        del todo_id
        raise AssertionError("task is a graph routing protocol, not an executable tool")

    task.metadata = {"effect": "read", "source": "builtin"}
    return task


def classify_supervisor_action(
    message: AIMessage,
) -> Literal["controls", "tasks", "final"]:
    """Classify one unambiguous Supervisor action and fail closed otherwise."""

    calls = list(message.tool_calls)
    if not calls:
        return "final"
    call_ids = [call.get("id") for call in calls]
    if any(not isinstance(call_id, str) or not call_id for call_id in call_ids):
        raise ValueError("invalid supervisor action: empty tool_call_id")
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("invalid supervisor action: duplicate tool_call_id")
    names = [str(call.get("name", "")) for call in calls]
    if len(calls) == 1 and names[0] in _CONTROL_TOOL_NAMES:
        return "controls"
    if calls and all(name == "task" for name in names):
        todo_ids: list[str] = []
        for call in calls:
            args = call.get("args")
            todo_id = args.get("todo_id") if isinstance(args, Mapping) else None
            if not isinstance(todo_id, str) or not todo_id:
                raise ValueError("invalid supervisor action: empty task todo_id")
            todo_ids.append(todo_id)
        if len(todo_ids) != len(set(todo_ids)):
            raise ValueError("invalid supervisor action: duplicate task todo_id")
        return "tasks"
    raise ValueError("invalid supervisor action: mixed or unknown tool calls")


def dispatch_tasks(state: PlanningState) -> list[Send]:
    """Map the latest pure task action directly to native Worker Sends."""

    message = _last_supervisor_message(state)
    if classify_supervisor_action(message) != "tasks":
        raise ValueError("task dispatch requires a pure task action")
    pending = {
        item.todo_id: item
        for raw in state.get("todos", ())
        if (item := PlanningTodo.model_validate(raw)).status == "pending"
    }
    loaded_references = _loaded_skill_references(state.get("messages", ()))
    active_skill_ids = set(state.get("active_skill_ids", ()))
    grants = {
        skill_id: [
            reference_id
            for reference_id in reference_ids
            if (skill_id, reference_id) in loaded_references
        ]
        for skill_id, reference_ids in state.get("skill_reference_grants", {}).items()
        if skill_id in active_skill_ids
    }
    sends: list[Send] = []
    for call in message.tool_calls:
        todo_id = str(call["args"]["todo_id"])
        todo = pending.get(todo_id)
        if todo is None:
            raise ValueError(f"task references non-pending todo {todo_id}")
        sends.append(
            Send(
                "worker",
                WorkerState(
                    todo_id=todo.todo_id,
                    content=todo.content,
                    task_call_id=str(call["id"]),
                    memory_context=tuple(state.get("memory_context", ())),
                    memory_status=state.get("memory_status", "empty"),
                    trusted_runtime_facts=state.get("trusted_runtime_facts", {}),
                    active_skill_ids=list(state.get("active_skill_ids", ())),
                    skill_reference_grants=grants,
                ),
            )
        )
    return sends


def join_workers(state: PlanningState) -> dict[str, object]:
    """Commit one complete wave and return its observations to the Supervisor."""

    writes = sorted(
        (WorkerWrite.model_validate(item) for item in state.get("worker_writes", ())),
        key=lambda item: item.result.todo_id,
    )
    additions = {
        item.result.todo_id: item.result.model_dump(mode="json") for item in writes
    }
    completed_ids = {
        todo_id for todo_id, result in additions.items() if result["status"] == "succeeded"
    }
    todos = [
        item.model_copy(update={"status": "completed"}).model_dump(mode="json")
        if item.todo_id in completed_ids
        else item.model_dump(mode="json")
        for raw in state.get("todos", ())
        for item in (PlanningTodo.model_validate(raw),)
    ]
    messages = [
        ToolMessage(
            content=item.result.model_dump_json(),
            name="task",
            tool_call_id=item.task_call_id,
        )
        for item in writes
    ]
    return {
        "todos": todos,
        "worker_results": additions,
        "worker_writes": Overwrite([]),
        "messages": messages,
    }


def build_planning_graph(
    model: BaseChatModel,
    fast_agent: Any,
    *,
    tools: Sequence[BaseTool] = (),
    skill_catalog: SkillCatalog | None = None,
):
    """Build the production Supervisor/Controls/Worker/Join planning loop."""

    catalog = (
        skill_catalog
        if skill_catalog is not None
        else load_repo_skill_descriptors(default_repo_root())
    )
    tool_by_name = {item.name: item for item in tools}
    duplicate_names = {
        name for name, count in Counter(item.name for item in tools).items() if count > 1
    }
    if duplicate_names:
        raise ValueError(f"duplicate production Tool names: {sorted(duplicate_names)}")
    required_controls = {LOAD_SKILL_TOOL_NAME, LOAD_SKILL_REFERENCE_TOOL_NAME}
    missing = sorted(required_controls - tool_by_name.keys())
    if missing:
        raise ValueError(f"planning controls are missing: {missing}")
    write_todos = create_write_todos_tool()
    task = create_task_tool()
    controls = [
        tool_by_name[LOAD_SKILL_TOOL_NAME],
        tool_by_name[LOAD_SKILL_REFERENCE_TOOL_NAME],
        write_todos,
    ]
    supervisor_model = planning_supervisor_model_view(model).bind_tools(
        [*controls, task]
    )

    async def supervisor_anode(
        state: PlanningState,
        runtime: Runtime[AssistantRunContext],
    ) -> dict[str, list[AIMessage]]:
        response = await supervisor_model.ainvoke(
            _supervisor_messages(
                state, runtime.context or AssistantRunContext(), catalog
            )
        )
        return {"messages": [_require_ai_message(response)]}

    def route_after_supervisor(state: PlanningState):
        action = classify_supervisor_action(_last_supervisor_message(state))
        if action == "controls":
            return "controls"
        if action == "final":
            return END
        return dispatch_tasks(state)

    async def worker_anode(
        worker_state: WorkerState,
        runtime: Runtime[AssistantRunContext],
    ) -> dict[str, list[dict[str, object]]]:
        result = await fast_agent.ainvoke(
            _worker_agent_input(worker_state, catalog),
            context=runtime.context or AssistantRunContext(),
        )
        return _worker_update(worker_state, result)

    builder = StateGraph(PlanningState, context_schema=AssistantRunContext)
    builder.add_node("supervisor", supervisor_anode)
    builder.add_node("controls", ToolNode(controls))
    builder.add_node("worker", worker_anode)
    builder.add_node("join", join_workers)
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        ["controls", "worker", END],
    )
    builder.add_edge("controls", "supervisor")
    builder.add_edge("worker", "join")
    builder.add_edge("join", "supervisor")
    return builder.compile(name="AssistantPlanningGraph")


def _supervisor_messages(
    state: PlanningState,
    context: AssistantRunContext,
    catalog: SkillCatalog,
) -> list[Any]:
    payload = {
        "todos": [
            PlanningTodo.model_validate(item).model_dump(mode="json")
            for item in state.get("todos", ())
        ],
        "worker_results": {
            todo_id: WorkerResult.model_validate(result).model_dump(mode="json")
            for todo_id, result in state.get("worker_results", {}).items()
        },
        "available_skills": [
            {"skill_id": descriptor.name, "description": descriptor.description}
            for descriptor in discoverable_skill_descriptors(catalog)
        ],
        "active_skills": _read_active_skill_payload(state, catalog),
        "loaded_references": _read_reference_payload(
            _authorized_loaded_skill_references(state), catalog
        ),
    }
    working_memory = _render_working_memory(payload)
    parent_messages = _bounded_parent_messages(state)
    transient_context = [
        HumanMessage(
            content=working_memory,
            name=PLANNING_WORKING_MEMORY_MESSAGE_NAME,
            additional_kwargs={
                PLANNING_WORKING_MEMORY_MARKER_KEY: (
                    PLANNING_WORKING_MEMORY_MARKER_VALUE
                ),
            },
        ),
        memory_context_message(tuple(state.get("memory_context", ()))),
        trusted_runtime_facts_message(state.get("trusted_runtime_facts")),
    ]
    return [
        _planning_system_message(state, context, catalog),
        *_insert_before_latest_human(
            parent_messages,
            [message for message in transient_context if message is not None],
        ),
    ]


def _planning_system_message(
    _state: PlanningState,
    _context: AssistantRunContext,
    _catalog: SkillCatalog,
) -> SystemMessage:
    """Build the planning prompt afresh for each Supervisor model call."""

    return SystemMessage(content=WRITE_TODOS_SYSTEM_PROMPT)


def _insert_before_latest_human(
    messages: Sequence[Any], transient: Sequence[HumanMessage]
) -> list[Any]:
    latest_human_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], HumanMessage)
        ),
        None,
    )
    if latest_human_index is None:
        return list(messages)
    return [
        *messages[:latest_human_index],
        *transient,
        *messages[latest_human_index:],
    ]


def _bounded_parent_messages(state: PlanningState) -> list[Any]:
    """Keep user dialogue bounded and exclude internal planning transcripts."""

    conversation = [
        message
        for message in state.get("messages", ())
        if isinstance(message, HumanMessage)
        or isinstance(message, AIMessage) and not message.tool_calls
    ]
    return trim_messages(
        conversation,
        max_tokens=_SUPERVISOR_PARENT_TOKEN_LIMIT,
        token_counter="approximate",
        strategy="last",
        allow_partial=True,
        start_on=HumanMessage,
    )


def _render_working_memory(payload: Mapping[str, object]) -> str:
    """Render bounded, valid JSON instead of truncating serialized state."""

    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(rendered) <= _SUPERVISOR_CONTEXT_LIMIT:
        return rendered
    compact = {
        "todos": [
            {**item, "content": str(item["content"])[:96]}
            for item in payload.get("todos", [])
            if isinstance(item, Mapping)
        ],
        "worker_results": {
            todo_id: {**result, "summary": str(result["summary"])[:96]}
            for todo_id, result in payload.get("worker_results", {}).items()
            if isinstance(result, Mapping)
        },
        "available_skills": [
            {
                "skill_id": str(item["skill_id"]),
                "description": str(item["description"])[:512],
            }
            for item in payload.get("available_skills", [])
            if isinstance(item, Mapping)
        ],
        "active_skills": [
            {**item, "content": str(item["content"])[:512]}
            for item in payload.get("active_skills", [])
            if isinstance(item, Mapping)
        ],
        "loaded_references": [
            {**item, "content": str(item["content"])[:512]}
            for item in payload.get("loaded_references", [])
            if isinstance(item, Mapping)
        ],
        "content_truncated": True,
    }
    rendered = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    if len(rendered) > _SUPERVISOR_CONTEXT_LIMIT:
        raise ValueError("planning working memory exceeds supervisor context limit")
    return rendered


def _worker_agent_input(
    state: WorkerState,
    catalog: SkillCatalog,
) -> dict[str, object]:
    references = _read_reference_payload(
        {
            (skill_id, reference_id)
            for skill_id, reference_ids in state.get(
                "skill_reference_grants", {}
            ).items()
            for reference_id in reference_ids
        },
        catalog,
    )
    payload = {
        "todo_id": state["todo_id"],
        "content": state["content"],
        "loaded_references": references,
        "instruction": (
            "只执行当前 Todo。正常完成时返回 succeeded；业务信息不足或无法继续时返回 blocked。"
            "不要处理其他 Todo，也不要披露内部 planning 上下文。"
        ),
    }
    agent_input: dict[str, object] = {
        "messages": [
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, sort_keys=True))
        ],
        "memory_context": tuple(state.get("memory_context", ())),
        "memory_status": state.get("memory_status", "empty"),
        "execution_mode": "planning",
        "agent_phase": "worker",
        "provider_search_profile": "none",
        "active_skill_ids": list(state.get("active_skill_ids", ())),
        "skill_reference_grants": dict(state.get("skill_reference_grants", {})),
    }
    trusted_facts = state.get("trusted_runtime_facts")
    if trusted_facts:
        agent_input["trusted_runtime_facts"] = dict(trusted_facts)
    return agent_input


def _worker_update(
    worker_state: WorkerState,
    result: Mapping[str, object],
) -> dict[str, list[dict[str, object]]]:
    response = WorkerResult.model_validate(result.get("structured_response"))
    if response.todo_id != worker_state["todo_id"]:
        raise ValueError("worker result todo_id mismatch")
    return {
        "worker_writes": [
            WorkerWrite(
                task_call_id=worker_state["task_call_id"], result=response
            ).model_dump(mode="json")
        ]
    }


def _last_supervisor_message(state: PlanningState) -> AIMessage:
    messages = state.get("messages", ())
    if not messages or not isinstance(messages[-1], AIMessage):
        raise ValueError("supervisor action requires a terminal AIMessage")
    return messages[-1]


def _require_ai_message(value: object) -> AIMessage:
    if not isinstance(value, AIMessage):
        raise TypeError("supervisor model must return AIMessage")
    return value


def _loaded_skill_references(messages: Sequence[object]) -> set[tuple[str, str]]:
    loaded: set[tuple[str, str]] = set()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        if message.name != LOAD_SKILL_REFERENCE_TOOL_NAME or message.status == "error":
            continue
        artifact = message.artifact
        if not isinstance(artifact, Mapping):
            continue
        skill_id = artifact.get("skill_id")
        reference_id = artifact.get("reference_id")
        if isinstance(skill_id, str) and isinstance(reference_id, str):
            loaded.add((skill_id, reference_id))
    return loaded


def _authorized_loaded_skill_references(
    state: PlanningState,
) -> set[tuple[str, str]]:
    """Intersect successful observations with state written by load_skill."""

    observed = _loaded_skill_references(state.get("messages", ()))
    active = set(state.get("active_skill_ids", ()))
    grants = state.get("skill_reference_grants", {})
    return {
        (skill_id, reference_id)
        for skill_id, reference_id in observed
        if skill_id in active and reference_id in grants.get(skill_id, ())
    }


def _read_active_skill_payload(
    state: PlanningState,
    catalog: SkillCatalog,
) -> list[dict[str, str]]:
    """Re-read active Skill instructions from the trusted catalog."""

    descriptors = {item.name: item for item in catalog.descriptors}
    payload: list[dict[str, str]] = []
    for skill_id in state.get("active_skill_ids", ()):
        descriptor = descriptors.get(skill_id)
        if descriptor is None:
            raise ValueError(f"active skill is no longer registered: {skill_id}")
        payload.append({"skill_id": skill_id, "content": descriptor.body})
    return payload


def _read_reference_payload(
    loaded: set[tuple[str, str]],
    catalog: SkillCatalog,
) -> list[dict[str, str]]:
    """Re-read only successfully loaded references into one bounded context."""

    descriptors = {item.name: item for item in catalog.descriptors}
    payload: list[dict[str, str]] = []
    total_chars = 0
    for skill_id, reference_id in sorted(loaded):
        descriptor = descriptors.get(skill_id)
        if descriptor is None:
            raise ValueError(f"loaded skill is no longer registered: {skill_id}")
        content = read_registered_skill_reference(
            default_repo_root(), descriptor, reference_id
        )
        if content is None:
            raise ValueError(
                f"loaded skill reference is no longer available: {skill_id}/{reference_id}"
            )
        total_chars += len(content)
        if total_chars > _REFERENCE_CONTEXT_LIMIT:
            raise ValueError("loaded skill reference context exceeds planning limit")
        payload.append(
            {
                "skill_id": skill_id,
                "reference_id": reference_id,
                "content": content,
            }
        )
    return payload


__all__ = [
    "build_planning_graph",
    "classify_supervisor_action",
    "create_task_tool",
    "create_write_todos_tool",
    "dispatch_tasks",
    "join_workers",
    "merge_worker_results",
    "replace_todos",
]
