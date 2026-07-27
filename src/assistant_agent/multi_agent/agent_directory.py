"""Directory for optional multi-agent instance routing."""

from __future__ import annotations

from collections.abc import Iterable

from assistant_agent.multi_agent.models import (
    DEFAULT_AGENT_ID,
    AgentCommunicationError,
    AgentDirectoryConfig,
    AgentInstance,
    AgentRouteRequest,
    AgentRouteResult,
)
from assistant_agent.tools.ids import (
    IMAGE_GENERATION_TOOL_NAME,
    MEDIA_INSPECT_TOOL_NAME,
    SHOPPING_SEARCH_TOOL_NAME,
)


class AgentDirectory:
    """In-memory registry of enabled agent runtime identities."""

    def __init__(self, instances: Iterable[AgentInstance] | AgentDirectoryConfig | None = None) -> None:
        self._instances: dict[str, AgentInstance] = {}
        resolved_instances = _instances_from_config(instances)
        for instance in resolved_instances:
            self.register(instance)

    @classmethod
    def from_config(cls, config: AgentDirectoryConfig) -> "AgentDirectory":
        """Build a directory from static routing config."""

        return cls(config)

    def register(self, instance: AgentInstance) -> None:
        if instance.agent_id in self._instances:
            raise ValueError(f"Agent already registered: {instance.agent_id}")
        self._instances[instance.agent_id] = instance

    def get(self, agent_id: str) -> AgentInstance | None:
        return self._instances.get(agent_id)

    def list(self, *, include_disabled: bool = False) -> list[AgentInstance]:
        instances = sorted(self._instances.values(), key=lambda item: item.agent_id)
        if include_disabled:
            return instances
        return [instance for instance in instances if instance.enabled]

    def resolve(self, request: AgentRouteRequest) -> AgentRouteResult:
        """Resolve a target agent by explicit id or capability."""

        if request.target_agent_id:
            return self._resolve_agent_id(request.target_agent_id)
        if request.capability:
            matches = [
                instance
                for instance in self.list()
                if request.capability in set(instance.capabilities)
            ]
            if len(matches) == 1:
                return AgentRouteResult(status="routed", instance=matches[0])
            if len(matches) > 1:
                return AgentRouteResult(
                    status="failed",
                    error=AgentCommunicationError(
                        code="agent_route_ambiguous",
                        message=f"Multiple agents match capability: {request.capability}",
                        detail={"capability": request.capability, "agent_ids": [item.agent_id for item in matches]},
                        recoverable=True,
                    ),
                )
            return AgentRouteResult(
                status="failed",
                error=AgentCommunicationError(
                    code="agent_capability_not_found",
                    message=f"No enabled agent supports capability: {request.capability}",
                    detail={"capability": request.capability},
                    recoverable=True,
                ),
            )
        return self._resolve_agent_id(DEFAULT_AGENT_ID)

    def _resolve_agent_id(self, agent_id: str) -> AgentRouteResult:
        instance = self.get(agent_id)
        if instance is None:
            return AgentRouteResult(
                status="failed",
                error=AgentCommunicationError(
                    code="agent_not_found",
                    message=f"Agent not found: {agent_id}",
                    detail={"agent_id": agent_id},
                    recoverable=True,
                ),
            )
        if not instance.enabled:
            return AgentRouteResult(
                status="failed",
                error=AgentCommunicationError(
                    code="agent_disabled",
                    message=f"Agent is disabled: {agent_id}",
                    detail={"agent_id": agent_id},
                    recoverable=True,
                ),
            )
        return AgentRouteResult(status="routed", instance=instance)


def default_agent_instance(
    *,
    can_delegate: bool = False,
    allowed_targets: list[str] | None = None,
) -> AgentInstance:
    """Return the default single-agent runtime identity."""

    return AgentInstance(
        agent_id=DEFAULT_AGENT_ID,
        display_name="Default Agent",
        description="Default local AgentGraphRuntime instance.",
        role="controller",
        capabilities=[
            "chat",
            "tool_calling",
            MEDIA_INSPECT_TOOL_NAME,
            SHOPPING_SEARCH_TOOL_NAME,
            IMAGE_GENERATION_TOOL_NAME,
        ],
        transports=["local"],
        can_delegate=can_delegate,
        allowed_targets=list(allowed_targets or []),
        metadata={"default": True, "offline": True},
    )


def _instances_from_config(
    instances: Iterable[AgentInstance] | AgentDirectoryConfig | None,
) -> list[AgentInstance]:
    if instances is None:
        return [default_agent_instance()]
    if isinstance(instances, AgentDirectoryConfig):
        if not instances.instances:
            return [default_agent_instance()]
        return [config.to_instance() for config in instances.instances]
    return list(instances)
