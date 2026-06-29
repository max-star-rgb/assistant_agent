"""Service boundary for optional agent-to-agent communication."""

from __future__ import annotations

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
    ) -> None:
        self.directory = directory or AgentDirectory()
        self._transports = {transport.name: transport for transport in transports or []}

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
                metadata=dict(metadata or {}),
            )
        )

    def send_task(self, task: AgentTask) -> AgentTaskResult:
        """Resolve a target agent and deliver one task through an enabled transport."""

        if task.delegation_depth > task.max_delegation_depth:
            return _failed_task(
                task,
                "agent_delegation_depth_exceeded",
                "Agent delegation depth exceeded.",
                detail={
                    "delegation_depth": task.delegation_depth,
                    "max_delegation_depth": task.max_delegation_depth,
                },
                recoverable=False,
            )
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
            )
        transport = self._select_transport(route.instance.transports)
        if transport is None:
            return _failed_task(
                task,
                "agent_transport_unavailable",
                "No enabled transport is available for the target agent.",
                detail={"agent_id": task.target_agent_id, "transports": list(route.instance.transports)},
                recoverable=True,
            )
        return transport.send_task(task)

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
        instance_map.setdefault(agent_id, _default_local_instance(agent_id))
    return AgentCommunicationService(
        directory=AgentDirectory(list(instance_map.values())),
        transports=[LocalAgentTransport(dict(runtimes))],
    )


def _default_local_instance(agent_id: str) -> AgentInstance:
    if agent_id == DEFAULT_AGENT_ID:
        return default_agent_instance()
    return AgentInstance(
        agent_id=agent_id,
        display_name=_display_name(agent_id),
        description="Local same-process agent runtime instance.",
        capabilities=["chat", "tool_calling"],
        transports=["local"],
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
) -> AgentTaskResult:
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
        metadata={
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "correlation_id": task.session.correlation_id,
        },
    )
