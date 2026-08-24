from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent import planning_graph
from assistant_agent.native_agent.models import PlanningTodo, WorkerResult
from assistant_agent.native_agent.runtime_facts import capture_trusted_runtime_facts_node
from assistant_agent.skills.loading import SkillCatalog, SkillDescriptor


def _calls(*calls: tuple[str, dict[str, object], str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
            for name, args, call_id in calls
        ],
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (AIMessage(content="done"), "final"),
        (_calls(("write_todos", {"todos": []}, "write-1")), "controls"),
        (_calls(("load_skill", {"skill_id": "probe"}, "skill-1")), "controls"),
        (
            _calls(
                ("task", {"todo_id": "A"}, "task-A"),
                ("task", {"todo_id": "B"}, "task-B"),
            ),
            "tasks",
        ),
    ],
)
def test_supervisor_action_contract(message: AIMessage, expected: str) -> None:
    assert planning_graph.classify_supervisor_action(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        _calls(
            ("write_todos", {"todos": []}, "write-1"),
            ("task", {"todo_id": "A"}, "task-A"),
        ),
        _calls(
            ("task", {"todo_id": "A"}, "task-A1"),
            ("task", {"todo_id": "A"}, "task-A2"),
        ),
        _calls(("unknown", {}, "unknown-1")),
        _calls(
            ("task", {"todo_id": "A"}, "same-id"),
            ("task", {"todo_id": "B"}, "same-id"),
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"todo_id": "A"},
                    "id": "",
                    "type": "tool_call",
                }
            ],
        ),
    ],
)
def test_supervisor_action_rejects_ambiguous_or_unknown_calls(
    message: AIMessage,
) -> None:
    with pytest.raises(ValueError, match="invalid supervisor action"):
        planning_graph.classify_supervisor_action(message)


def test_todo_replacement_preserves_completed_work() -> None:
    current = [
        PlanningTodo(todo_id="A", content="done-A", status="completed"),
        PlanningTodo(todo_id="B", content="pending-B", status="pending"),
    ]
    replacement = [
        PlanningTodo(todo_id="A", content="done-A", status="completed"),
        PlanningTodo(todo_id="C", content="pending-C", status="pending"),
    ]

    assert planning_graph.replace_todos(current, replacement) == [
        item.model_dump(mode="json") for item in replacement
    ]

    with pytest.raises(ValueError, match="completed todo A cannot be removed"):
        planning_graph.replace_todos(current, replacement[1:])
    with pytest.raises(ValueError, match="completed todo A cannot be changed"):
        planning_graph.replace_todos(
            current,
            [
                PlanningTodo(todo_id="A", content="changed", status="completed"),
                replacement[1],
            ],
        )


def test_todo_replacement_is_bounded() -> None:
    with pytest.raises(ValueError, match="too many todos"):
        planning_graph.replace_todos(
            [],
            [
                PlanningTodo(todo_id=f"T{index}", content="bounded")
                for index in range(65)
            ],
        )


def test_only_join_can_mark_a_todo_completed() -> None:
    with pytest.raises(ValueError, match="can only be completed by join"):
        planning_graph.replace_todos(
            [],
            [PlanningTodo(todo_id="A", content="not-run", status="completed")],
        )
    with pytest.raises(ValueError, match="can only be completed by join"):
        planning_graph.replace_todos(
            [PlanningTodo(todo_id="A", content="pending")],
            [PlanningTodo(todo_id="A", content="pending", status="completed")],
        )


@pytest.mark.parametrize("status", ["completed", "missing"])
def test_dispatch_rejects_non_pending_todo(status: str) -> None:
    todos = (
        [{"todo_id": "A", "content": "done", "status": "completed"}]
        if status == "completed"
        else []
    )
    with pytest.raises(ValueError, match="task references non-pending todo A"):
        planning_graph.dispatch_tasks(
            {
                "messages": [_calls(("task", {"todo_id": "A"}, "task-A"))],
                "todos": todos,
            }
        )


def test_succeeded_worker_result_is_monotonic_but_blocked_can_be_retried() -> None:
    succeeded = WorkerResult(todo_id="A", status="succeeded", summary="first")
    assert planning_graph.merge_worker_results(
        {"A": succeeded}, {"A": succeeded}
    ) == {"A": succeeded.model_dump(mode="json")}

    with pytest.raises(ValueError, match="conflicting worker result A"):
        planning_graph.merge_worker_results(
            {"A": succeeded},
            {"A": WorkerResult(todo_id="A", status="blocked", summary="later")},
        )

    retried = WorkerResult(todo_id="B", status="succeeded", summary="recovered")
    assert planning_graph.merge_worker_results(
        {"B": WorkerResult(todo_id="B", status="blocked", summary="no data")},
        {"B": retried},
    ) == {"B": retried.model_dump(mode="json")}


