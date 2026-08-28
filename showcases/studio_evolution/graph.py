"""仅供 LangGraph Studio 展示演进方向，不参与生产运行。"""

from __future__ import annotations

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


Route = Literal["fast", "plan", "code"]


class ShowcaseState(TypedDict, total=False):
    route: Route


def _noop(_: ShowcaseState) -> dict:
    return {}


def _select_route(state: ShowcaseState) -> Route:
    return state.get("route", "fast")


builder = StateGraph(ShowcaseState)
for node in ("execute_route", "fast", "plan", "code"):
    builder.add_node(node, _noop)
builder.add_edge(START, "execute_route")
builder.add_conditional_edges(
    "execute_route",
    _select_route,
    {"fast": "fast", "plan": "plan", "code": "code"},
)
for node in ("fast", "plan", "code"):
    builder.add_edge(node, END)

graph = builder.compile(name="studio-evolution-showcase")
