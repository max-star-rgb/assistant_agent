from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from showcases.studio_hitl_extension.graph import build_graph


def test_single_action_interrupt_exposes_typed_args_and_accepts_edit() -> None:
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "single-hitl"}}

    interrupted = graph.invoke({"scenario": "single"}, config=config)
    request = interrupted["__interrupt__"][0].value

    assert [action["name"] for action in request["action_requests"]] == [
        "execute"
    ]
    assert request["action_requests"][0]["args"] == {
        "command": "python -m pytest -q",
        "timeout_seconds": 30,
        "enabled": True,
        "env": {"MODE": "mock"},
        "paths": ["tests/core"],
    }
    assert request["review_configs"][0]["args_schema"]["properties"][
        "command"
    ]["enum"] == ["python -m pytest -q", "python -m compileall -q src"]

    edited_args = {
        "command": "python -m compileall -q src",
        "timeout_seconds": 45,
        "enabled": False,
        "env": {"MODE": "mock"},
        "paths": ["src"],
    }
    resumed = graph.invoke(
        Command(
            resume={
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {
                            "name": "execute",
                            "args": edited_args,
                        },
                    }
                ]
            }
        ),
        config=config,
    )

    assert resumed["result"] == [
        {"name": "execute", "decision": "edit", "args": edited_args}
    ]


def test_multiple_actions_preserve_order_for_approve_and_reject() -> None:
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "multi-hitl"}}

    interrupted = graph.invoke({"scenario": "multi"}, config=config)
    request = interrupted["__interrupt__"][0].value

    assert [action["name"] for action in request["action_requests"]] == [
        "execute",
        "write_file",
    ]

    resumed = graph.invoke(
        Command(
            resume={
                "decisions": [
                    {"type": "approve"},
                    {"type": "reject", "message": "不需要写文件"},
                ]
            }
        ),
        config=config,
    )

    assert resumed["result"] == [
        {
            "name": "execute",
            "decision": "approve",
            "args": request["action_requests"][0]["args"],
        },
        {
            "name": "write_file",
            "decision": "reject",
            "message": "不需要写文件",
        },
    ]
