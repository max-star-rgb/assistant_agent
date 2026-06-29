"""Service boundary for optional agent-to-agent communication."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from multimodal_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentCommunicationError,
    AgentInstance,
    AgentMessage,
    AgentRouteRequest,
    AgentSessionRef,
    AgentTask,
    AgentTaskResult,
)
from multimodal_agent.services.agent_delegation_context import (
    ArtifactSummaryBuilder,
    DelegationContextBuilder,
)
from multimodal_agent.services.agent_delegation_policy import AgentDelegationPolicy
from multimodal_agent.services.agent_directory import AgentDirectory, default_agent_instance
from multimodal_agent.services.agent_transports import AgentTransport, LocalAgentTransport

if TYPE_CHECKING:
    from multimodal_agent.agent.runtime import AgentGraphRuntime


class AgentCommunicationService:
    """Route protocol-neutral tasks to enabled agent instances."""

    def __init__(
        self,
        *,
        directory: AgentDirectory | None = None,
        transports: Iterable[AgentTransport] | None = None,
        delegation_policy: AgentDelegationPolicy | None = None,
        context_builder: DelegationContextBuilder | None = None,
        artifact_summary_builder: ArtifactSummaryBuilder | None = None,
    ) -> None:
        self.directory = directory or AgentDirectory()
        self._transports = {transport.name: transport for transport in transports or []}
        self.delegation_policy = delegation_policy or AgentDelegationPolicy()
        self.context_builder = context_builder or DelegationContextBuilder()
        self.artifact_summary_builder = artifact_summary_builder or ArtifactSummaryBuilder()

    def send_message(
        self,
        *,
        target_agent_id: str = DEFAULT_AGENT_ID,
        message: AgentMessage,
        session: AgentSessionRef,
        source_agent_id: str = DEFAULT_AGENT_ID,
        timeout_ms: int = 30_000,
        delegation_depth: int = 0,
        max_delegation_depth: int = 1,
        token_budget: int | None = None,
        tool_budget: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentTaskResult:
        """Build and send a task from a single message."""

        return self.send_task(
            AgentTask(
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                session=session,
                message=message,
                timeout_ms=timeout_ms,
                delegation_depth=delegation_depth,
                max_delegation_depth=max_delegation_depth,
                token_budget=token_budget,
                tool_budget=tool_budget,
                metadata=dict(metadata or {}),
            )
        )

    def send_task(self, task: AgentTask) -> AgentTaskResult:
        """Resolve a target agent and deliver one task through an enabled transport."""

        policy = self.delegation_policy.validate(task, directory=self.directory)
        task = task.model_copy(update={"metadata": policy.metadata}, deep=True)
        if not policy.accepted:
            error = policy.error or AgentCommunicationError(
                code="agent_delegation_policy_rejected",
                message="Agent delegation rejected by policy.",
                recoverable=True,
            )
            return _failed_task(
                task,
                error.code,
                error.message,
                detail=error.detail,
                recoverable=error.recoverable,
                audit_events=[policy.audit_event],
            )
        context_pack = self.context_builder.build(task)
        task = context_pack.task
        route = self.directory.resolve(
            AgentRouteRequest(
                target_agent_id=task.target_agent_id,
                source_agent_id=task.source_agent_id,
            )
        )
        if route.status != "routed" or route.instance is None:
            error = route.error or AgentCommunicationError(
                code="agent_route_failed",
                message="Agent route failed.",
                recoverable=True,
            )
            return _failed_task(
                task,
                error.code,
                error.message,
                detail=error.detail,
                recoverable=error.recoverable,
                audit_events=[policy.audit_event],
            )
        transport = self._select_transport(route.instance.transports)
        if transport is None:
            return _failed_task(
                task,
                "agent_transport_unavailable",
                "No enabled transport is available for the target agent.",
                detail={"agent_id": task.target_agent_id, "transports": list(route.instance.transports)},
                recoverable=True,
                audit_events=[policy.audit_event],
            )
        started_at = time.monotonic()
        result = transport.send_task(task, instance=route.instance)
        result = _with_latency(result, started_at=started_at)
        return _with_audit_events(
            result,
            [
                policy.audit_event,
                self.delegation_policy.completion_event(task, status=result.status),
            ],
            task=task,
            artifact_summary_builder=self.artifact_summary_builder,
        )

    def _select_transport(self, transport_names: list[str]) -> AgentTransport | None:
        for name in transport_names:
            transport = self._transports.get(name)
            if transport is not None:
                return transport
        return None


def create_default_agent_communication_service(
    runtime: AgentGraphRuntime,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    directory: AgentDirectory | None = None,
) -> AgentCommunicationService:
    """Create an offline local service for the default agent runtime."""

    return AgentCommunicationService(
        directory=directory or AgentDirectory(),
        transports=[LocalAgentTransport({agent_id: runtime})],
    )


def create_local_agent_communication_service(
    runtimes: Mapping[str, Any],
    *,
    instances: Iterable[AgentInstance] | None = None,
) -> AgentCommunicationService:
    """Create an offline local communication service for multiple runtimes."""

    if not runtimes:
        raise ValueError("at least one local runtime is required")
    instance_map = {instance.agent_id: instance for instance in instances or []}
    for agent_id in sorted(runtimes):
        instance_map.setdefault(agent_id, _default_local_instance(agent_id, runtimes.keys()))
    return AgentCommunicationService(
        directory=AgentDirectory(list(instance_map.values())),
        transports=[LocalAgentTransport(dict(runtimes))],
    )


def _default_local_instance(agent_id: str, runtime_agent_ids: Iterable[str] | None = None) -> AgentInstance:
    if agent_id == DEFAULT_AGENT_ID:
        allowed_targets = sorted(item for item in (runtime_agent_ids or []) if item != DEFAULT_AGENT_ID)
        return default_agent_instance(can_delegate=bool(allowed_targets), allowed_targets=allowed_targets)
    return AgentInstance(
        agent_id=agent_id,
        display_name=_display_name(agent_id),
        description="Local same-process agent runtime instance.",
        role="worker",
        capabilities=["chat", "tool_calling"],
        transports=["local"],
        can_delegate=False,
        allowed_targets=[],
        metadata={"offline": True, "local": True},
    )


def _display_name(agent_id: str) -> str:
    label = agent_id.removeprefix("agent.").replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in label.split()) or agent_id


def _failed_task(
    task: AgentTask,
    code: str,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
    recoverable: bool = False,
    audit_events: list[Any] | None = None,
) -> AgentTaskResult:
    metadata = {
        "source_agent_id": task.source_agent_id,
        "target_agent_id": task.target_agent_id,
        "correlation_id": task.session.correlation_id,
    }
    if audit_events:
        metadata["delegation_audit"] = [_audit_payload(event) for event in audit_events]
    for key in ("delegation_pairs", "agent_context", "child_context_budget", "tool_result_refs"):
        if key in task.metadata:
            metadata[key] = task.metadata[key]
    return AgentTaskResult(
        task_id=task.task_id,
        target_agent_id=task.target_agent_id,
        status="failed",
        errors=[
            AgentCommunicationError(
                code=code,
                message=message,
                detail=detail or {},
                recoverable=recoverable,
            )
        ],
        metadata=metadata,
    )


def _with_audit_events(
    result: AgentTaskResult,
    audit_events: list[Any],
    *,
    task: AgentTask | None = None,
    artifact_summary_builder: ArtifactSummaryBuilder | None = None,
) -> AgentTaskResult:
    metadata = dict(result.metadata)
    metadata["delegation_audit"] = [_audit_payload(event) for event in audit_events]
    if task is not None:
        for key in ("delegation_pairs", "agent_context", "child_context_budget", "tool_result_refs"):
            if key in task.metadata:
                metadata[key] = task.metadata[key]
    if artifact_summary_builder is not None:
        metadata["artifact_summary"] = artifact_summary_builder.build(result)
    return result.model_copy(update={"metadata": metadata}, deep=True)


def _with_latency(result: AgentTaskResult, *, started_at: float) -> AgentTaskResult:
    metadata = dict(result.metadata)
    metadata.setdefault("latency_ms", int((time.monotonic() - started_at) * 1000))
    return result.model_copy(update={"metadata": metadata}, deep=True)


def _audit_payload(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    return dict(event) if isinstance(event, dict) else {"event": str(event)}