def test_supervisor_projection_uses_transient_user_context_before_latest_request() -> None:
    internal_call = _calls(("write_todos", {"todos": []}, "write-internal"))
    parent_messages = [
        HumanMessage(content="old-user-sentinel"),
        AIMessage(content="old-assistant-sentinel"),
        internal_call,
        ToolMessage(
            content="internal-tool-sentinel",
            name="write_todos",
            tool_call_id="write-internal",
        ),
        HumanMessage(content="latest-user-sentinel"),
    ]
    messages = planning_graph._supervisor_messages(
        {
            "messages": parent_messages,
            "todos": [
                {
                    "todo_id": "todo-sentinel",
                    "content": "planning-work-sentinel",
                    "status": "pending",
                }
            ],
            "memory_context": ("memory-sentinel",),
            "memory_status": "ready",
            "trusted_runtime_facts": capture_trusted_runtime_facts_node({})[
                "trusted_runtime_facts"
            ],
        },
        AssistantRunContext(),
        SkillCatalog(),
    )

    assert isinstance(messages[0], SystemMessage)
    assert "planning-work-sentinel" not in str(messages[0].content)
    assert "memory-sentinel" not in str(messages[0].content)
    assert "用户默认地点" not in str(messages[0].content)
    assert "internal-tool-sentinel" not in str(messages)
    assert internal_call not in messages
    humans = [message for message in messages if isinstance(message, HumanMessage)]
    assert humans[-4].name == "planning_working_memory"
    assert humans[-4].additional_kwargs == {
        "assistant_agent_context": "planning_working_memory_v1"
    }
    assert [
        "planning-work-sentinel" in str(humans[-4].content),
        "memory-sentinel" in str(humans[-3].content),
        "用户默认地点" in str(humans[-2].content),
        humans[-1].content == "latest-user-sentinel",
    ] == [True, True, True, True]
    assert [
        message.content
        for message in parent_messages
        if isinstance(message, HumanMessage)
    ] == ["old-user-sentinel", "latest-user-sentinel"]


def test_supervisor_reprojects_only_observed_and_state_granted_reference(
    monkeypatch,
) -> None:
    descriptor = SkillDescriptor(
        name="reference-probe",
        description="probe",
        body="active-skill-sentinel",
        governed_tools=["probe_tool"],
        references={"details": "references/details.md"},
    )
    monkeypatch.setattr(
        planning_graph,
        "read_registered_skill_reference",
        lambda *_args, **_kwargs: "trusted-reference-sentinel",
    )
    state = {
        "messages": [
            HumanMessage(content="goal"),
            ToolMessage(
                content="loaded",
                artifact={
                    "skill_id": "reference-probe",
                    "reference_id": "details",
                },
                name="load_skill_reference",
                tool_call_id="reference-call",
            ),
        ]
    }
    untrusted = planning_graph._supervisor_messages(
        state,
        AssistantRunContext(),
        SkillCatalog(descriptors=[descriptor]),
    )
    assert "trusted-reference-sentinel" not in str(untrusted[0].content)

    state["active_skill_ids"] = ["reference-probe"]
    state["skill_reference_grants"] = {"reference-probe": ["details"]}
    messages = planning_graph._supervisor_messages(
        state, AssistantRunContext(), SkillCatalog(descriptors=[descriptor])
    )

    assert "active-skill-sentinel" not in str(messages[0].content)
    assert "trusted-reference-sentinel" not in str(messages[0].content)
    working_memory = json.loads(str(messages[-2].content))
    assert set(working_memory) == {
        "active_skills",
        "available_skills",
        "loaded_references",
        "todos",
        "worker_results",
    }
    assert working_memory["available_skills"] == [
        {"description": "probe", "skill_id": "reference-probe"}
    ]
    assert working_memory["active_skills"] == [
        {"content": "active-skill-sentinel", "skill_id": "reference-probe"}
    ]
    assert working_memory["loaded_references"][0]["content"] == (
        "trusted-reference-sentinel"
    )
    assert "reference-call" not in str(messages)
