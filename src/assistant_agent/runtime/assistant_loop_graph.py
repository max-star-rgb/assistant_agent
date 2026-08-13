"""Assistant loop graph builders for standalone and namespaced composition."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from assistant_agent.memory.backends.disabled import build_disabled_memory_bundle
from assistant_agent.memory.node_bundle import MemoryNodeBundle
from assistant_agent.runtime.assistant_loop_nodes import (
    assistant_node,
    await_input_node,
    compose_response_node,
    execute_requested_tool_node,
    prepare_invocation_node,
    publish_response_node,
    time_travel_anchor_node,
)
from assistant_agent.runtime.assistant_graph_state import (
    AssistantGraphContinuation,
    AssistantTurnState,
    route_after_await_input_turn_state,
    route_after_assistant_turn_state,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.assistant_graph_profiles import AssistantGraphProfileName
from assistant_agent.runtime.graph_runtime import (
    GraphRuntimeContext,
    bind_checkpointed_runtime_node,
)


_CONTINUATION_TARGETS = {
    "memory_recall": "memory_recall",
    "assistant": "assistant",
    "await_input": "await_input",
    "execute_tool": "execute_tool",
    "compose_response": "compose_response",
    "publish_response": "publish_response",
    "memory_commit": "memory_commit",
    "end": END,
}


def _assistant_continuation(state: AssistantTurnState) -> AssistantGraphContinuation:
    route = route_after_assistant_turn_state(state)
    return cast(
        AssistantGraphContinuation,
        "compose_response" if route == "finish" else route,
    )


def _await_continuation(state: AssistantTurnState) -> AssistantGraphContinuation:
    return cast(AssistantGraphContinuation, route_after_await_input_turn_state(state))


def _route_prepared(state: Mapping[str, object]) -> AssistantGraphContinuation:
    return validate_assistant_turn_state(state)["continuation"]


def _semantic_node(
    node: Callable[[AssistantTurnState, Runtime[GraphRuntimeContext]], AssistantTurnState],
    continuation: Callable[[AssistantTurnState], AssistantGraphContinuation]
    | AssistantGraphContinuation,
) -> Callable[[AssistantTurnState, Runtime[GraphRuntimeContext]], AssistantTurnState]:
    def invoke(
        state: AssistantTurnState,
        runtime: Runtime[GraphRuntimeContext],
    ) -> AssistantTurnState:
        updated = dict(node(state, runtime))
        updated["continuation"] = (
            continuation(updated) if callable(continuation) else continuation
        )
        return validate_assistant_turn_state(updated)

    return invoke


def build_assistant_loop_graph(
    *,
    checkpointer: Any | None = None,
    memory_bundle: MemoryNodeBundle | None = None,
    profile: AssistantGraphProfileName = "standard",
    graph_name: str = "AssistantTurnGraph",
) -> Any:
    """Build the native loop with one stable invocation gate between semantics."""

    bundle = memory_bundle or build_disabled_memory_bundle()
    graph = StateGraph(AssistantTurnState, context_schema=GraphRuntimeContext)
    graph.add_node("prepare_invocation", prepare_invocation_node)
    graph.add_node("time_travel_anchor", time_travel_anchor_node)
    graph.add_node(
        "memory_recall",
        _semantic_node(bundle.recall_node, "assistant"),
    )
    graph.add_node(
        "assistant",
        _semantic_node(
            bind_checkpointed_runtime_node(
                "assistant", assistant_node, expected_profile=profile
            ),
            _assistant_continuation,
        ),
    )
    graph.add_node(
        "await_input",
        _semantic_node(await_input_node, _await_continuation),
    )
    graph.add_node(
        "execute_tool",
        _semantic_node(
            bind_checkpointed_runtime_node(
                "execute_tool",
                execute_requested_tool_node,
                expected_profile=profile,
            ),
            "assistant",
        ),
    )
    graph.add_node(
        "compose_response",
        _semantic_node(
            bind_checkpointed_runtime_node(
                "compose_response", compose_response_node, expected_profile=profile
            ),
            "publish_response",
        ),
    )
    graph.add_node(
        "publish_response",
        _semantic_node(publish_response_node, "memory_commit"),
    )
    graph.add_node(
        "memory_commit",
        _semantic_node(bundle.commit_node, "end"),
    )

    graph.add_edge(START, "prepare_invocation")
    graph.add_conditional_edges(
        "prepare_invocation", _route_prepared, _CONTINUATION_TARGETS
    )
    for semantic_node in (
        "memory_recall",
        "assistant",
        "await_input",
        "execute_tool",
        "compose_response",
        "publish_response",
        "memory_commit",
    ):
        graph.add_edge(semantic_node, "time_travel_anchor")
    graph.add_edge("time_travel_anchor", "prepare_invocation")
    return graph.compile(
        checkpointer=checkpointer,
        store=bundle.store,
        name=graph_name,
    )


def build_namespaced_assistant_loop_graph(
    *,
    state_schema: type,
    context_schema: type,
    child_state_key: str,
    runtime_context_resolver: Callable[
        [Mapping[str, object], AssistantTurnState, object], GraphRuntimeContext
    ],
    profile: AssistantGraphProfileName,
    graph_name: str,
    memory_bundle: MemoryNodeBundle | None = None,
) -> Any:
    """Compile the same gated loop over one explicit parent child-state channel."""

    bundle = memory_bundle or build_disabled_memory_bundle()

    def child_and_runtime(
        state: Mapping[str, object], runtime: Runtime[object]
    ) -> tuple[AssistantTurnState, Runtime[GraphRuntimeContext]]:
        child = state.get(child_state_key)
        if not isinstance(child, Mapping):
            raise ValueError(f"{child_state_key} must contain AssistantTurnState")
        child_state = validate_assistant_turn_state(child)
        child_context = runtime_context_resolver(state, child_state, runtime.context)
        return child_state, replace(runtime, context=child_context)

    def nested_gate(
        state: Mapping[str, object], runtime: Runtime[object]
    ) -> dict[str, AssistantTurnState]:
        child, child_runtime = child_and_runtime(state, runtime)
        return {child_state_key: prepare_invocation_node(child, child_runtime)}

    def nested_anchor(
        state: Mapping[str, object], runtime: Runtime[object]
    ) -> dict[str, AssistantTurnState]:
        child, _ = child_and_runtime(state, runtime)
        return {
            child_state_key: cast(AssistantTurnState, time_travel_anchor_node(child))
        }

    def nested_semantic(
        node_name: str,
        node_func: Callable[..., AssistantTurnState],
        continuation: Callable[[AssistantTurnState], AssistantGraphContinuation]
        | AssistantGraphContinuation,
        *,
        bind: bool = True,
    ) -> Callable[[Mapping[str, object], Runtime[object]], dict[str, AssistantTurnState]]:
        executable = (
            bind_checkpointed_runtime_node(
                node_name, node_func, expected_profile=profile
            )
            if bind
            else node_func
        )

        def invoke(
            state: Mapping[str, object], runtime: Runtime[object]
        ) -> dict[str, AssistantTurnState]:
            child, child_runtime = child_and_runtime(state, runtime)
            updated = dict(executable(child, child_runtime))
            updated["continuation"] = (
                continuation(updated) if callable(continuation) else continuation
            )
            return {child_state_key: validate_assistant_turn_state(updated)}

        return invoke

    def route_child(state: Mapping[str, object]) -> AssistantGraphContinuation:
        child = state.get(child_state_key)
        if not isinstance(child, Mapping):
            raise ValueError(f"{child_state_key} must contain AssistantTurnState")
        return _route_prepared(child)

    graph = StateGraph(state_schema, context_schema=context_schema)
    graph.add_node("prepare_invocation", nested_gate)
    graph.add_node("time_travel_anchor", nested_anchor)
    graph.add_node(
        "memory_recall",
        nested_semantic("memory_recall", bundle.recall_node, "assistant", bind=False),
    )
    graph.add_node(
        "assistant",
        nested_semantic("assistant", assistant_node, _assistant_continuation),
    )
    graph.add_node(
        "await_input",
        nested_semantic(
            "await_input", await_input_node, _await_continuation, bind=False
        ),
    )
    graph.add_node(
        "execute_tool",
        nested_semantic("execute_tool", execute_requested_tool_node, "assistant"),
    )
    graph.add_node(
        "compose_response",
        nested_semantic(
            "compose_response", compose_response_node, "publish_response"
        ),
    )
    graph.add_node(
        "publish_response",
        nested_semantic(
            "publish_response", publish_response_node, "memory_commit", bind=False
        ),
    )
    graph.add_node(
        "memory_commit",
        nested_semantic("memory_commit", bundle.commit_node, "end", bind=False),
    )
    graph.add_edge(START, "prepare_invocation")
    graph.add_conditional_edges("prepare_invocation", route_child, _CONTINUATION_TARGETS)
    for semantic_node in (
        "memory_recall",
        "assistant",
        "await_input",
        "execute_tool",
        "compose_response",
        "publish_response",
        "memory_commit",
    ):
        graph.add_edge(semantic_node, "time_travel_anchor")
    graph.add_edge("time_travel_anchor", "prepare_invocation")
    return graph.compile(checkpointer=None, store=bundle.store, name=graph_name)
