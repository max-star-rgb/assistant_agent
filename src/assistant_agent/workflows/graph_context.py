"""Runtime-only dependencies for native Durable Workflow graph branches."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from assistant_agent.context.models import ContextSection, ContextSourceResult
from assistant_agent.context.service import ContextService
from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    AssistantStateCompatibilityError,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.capability_grants import CapabilityGrantValue
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.runtime.graph_invocation_claims import (
    GraphInvocationClaimStore,
    derive_child_invocation_token,
)
from assistant_agent.runtime.requests import RuntimeTaskUpdate, UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.tool_operation_barrier import ToolOperationStore
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.graph_state import (
    PersistedWorkflowIdentity,
    WorkflowProfileAssignment,
)
from assistant_agent.workflows.graph_publish import (
    SQLiteWorkflowPublishStore,
    SQLiteWorkflowPublisher,
)


class WorkflowCancelReader(Protocol):
    def __call__(self, assignment: WorkflowProfileAssignment) -> object | None: ...


class WorkflowStreamWriter(Protocol):
    def __call__(
        self,
        assignment: WorkflowProfileAssignment,
        fact: object,
    ) -> None: ...


class CapabilityGrantResolver(Protocol):
    def __call__(
        self,
        assignment: WorkflowProfileAssignment,
    ) -> tuple[CapabilityGrantValue, ...]: ...


@dataclass(frozen=True)
class WorkflowGraphRuntimeServices:
    """Immutable process-owned service bundle; never checkpointed."""

    provider_registry: Mapping[str, ChatAdapter] | object
    tool_registry: ToolRegistry
    context_service: ContextService
    operation_store: ToolOperationStore
    workflow_identity: PersistedWorkflowIdentity
    cancel_reader: WorkflowCancelReader
    stream_writer: WorkflowStreamWriter
    invocation_claim_store: GraphInvocationClaimStore
    capability_grant_resolver: CapabilityGrantResolver | None = None
    publish_store: SQLiteWorkflowPublishStore | None = None
    publisher: SQLiteWorkflowPublisher | None = None


@dataclass(frozen=True)
class WorkflowGraphRuntimeContext:
    """Runtime context for the parent workflow graph."""

    assistant_graph_app: AssistantTurnGraphApp
    artifact_store: LocalWorkflowArtifactStore
    context_compiler: WorkflowContextCompiler
    branch_context_factory: "BranchProfileContextFactory"
    services: WorkflowGraphRuntimeServices
    invocation_token: str


class BranchProfileContextFactory:
    """Purely rebuild an isolated child runtime from persisted assignment facts.

    The factory deliberately owns no mutable AgentState or ToolExecutor pool.
    Repeating the call after process/cache loss follows the same construction
    path and returns fresh branch-local objects.
    """

    def context_for_assignment(
        self,
        outer_assignment: WorkflowProfileAssignment,
        child_state: AssistantTurnState,
        services: WorkflowGraphRuntimeServices,
        *,
        parent_invocation_token: str,
    ) -> GraphRuntimeContext:
        assignment = WorkflowProfileAssignment.model_validate(
            outer_assignment.model_dump(mode="python")
            if isinstance(outer_assignment, WorkflowProfileAssignment)
            else outer_assignment
        )
        child = validate_assistant_turn_state(child_state)
        identity = services.workflow_identity
        if (
            assignment.user_id != identity.user_id
            or assignment.session_id != identity.session_id
            or assignment.agent_id != identity.agent_id
            or assignment.workflow_thread_id != identity.workflow_thread_id
        ):
            raise AssistantStateCompatibilityError(
                "Workflow branch owner or thread does not match runtime services."
            )
        self._validate_assignment_child(assignment, child, services)

        grants = self._resolve_capability_grants(assignment, services)
        request = _request_from_assignment(assignment)
        agent_state = AgentState.from_request(
            request,
            run_id=assignment.run_id,
            trace_id=assignment.trace_id,
            agent_id=assignment.agent_id,
        )
        agent_state.capability_grants = list(grants)
        agent_state.context_source_result = ContextSourceResult(
            sections=[
                ContextSection(
                    section_id=assignment.assignment_ref,
                    kind="durable_task_state",
                    title="Workflow branch assignment",
                    content=assignment.objective,
                    authority="runtime_evidence",
                    stability="volatile",
                    source_type="runtime",
                    source_ref=assignment.assignment_ref,
                    identity_scope="runtime",
                    max_chars=len(assignment.objective),
                )
            ]
        )

        cancel_token = services.cancel_reader(assignment)
        executor = ToolExecutor(
            registry=services.tool_registry,
            context_metadata={
                "_trusted_workflow_assignment": assignment.model_dump(mode="json"),
                "_trusted_workflow_allowed_tools": list(
                    assignment.available_tool_names
                ),
            },
            cancel_token=cancel_token,
            operation_store=services.operation_store,
        )
        return GraphRuntimeContext(
            tool_executor=executor,
            chat_adapter=_provider_for_profile(services, assignment.profile),
            context_service=services.context_service,
            product_fact_writer=lambda fact: services.stream_writer(assignment, fact),
            cancel_token=cancel_token,
            agent_state=agent_state,
            state_ref_resolver=_AssignmentStateRefResolver(assignment),
            profile_allowed_tool_names=frozenset(assignment.available_tool_names),
            invocation_claim_store=services.invocation_claim_store,
            invocation_token=derive_child_invocation_token(
                parent_invocation_token=parent_invocation_token,
                assignment_ref=assignment.assignment_ref,
            ),
            graph_profile=assignment.profile,
        )

    @staticmethod
    def _resolve_capability_grants(
        assignment: WorkflowProfileAssignment,
        services: WorkflowGraphRuntimeServices,
    ) -> tuple[CapabilityGrantValue, ...]:
        if not assignment.capability_refs:
            return ()
        resolver = services.capability_grant_resolver
        if resolver is None:
            raise AssistantStateCompatibilityError(
                "Capability refs cannot be resolved by this workflow runtime."
            )
        grants = tuple(resolver(assignment))
        if tuple(grant.grant_id for grant in grants) != assignment.capability_refs:
            raise AssistantStateCompatibilityError(
                "Capability refs do not match resolved workflow grants."
            )
        if any(grant.agent_id != assignment.agent_id for grant in grants):
            raise AssistantStateCompatibilityError(
                "Capability grant owner does not match workflow assignment."
            )
        return grants

    @staticmethod
    def _validate_assignment_child(
        assignment: WorkflowProfileAssignment,
        child: AssistantTurnState,
        services: WorkflowGraphRuntimeServices,
    ) -> None:
        registry = services.tool_registry
        if not registry.sealed or registry.generation is None:
            raise AssistantStateCompatibilityError(
                "Workflow Tool registry must be sealed."
            )
        if assignment.tool_scope_ref != registry.generation:
            raise AssistantStateCompatibilityError(
                "Tool scope does not match the current sealed Registry."
            )
        registered = {spec.name for spec in registry.list_specs()}
        available = set(assignment.available_tool_names)
        explicit = set(assignment.explicit_tool_allowlist)
        if not available.issubset(registered) or (
            explicit and not available.issubset(explicit)
        ):
            raise AssistantStateCompatibilityError(
                "Workflow Tool scope is not available in this runtime."
            )
        request = cast(Mapping[str, Any], child["request"])
        run = cast(Mapping[str, Any], child["run"])
        if (
            child["profile"] != assignment.profile
            or request.get("user_id") != assignment.user_id
            or request.get("session_id") != assignment.session_id
            or run.get("agent_id") != assignment.agent_id
            or run.get("run_id") != assignment.run_id
            or run.get("trace_id") != assignment.trace_id
        ):
            raise AssistantStateCompatibilityError(
                "Child owner or invocation identity does not match assignment."
            )
        if tuple(child.get("capability_refs", ())) != assignment.capability_refs:
            raise AssistantStateCompatibilityError(
                "Child capability refs do not match assignment."
            )
        context_refs = tuple(child.get("context_refs", ()))
        if len(context_refs) != 1 or context_refs[0].get("ref") != assignment.assignment_ref:
            raise AssistantStateCompatibilityError(
                "Child assignment reference does not match outer assignment."
            )
        child_catalog = cast(Mapping[str, Any], child["catalog"])
        if tuple(child_catalog.get("available_tool_names", ())) != assignment.available_tool_names:
            raise AssistantStateCompatibilityError(
                "Child Tool scope does not match outer assignment."
            )
        budget = assignment.budget_slice
        if (
            int(child["max_assistant_iterations"]) > budget.model_calls
            or int(child["max_tool_calls_per_run"]) > budget.tool_calls
            or int(child["max_action_tool_calls_per_run"]) > budget.tool_calls
            or int(child["max_control_tool_calls_per_run"]) > budget.tool_calls
        ):
            raise AssistantStateCompatibilityError(
                "Child execution limits exceed persisted workflow budget slice."
            )


@dataclass(frozen=True)
class _AssignmentStateRefResolver:
    assignment: WorkflowProfileAssignment

    def __call__(
        self,
        persisted: AssistantTurnState,
        runtime_state: AgentState,
    ) -> None:
        child = validate_assistant_turn_state(persisted)
        request = cast(Mapping[str, Any], child["request"])
        run = cast(Mapping[str, Any], child["run"])
        context_refs = tuple(child.get("context_refs", ()))
        if (
            request.get("user_id") != self.assignment.user_id
            or request.get("session_id") != self.assignment.session_id
            or run.get("agent_id") != self.assignment.agent_id
            or run.get("run_id") != self.assignment.run_id
            or run.get("trace_id") != self.assignment.trace_id
            or runtime_state.user_id != self.assignment.user_id
            or runtime_state.session_id != self.assignment.session_id
            or runtime_state.agent_id != self.assignment.agent_id
            or len(context_refs) != 1
            or context_refs[0].get("ref") != self.assignment.assignment_ref
            or tuple(child.get("capability_refs", ()))
            != self.assignment.capability_refs
        ):
            raise AssistantStateCompatibilityError(
                "Child checkpoint facts do not match outer workflow assignment."
            )


def _request_from_assignment(assignment: WorkflowProfileAssignment) -> UserRequest:
    return UserRequest(
        user_id=assignment.user_id,
        session_id=assignment.session_id,
        text=assignment.objective,
        task_execution_mode="foreground",
        response_style="structured",
        runtime_task_update=RuntimeTaskUpdate(
            action="continue",
            objective=assignment.objective,
            constraints=list(assignment.constraints),
        ),
        metadata={
            "_trusted_workflow_assignment": assignment.model_dump(mode="json"),
            "_trusted_workflow_allowed_tools": list(assignment.available_tool_names),
            "_trusted_graph_profile": assignment.profile,
        },
    )


def _provider_for_profile(
    services: WorkflowGraphRuntimeServices,
    profile: str,
) -> ChatAdapter:
    registry = services.provider_registry
    adapter: object | None = None
    if isinstance(registry, Mapping):
        adapter = registry.get(profile)
    else:
        resolver = getattr(registry, "for_profile", None)
        if callable(resolver):
            adapter = resolver(profile)
        elif callable(getattr(registry, "get", None)):
            adapter = registry.get(profile)  # type: ignore[attr-defined]
    if adapter is None or not callable(getattr(adapter, "chat", None)):
        raise AssistantStateCompatibilityError(
            f"Provider adapter for workflow profile {profile!r} is unavailable."
        )
    return cast(ChatAdapter, adapter)


__all__ = [
    "BranchProfileContextFactory",
    "CapabilityGrantResolver",
    "WorkflowCancelReader",
    "WorkflowGraphRuntimeContext",
    "WorkflowGraphRuntimeServices",
    "WorkflowStreamWriter",
]
