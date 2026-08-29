"""离线展示 Studio HITL 扩展；只生成审批，不执行任何副作用。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class ShowcaseState(TypedDict, total=False):
    scenario: Literal["single", "multi"]
    result: list[dict[str, Any]]


_EXECUTE = {
    "name": "execute",
    "description": "演示审批一条离线命令；本图不会实际执行它。",
    "args": {
        "command": "python -m pytest -q",
        "timeout_seconds": 30,
        "enabled": True,
        "env": {"MODE": "mock"},
        "paths": ["tests/core"],
    },
}
_WRITE_FILE = {
    "name": "write_file",
    "description": "演示审批写文件参数；本图不会实际写入。",
    "args": {"path": "notes.txt", "content": "hello"},
}
_REVIEWS = {
    "execute": {
        "action_name": "execute",
        "allowed_decisions": ["approve", "edit", "reject"],
        "args_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": [
                        "python -m pytest -q",
                        "python -m compileall -q src",
                    ],
                },
                "timeout_seconds": {"type": "integer"},
                "enabled": {"type": "boolean"},
                "env": {
                    "type": "object",
                    "properties": {"MODE": {"type": "string"}},
                },
                "paths": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "write_file": {
        "action_name": "write_file",
        "allowed_decisions": ["approve", "edit", "reject"],
        "args_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    },
}


def _review(state: ShowcaseState) -> dict[str, list[dict[str, Any]]]:
    actions = [_EXECUTE]
    if state.get("scenario") == "multi":
        actions.append(_WRITE_FILE)
    actions = deepcopy(actions)
    resumed = interrupt(
        {
            "action_requests": actions,
            "review_configs": [deepcopy(_REVIEWS[action["name"]]) for action in actions],
        }
    )
    decisions = resumed.get("decisions") if isinstance(resumed, dict) else None
    if not isinstance(decisions, list) or len(decisions) != len(actions):
        raise ValueError("每个 action 必须按顺序提供一个 decision")

    result: list[dict[str, Any]] = []
    for action, decision in zip(actions, decisions, strict=True):
        decision_type = decision.get("type") if isinstance(decision, dict) else None
        if decision_type == "approve":
            result.append(
                {"name": action["name"], "decision": "approve", "args": action["args"]}
            )
        elif decision_type == "edit":
            edited = decision.get("edited_action", {})
            if edited.get("name") != action["name"] or not isinstance(
                edited.get("args"), dict
            ):
                raise ValueError("edited_action 必须匹配原 action")
            result.append(
                {"name": action["name"], "decision": "edit", "args": edited["args"]}
            )
        elif decision_type == "reject":
            result.append(
                {
                    "name": action["name"],
                    "decision": "reject",
                    "message": decision.get("message", ""),
                }
            )
        else:
            raise ValueError("不支持的 decision")
    return {"result": result}


def build_graph(*, checkpointer=None):
    builder = StateGraph(ShowcaseState)
    builder.add_node("review", _review)
    builder.add_edge(START, "review")
    builder.add_edge("review", END)
    return builder.compile(
        checkpointer=checkpointer,
        name="studio-hitl-showcase",
    )


graph = build_graph()
