"""Assistant loop graph builders for standalone and namespaced composition."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

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
)
from assistant_agent.runtime.assistant_graph_state import (
    AssistantGraphContinuation,
    AssistantTurnState,
    reenter_assistant_invocation,
    route_after_await_input_turn_state,
    route_after_assistant_turn_state,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.assistant_graph_profiles import (
    AssistantGraphProfileName,
    GraphExecutionPolicyMismatchError,
)
from assistant_agent.runtime.graph_runtime import (
    GraphRuntimeContext,
    bind_checkpointed_runtime_node,
)


_ASSISTANT_TARGETS = {
    "await_input": "await_input",
    "execute_tool": "execute_tool",
    "finish": "compose_response",
}

_V4_CONTINUATION_TARGETS = {
    "memory_recall": "memory_recall",
    "assistant": "assistant",
    "await_input": "await_input",
    "execute_tool": "execute_tool",
    "compose_response": "compose_response",
    "publish_response": "publish_response",
    "memory_commit": "memory_commit",
    "end": END,
}

_AWAIT_INPUT_TARGETS = {
    "execute_tool": "execute_tool",
    "assistant": "assistant",
}


def _route_prepared_v4(state: Mapping[str, object]) -> AssistantGraphContinuation:
    """Consume the legacy route only when entering through the v4 gate."""

    return validate_assistant_turn_state(state)["continuation"]


def _route_after_assistant_native(
    state: AssistantTurnState,
    runtime: Runtime[GraphRuntimeContext],
) -> str:
    context = runtime.context
    if (
        context is not None
        and context.interrupt_request is not None
        and context.invocation_kind == "invoke"
    ):
        return "await_input"
    return route_after_assistant_turn_state(state)


def _reenter_for_runtime(
    state: AssistantTurnState,
    runtime: Runtime[GraphRuntimeContext],
) -> AssistantTurnState:
    context = runtime.context
    if context is not None and (
        context.execution_policy.profile != state.get("profile")
        or context.execution_policy.policy_digest != state.get("policy_digest")
    ):
        raise GraphExecutionPolicyMismatchError(
            "Runtime execution policy does not match the checkpoint digest."
        )
    persisted_run = state.get("run")
    if (
        context is None
        or context.agent_state is None
        or not isinstance(persisted_run, Mapping)
        or persisted_run.get("run_id") == context.agent_state.run_id
    ):
        return state
    return reenter_assistant_invocation(
        state,
        runtime_state=context.agent_state,
        invocation_kind=context.invocation_kind,
    )


def _validated_node(
    node: Callable[[AssistantTurnState, Runtime[GraphRuntimeContext]], AssistantTurnState],
) -> Callable[[AssistantTurnState, Runtime[GraphRuntimeContext]], AssistantTurnState]:
    def invoke(
        state: AssistantTurnState,
        runtime: Runtime[GraphRuntimeContext],
    ) -> AssistantTurnState:
        reentered = _reenter_for_runtime(state, runtime)
        return validate_assistant_turn_state(node(reentered, runtime))

    return invoke


def build_assistant_loop_graph(
    *,
    checkpointer: Any | None = None,
    memory_bundle: MemoryNodeBundle | None = None,
    context_schema: type = GraphRuntimeContext,
    runtime_context_resolver: Callable[
        [AssistantTurnState, Runtime[Any]], GraphRuntimeContext
    ]
    | None = None,
    profile: AssistantGraphProfileName = "standard",
    graph_name: str = "AssistantTurnGraph",
) -> Any:
    """Build the native loop with LangGraph edges as the execution-position source."""

    bundle = memory_bundle or build_disabled_memory_bundle()
    def resolved_node(node: Callable[..., AssistantTurnState]) -> Callable[..., AssistantTurnState]:
        if runtime_context_resolver is None:
            return node

        def invoke(
            state: AssistantTurnState,
            runtime: Runtime[Any],
        ) -> AssistantTurnState:
            worker_context = runtime_context_resolver(state, runtime)
            return node(state, runtime.override(context=worker_context))

        return invoke

    graph = StateGraph(AssistantTurnState, context_schema=context_schema)
    graph.add_node("prepare_invocation", resolved_node(prepare_invocation_node))
    graph.add_node(
        "memory_recall",
        resolved_node(_validated_node(bundle.recall_node)),
    )
    graph.add_node(
        "assistant",
        resolved_node(_validated_node(
            bind_checkpointed_runtime_node(
                "assistant", assistant_node, expected_profile=profile
            ),
        )),
    )
    graph.add_node(
        "await_input",
        resolved_node(_validated_node(await_input_node)),
    )
    graph.add_node(
        "execute_tool",
        resolved_node(_validated_node(
            bind_checkpointed_runtime_node(
                "execute_tool",
                execute_requested_tool_node,
                expected_profile=profile,
            ),
        )),
    )
    graph.add_node(
        "compose_response",
        resolved_node(_validated_node(
            bind_checkpointed_runtime_node(
                "compose_response", compose_response_node, expected_profile=profile
            ),
        )),
    )
    graph.add_node(
        "publish_response",
        resolved_node(_validated_node(publish_response_node)),
    )
    graph.add_node(
        "memory_commit",
        resolved_node(_validated_node(bundle.commit_node)),
    )

    graph.add_edge(START, "prepare_invocation")
    graph.add_conditional_edges(
        "prepare_invocation", _route_prepared_v4, _V4_CONTINUATION_TARGETS
    )
    graph.add_edge("memory_recall", "assistant")
    graph.add_conditional_edges(
        "assistant", _route_after_assistant_native, _ASSISTANT_TARGETS
    )
    graph.add_conditional_edges(
        "await_input", route_after_await_input_turn_state, _AWAIT_INPUT_TARGETS
    )
    graph.add_edge("execute_tool", "assistant")
    graph.add_edge("compose_response", "publish_response")
    graph.add_edge("publish_response", "memory_commit")
    graph.add_edge("memory_commit", END)
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
        [Mapping[str, object], AssistantTurnState, Runtime[Any]], GraphRuntimeContext
    ],
    profile: AssistantGraphProfileName,
    graph_name: str,
    memory_bundle: MemoryNodeBundle | None = None,
    bind_store: bool = True,
) -> Any:
    """Compile the same native loop over one explicit parent child-state channel."""

    bundle = memory_bundle or build_disabled_memory_bundle()

    def child_and_runtime(
        state: Mapping[str, object], runtime: Runtime[object]
    ) -> tuple[AssistantTurnState, Runtime[GraphRuntimeContext]]:
        child = state.get(child_state_key)
        if not isinstance(child, Mapping):
            raise ValueError(f"{child_state_key} must contain AssistantTurnState")
        child_state = validate_assistant_turn_state(child)
        child_context = runtime_context_resolver(state, child_state, runtime)
        return child_state, replace(runtime, context=child_context)

    def nested_gate(
        state: Mapping[str, object], runtime: Runtime[object]
    ) -> dict[str, AssistantTurnState]:
        child, child_runtime = child_and_runtime(state, runtime)
        return {child_state_key: prepare_invocation_node(child, child_runtime)}

    def nested_semantic(
        node_name: str,
        node_func: Callable[..., AssistantTurnState],
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
            reentered = _reenter_for_runtime(child, child_runtime)
            updated = executable(reentered, child_runtime)
            return {child_state_key: validate_assistant_turn_state(updated)}

        return invoke

    def route_child_after_assistant(
        state: Mapping[str, object], runtime: Runtime[object]
    ) -> str:
        child, child_runtime = child_and_runtime(state, runtime)
        return _route_after_assistant_native(child, child_runtime)

    def route_child_after_await_input(state: Mapping[str, object]) -> str:
        child = state.get(child_state_key)
        if not isinstance(child, Mapping):
            raise ValueError(f"{child_state_key} must contain AssistantTurnState")
        return route_after_await_input_turn_state(child)

    def route_child_after_prepare(
        state: Mapping[str, object],
    ) -> AssistantGraphContinuation:
        child = state.get(child_state_key)
        if not isinstance(child, Mapping):
            raise ValueError(f"{child_state_key} must contain AssistantTurnState")
        return _route_prepared_v4(child)

    graph = StateGraph(state_schema, context_schema=context_schema)
    graph.add_node("prepare_invocation", nested_gate)
    graph.add_node(
        "memory_recall",
        nested_semantic("memory_recall", bundle.recall_node, bind=False),
    )
    graph.add_node(
        "assistant",
        nested_semantic("assistant", assistant_node),
    )
    graph.add_node(
        "await_input",
        nested_semantic("await_input", await_input_node, bind=False),
    )
    graph.add_node(
        "execute_tool",
        nested_semantic("execute_tool", execute_requested_tool_node),
    )
    graph.add_node(
        "compose_response",
        nested_semantic("compose_response", compose_response_node),
    )
    graph.add_node(
        "publish_response",
        nested_semantic("publish_response", publish_response_node, bind=False),
    )
    graph.add_node(
        "memory_commit",
        nested_semantic("memory_commit", bundle.commit_node, bind=False),
    )
    graph.add_edge(START, "prepare_invocation")
    graph.add_conditional_edges(
        "prepare_invocation", route_child_after_prepare, _V4_CONTINUATION_TARGETS
    )
    graph.add_edge("memory_recall", "assistant")
    graph.add_conditional_edges(
        "assistant", route_child_after_assistant, _ASSISTANT_TARGETS
    )
    graph.add_conditional_edges(
        "await_input", route_child_after_await_input, _AWAIT_INPUT_TARGETS
    )
    graph.add_edge("execute_tool", "assistant")
    graph.add_edge("compose_response", "publish_response")
    graph.add_edge("publish_response", "memory_commit")
    graph.add_edge("memory_commit", END)
    return graph.compile(
        checkpointer=None,
        store=bundle.store if bind_store else None,
        name=graph_name,
    )
