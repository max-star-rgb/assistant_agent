"""Plan-and-solve graph builder."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from multimodal_agent.agent.graph_runtime import GraphRuntimeContext, bind_runtime_node
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


def build_plan_and_solve_graph(
    *,
    checkpointer: Any | None = None,
    runtime_context: GraphRuntimeContext | None = None,
) -> Any:
    """Build and compile the explicit plan-and-solve strategy graph."""

    graph = StateGraph(PlanAndSolveState)

    graph.add_node("load_memory", bind_runtime_node("load_memory", load_memory_node, runtime_context))
    graph.add_node("planner", bind_runtime_node("planner", planner_node, runtime_context))
    graph.add_node("validate_plan", bind_runtime_node("validate_plan", validate_plan_node, runtime_context))
    graph.add_node("plan_controller", bind_runtime_node("plan_controller", plan_controller_node, runtime_context))
    graph.add_node("execute_plan_step", bind_runtime_node("execute_plan_step", execute_plan_step_node, runtime_context))
    graph.add_node("compose_response", bind_runtime_node("compose_response", compose_response_node, runtime_context))
    graph.add_node("save_memory", bind_runtime_node("save_memory", save_memory_node, runtime_context))

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

    return graph.compile(checkpointer=checkpointer)
