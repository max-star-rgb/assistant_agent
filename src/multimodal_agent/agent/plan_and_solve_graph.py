"""Plan-and-solve graph builder."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from multimodal_agent.agent.graph_nodes import compose_response_node, load_memory_node, save_memory_node
from multimodal_agent.agent.plan_and_solve_nodes import (
    PlanAndSolveState,
    execute_plan_step_node,
    plan_controller_node,
    planner_node,
    route_after_controller,
    route_after_execute_step,
    route_after_validate_plan,
    validate_plan_node,
)
from multimodal_agent.services.trace_store import trace_graph_node


def build_plan_and_solve_graph() -> Any:
    """Build and compile the explicit plan-and-solve strategy graph."""

    graph = StateGraph(PlanAndSolveState)

    graph.add_node("load_memory", trace_graph_node("load_memory", load_memory_node))
    graph.add_node("planner", trace_graph_node("planner", planner_node))
    graph.add_node("validate_plan", trace_graph_node("validate_plan", validate_plan_node))
    graph.add_node("plan_controller", trace_graph_node("plan_controller", plan_controller_node))
    graph.add_node("execute_plan_step", trace_graph_node("execute_plan_step", execute_plan_step_node))
    graph.add_node("compose_response", trace_graph_node("compose_response", compose_response_node))
    graph.add_node("save_memory", trace_graph_node("save_memory", save_memory_node))

    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "planner")
    graph.add_edge("planner", "validate_plan")
    graph.add_conditional_edges(
        "validate_plan",
        route_after_validate_plan,
        {
            "controller": "plan_controller",
            "finish": "compose_response",
        },
    )
    graph.add_conditional_edges(
        "plan_controller",
        route_after_controller,
        {
            "execute_step": "execute_plan_step",
            "planner": "planner",
            "finish": "compose_response",
        },
    )
    graph.add_conditional_edges(
        "execute_plan_step",
        route_after_execute_step,
        {
            "controller": "plan_controller",
            "finish": "compose_response",
        },
    )
    graph.add_edge("compose_response", "save_memory")
    graph.add_edge("save_memory", END)

    return graph.compile()
