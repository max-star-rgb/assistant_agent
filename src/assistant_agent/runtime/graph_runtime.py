"""Runtime dependency binding for LangGraph nodes.

LangGraph checkpoints serialize graph state. Agent dependencies such as tool
executors, model adapters, stores, and managers are runtime objects and must not
be persisted in that state. This module binds those dependencies around node
execution and strips them before the node result returns to LangGraph.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import copy
from dataclasses import dataclass, field
import secrets
from typing import Any, Protocol, TypeVar, cast

from langgraph.runtime import Runtime

from assistant_agent.runtime.cancellation import raise_if_cancelled
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.context.service import ContextService
from assistant_agent.observability.trace_store import TraceStore, trace_graph_node
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    assistant_capability_ref_identity,
    assistant_context_ref_identity,
    assistant_loop_state_from_turn_state,
    assistant_turn_state_from_loop_state,
    validate_assistant_runtime_refs,
)
from assistant_agent.runtime.requests import RuntimeTaskUpdate, UserRequest
from assistant_agent.runtime.assistant_graph_profiles import (
    assistant_graph_profile,
    AssistantGraphProfileName,
    GraphProfileMismatchError,
    GraphProfilePolicyError,
    profile_scope_matches,
)
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.graph_invocation_claims import (
    GraphInvocationClaimStore,
    GraphInvocationKind,
)


GraphState = dict[str, Any]
GraphStateT = TypeVar("GraphStateT", bound=dict[str, Any])


class ContinuationPreparationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def user_request_from_checkpoint(value: Mapping[str, Any]) -> UserRequest:
    """Rebuild only product request fields frozen in a validated checkpoint."""

    from assistant_agent.runtime.assistant_graph_state import (
        persisted_request_from_user_request,
    )

    media = tuple(value.get("media_refs") or ())
    messages = [dict(item) for item in tuple(value.get("messages") or ())]
    request_text = value.get("text")
    if (
        request_text is not None
        and messages
        and messages[-1].get("role") == "user"
        and messages[-1].get("text") == request_text
    ):
        messages.pop()
    task_facts = value.get("runtime_task_facts")
    request = UserRequest(
        user_id=str(value["user_id"]),
        session_id=str(value["session_id"]),
        text=request_text,
        image_ids=[str(item["ref"]) for item in media if item.get("kind") == "image"],
        video_ids=[str(item["ref"]) for item in media if item.get("kind") == "video"],
        audio_id=next(
            (str(item["ref"]) for item in media if item.get("kind") == "audio"),
            None,
        ),
        assistant_mode=value.get("assistant_mode", "standard"),
        task_execution_mode=value.get("task_execution_mode", "auto"),
        response_style=value.get("response_style"),
        runtime_task_update=(
            RuntimeTaskUpdate.model_validate(task_facts)
            if isinstance(task_facts, Mapping)
            else None
        ),
        metadata={"conversation_history": messages},
    )
    projected = persisted_request_from_user_request(request)
    projected["capability_refs"] = list(value.get("capability_refs") or ())
    if projected != dict(value):
        raise ContinuationPreparationError(
            "graph_checkpoint_request_mismatch",
            "Checkpoint request cannot be reconstructed safely.",
        )
    return request


def continuation_ref_resolver(
    persisted: AssistantTurnState,
    runtime_state: AgentState,
) -> AssistantRuntimeStateRefResolver:
    """Validate freshly reconstructed owner-bound refs and freeze their identity."""

    expected_context_refs = tuple(
        tuple(
            item.get(key) for key in ("kind", "ref", "source", "version", "status_code")
        )
        for item in persisted["context_refs"]
    )
    if expected_context_refs != assistant_context_ref_identity(runtime_state):
        raise ContinuationPreparationError(
            "graph_continuation_context_refs_unavailable",
            "Checkpoint context refs cannot be resolved through current owners.",
        )
    if tuple(persisted["capability_refs"]) != assistant_capability_ref_identity(
        runtime_state
    ):
        raise ContinuationPreparationError(
            "graph_continuation_capability_mismatch",
            "Checkpoint capabilities are unavailable in this Runtime.",
        )

    def resolve(checkpoint: AssistantTurnState, state: AgentState) -> None:
        validate_assistant_runtime_refs(checkpoint, state)

    return resolve


RUNTIME_STATE_KEYS = frozenset(
    {
        "tool_executor",
        "chat_adapter",
        "chat_turn",
        "context_service",
        "context_projector",
        "tool_result_handler",
        "trace_store",
        "event_sink",
        "product_fact_writer",
        "current_node_name",
        "cancel_token",
    }
)


@dataclass(frozen=True)
class GraphRuntimeContext:
    """Non-checkpointable dependencies used by graph nodes."""

    tool_executor: ToolExecutor
    chat_adapter: ChatAdapter
    invocation_claim_store: GraphInvocationClaimStore
    chat_turn: Callable[[Any], Any] | None = None
    context_service: ContextService | None = None
    context_projector: Callable[[Any], None] | None = None
    tool_result_handler: Callable[[Any, Any], None] | None = None
    trace_store: TraceStore | None = None
    product_fact_writer: Callable[[Any], None] | None = None
    cancel_token: Any | None = None
    agent_state: AgentState | None = None
    state_ref_resolver: "AssistantRuntimeStateRefResolver | None" = None
    profile_allowed_tool_names: frozenset[str] | None = None
    invocation_kind: GraphInvocationKind = "invoke"
    invocation_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    graph_profile: AssistantGraphProfileName = "standard"


class AssistantRuntimeStateRefResolver(Protocol):
    """Resolve or validate checkpoint refs before a node may consume them."""

    def __call__(
        self,
        persisted: AssistantTurnState,
        runtime_state: AgentState,
    ) -> None: ...


def bind_checkpointed_runtime_node(
    node_name: str,
    node_func: Callable[[GraphState], GraphState],
    *,
    trace: bool = True,
    expected_profile: AssistantGraphProfileName = "standard",
) -> Callable[[AssistantTurnState, Runtime[GraphRuntimeContext]], AssistantTurnState]:
    """Adapt a strict checkpoint state to a temporary legacy node invocation."""

    executable = trace_graph_node(node_name, node_func) if trace else node_func

    def wrapped(
        graph_state: AssistantTurnState,
        runtime: Runtime[GraphRuntimeContext],
    ) -> AssistantTurnState:
        runtime_context = runtime.context
        if runtime_context is None or runtime_context.agent_state is None:
            raise RuntimeError(f"{node_name} requires invocation-local AgentState")
        checkpoint_profile = graph_state.get("profile")
        if checkpoint_profile != expected_profile:
            raise GraphProfileMismatchError(
                f"compiled {expected_profile!r} graph cannot run "
                f"checkpoint profile {checkpoint_profile!r}"
            )
        raise_if_cancelled(
            runtime_context.cancel_token,
            phase="before_node",
            node_name=node_name,
        )
        resolver = runtime_context.state_ref_resolver or validate_assistant_runtime_refs
        resolver(graph_state, runtime_context.agent_state)
        checkpoint_tool_names = set(
            graph_state.get("catalog", {}).get("available_tool_names", ())
        )
        runtime_tool_names = {
            spec.name for spec in runtime_context.tool_executor.registry.list_specs()
        }
        if not checkpoint_tool_names.issubset(runtime_tool_names):
            from assistant_agent.runtime.assistant_graph_state import (
                AssistantStateCompatibilityError,
            )

            raise AssistantStateCompatibilityError(
                "Checkpoint Tool catalog is unavailable in this runtime."
            )
        scoped_runtime_context = _scoped_runtime_context(
            runtime_context,
            graph_state,
            expected_profile=expected_profile,
        )
        legacy_state = assistant_loop_state_from_turn_state(
            graph_state,
            runtime_state=runtime_context.agent_state,
        )
        enriched_state = _with_runtime_context(
            legacy_state,
            scoped_runtime_context,
            native_stream_writer=runtime.stream_writer,
        )
        result = executable(enriched_state)
        raise_if_cancelled(
            runtime_context.cancel_token,
            phase="after_node",
            node_name=node_name,
            state=result.get("state") if isinstance(result, dict) else None,
        )
        profile = graph_state.get("profile", "standard")
        projected = assistant_turn_state_from_loop_state(result, profile=profile)
        return _preserve_profile_scope(
            projected,
            graph_state,
            expected_profile=expected_profile,
        )

    return wrapped


def bind_runtime_node(
    node_name: str,
    node_func: Callable[[GraphStateT], GraphStateT],
    *,
    trace: bool = True,
) -> Callable[[GraphStateT, Runtime[GraphRuntimeContext]], GraphStateT]:
    """Return a node that injects runtime objects only during execution."""

    executable = trace_graph_node(node_name, node_func) if trace else node_func

    def wrapped(
        graph_state: GraphStateT,
        runtime: Runtime[GraphRuntimeContext],
    ) -> GraphStateT:
        runtime_context = runtime.context
        if runtime_context is None:
            raise RuntimeError(f"{node_name} requires GraphRuntimeContext")
        raise_if_cancelled(
            runtime_context.cancel_token, phase="before_node", node_name=node_name
        )
        enriched_state = _with_runtime_context(graph_state, runtime_context)
        result = executable(enriched_state)
        raise_if_cancelled(
            runtime_context.cancel_token,
            phase="after_node",
            node_name=node_name,
            state=result.get("state") if isinstance(result, dict) else None,
        )
        return cast(GraphStateT, strip_runtime_context(result))

    return wrapped


def strip_runtime_context(graph_state: GraphState) -> GraphState:
    """Remove runtime-only keys from a graph state copy."""

    clean_state = dict(graph_state)
    for key in RUNTIME_STATE_KEYS:
        clean_state.pop(key, None)
    return clean_state


def _with_runtime_context(
    graph_state: GraphStateT,
    runtime_context: GraphRuntimeContext,
    *,
    native_stream_writer: Callable[[Any], None] | None = None,
) -> GraphStateT:
    enriched_state = dict(graph_state)
    enriched_state["tool_executor"] = runtime_context.tool_executor
    enriched_state["chat_adapter"] = runtime_context.chat_adapter
    if runtime_context.chat_turn is not None:
        enriched_state["chat_turn"] = runtime_context.chat_turn
    if runtime_context.context_service is not None:
        enriched_state["context_service"] = runtime_context.context_service
    if runtime_context.context_projector is not None:
        enriched_state["context_projector"] = runtime_context.context_projector
    if runtime_context.tool_result_handler is not None:
        enriched_state["tool_result_handler"] = runtime_context.tool_result_handler
    if runtime_context.trace_store is not None:
        enriched_state["trace_store"] = runtime_context.trace_store
    product_fact_writer = (
        runtime_context.product_fact_writer
        if runtime_context.product_fact_writer is not None
        else native_stream_writer
    )
    if product_fact_writer is not None:
        enriched_state["product_fact_writer"] = product_fact_writer
    if runtime_context.cancel_token is not None:
        enriched_state["cancel_token"] = runtime_context.cancel_token
    return cast(GraphStateT, enriched_state)


class _ProfileToolRegistryView:
    """Filter Provider-facing specs while preserving governed lookup/execution."""

    def __init__(self, registry: Any, allowed_names: frozenset[str]) -> None:
        self._registry = registry
        self._allowed_names = allowed_names

    def list_specs(self) -> list[Any]:
        return [
            spec
            for spec in self._registry.list_specs()
            if spec.name in self._allowed_names
        ]

    def describe_tools(self) -> list[dict[str, Any]]:
        return [spec.model_dump(mode="json") for spec in self.list_specs()]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._registry, name)


def _scoped_runtime_context(
    runtime_context: GraphRuntimeContext,
    graph_state: AssistantTurnState,
    *,
    expected_profile: AssistantGraphProfileName,
) -> GraphRuntimeContext:
    catalog = graph_state.get("catalog", {})
    reason_codes = tuple(catalog.get("selection_reason_codes", ()))
    marker = f"graph_profile:{expected_profile}"
    if marker not in reason_codes:
        if expected_profile != "standard":
            raise GraphProfilePolicyError(
                f"{expected_profile!r} child state lacks its profile scope marker"
            )
        return runtime_context

    profile = assistant_graph_profile(expected_profile)
    allowed_names = frozenset(catalog.get("available_tool_names", ()))
    trusted_names = runtime_context.profile_allowed_tool_names
    if trusted_names is None or allowed_names != trusted_names:
        raise GraphProfilePolicyError(
            "checkpoint Tool scope does not match its trusted runtime assignment"
        )
    if not profile_scope_matches(
        expected_profile,
        list(allowed_names),
        list(reason_codes),
    ):
        raise GraphProfilePolicyError(
            "checkpoint Tool scope does not match its profile adapter"
        )
    registered_specs = {
        spec.name: spec for spec in runtime_context.tool_executor.registry.list_specs()
    }
    invalid_categories = sorted(
        name
        for name in allowed_names
        if name not in registered_specs
        or registered_specs[name].category not in profile.allowed_categories
        or (
            profile.max_control_tool_iterations == 0
            and name in {LOAD_SKILL_TOOL_NAME, LOAD_SKILL_REFERENCE_TOOL_NAME}
        )
    )
    if invalid_categories:
        raise GraphProfilePolicyError(
            "checkpoint Tool scope exceeds the selected graph profile"
        )
    if (
        int(graph_state.get("max_tool_calls_per_run", 0)) > profile.max_tool_iterations
        or int(graph_state.get("max_action_tool_calls_per_run", 0))
        > profile.max_tool_iterations
        or int(graph_state.get("max_control_tool_calls_per_run", 0))
        > profile.max_control_tool_iterations
    ):
        raise GraphProfilePolicyError(
            "checkpoint Tool budget exceeds the selected graph profile"
        )
    executor = copy(runtime_context.tool_executor)
    executor.registry = _ProfileToolRegistryView(
        runtime_context.tool_executor.registry,
        allowed_names,
    )
    return GraphRuntimeContext(
        tool_executor=executor,
        chat_adapter=runtime_context.chat_adapter,
        chat_turn=runtime_context.chat_turn,
        context_service=runtime_context.context_service,
        context_projector=runtime_context.context_projector,
        tool_result_handler=runtime_context.tool_result_handler,
        trace_store=runtime_context.trace_store,
        product_fact_writer=runtime_context.product_fact_writer,
        cancel_token=runtime_context.cancel_token,
        agent_state=runtime_context.agent_state,
        state_ref_resolver=runtime_context.state_ref_resolver,
        profile_allowed_tool_names=runtime_context.profile_allowed_tool_names,
        invocation_claim_store=runtime_context.invocation_claim_store,
        invocation_kind=runtime_context.invocation_kind,
        invocation_token=runtime_context.invocation_token,
        graph_profile=runtime_context.graph_profile,
    )


def _preserve_profile_scope(
    projected: AssistantTurnState,
    prior: AssistantTurnState,
    *,
    expected_profile: AssistantGraphProfileName,
) -> AssistantTurnState:
    # A trusted interrupt request is committed in the graph input before the
    # assistant decision runs.  Legacy node projection must not erase that
    # checkpoint channel while the native await_input edge is still pending.
    projected["pending_interrupt"] = prior.get("pending_interrupt")
    if prior.get("pending_interrupt") is not None:
        run = dict(projected["run"])
        run["status"] = "running"
        projected["run"] = run
        projected["final_response"] = None
    marker = f"graph_profile:{expected_profile}"
    prior_catalog = prior.get("catalog", {})
    prior_reasons = tuple(prior_catalog.get("selection_reason_codes", ()))
    if marker not in prior_reasons:
        return projected
    profile = assistant_graph_profile(expected_profile)
    allowed = frozenset(prior_catalog.get("available_tool_names", ()))
    current_catalog = projected.get("catalog", {})
    current_names = tuple(current_catalog.get("available_tool_names", ()))
    projected["catalog"] = {
        "schema_version": "run_tool_catalog_v1",
        "available_tool_names": [name for name in current_names if name in allowed],
        "selection_reason_codes": list(
            dict.fromkeys(
                [
                    *current_catalog.get("selection_reason_codes", ()),
                    *prior_catalog.get("selection_reason_codes", ()),
                ]
            )
        ),
        "exclusion_reason_codes": list(
            dict.fromkeys(
                [
                    *prior_catalog.get("exclusion_reason_codes", ()),
                    *current_catalog.get("exclusion_reason_codes", ()),
                ]
            )
        ),
    }
    projected["max_assistant_iterations"] = min(
        int(projected.get("max_assistant_iterations", 0)),
        max(1, profile.max_tool_iterations),
    )
    projected["max_tool_calls_per_run"] = min(
        int(projected.get("max_tool_calls_per_run", 0)),
        profile.max_tool_iterations,
    )
    projected["max_action_tool_calls_per_run"] = min(
        int(projected.get("max_action_tool_calls_per_run", 0)),
        profile.max_tool_iterations,
    )
    projected["max_control_tool_calls_per_run"] = min(
        int(projected.get("max_control_tool_calls_per_run", 0)),
        profile.max_control_tool_iterations,
    )
    return projected
