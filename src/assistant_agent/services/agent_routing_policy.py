"""Deterministic routing policy for the optional agent gateway."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from assistant_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentDirectoryConfig,
    AgentRouteRequest,
    AgentRouteResult,
)
from assistant_agent.schemas.agent_gateway import (
    AgentCollaborationMode,
    AgentGatewayRouteReason,
    AgentGatewayRunRequest,
)
from assistant_agent.services.agent_directory import AgentDirectory


class RoutingTablePolicy:
    """Resolve configured capability-to-agent overrides."""

    def __init__(self, routes: Mapping[str, str] | None = None) -> None:
        self._routes = dict(routes or {})

    @classmethod
    def from_config(cls, config: AgentDirectoryConfig) -> "RoutingTablePolicy":
        return cls(config.routing_table)

    def target_for_capability(self, capability: str | None) -> str | None:
        if not capability:
            return None
        return self._routes.get(capability)

    def as_dict(self) -> dict[str, str]:
        return dict(self._routes)


class CapabilityMatchPolicy:
    """Resolve a capability through the directory's enabled agent metadata."""

    def resolve(
        self,
        *,
        directory: AgentDirectory,
        capability: str,
        source_agent_id: str,
    ) -> AgentRouteResult:
        return directory.resolve(
            AgentRouteRequest(
                capability=capability,
                source_agent_id=source_agent_id,
            )
        )


class AgentRoutingDecision(BaseModel):
    """Internal route decision consumed by AgentGateway."""

    route: AgentRouteResult
    reason: AgentGatewayRouteReason
    collaboration_mode: AgentCollaborationMode
    use_controller_runtime: bool = False


class AgentRoutingPolicy:
    """Select a gateway target through deterministic, auditable rules."""

    def __init__(
        self,
        *,
        controller_agent_id: str = DEFAULT_AGENT_ID,
        default_agent_id: str = DEFAULT_AGENT_ID,
        routing_table: Mapping[str, str] | None = None,
        capability_policy: CapabilityMatchPolicy | None = None,
        routing_table_policy: RoutingTablePolicy | None = None,
    ) -> None:
        self.controller_agent_id = controller_agent_id
        self.default_agent_id = default_agent_id
        self.capability_policy = capability_policy or CapabilityMatchPolicy()
        self.routing_table_policy = routing_table_policy or RoutingTablePolicy(routing_table)

    @classmethod
    def from_config(
        cls,
        config: AgentDirectoryConfig,
        *,
        controller_agent_id: str | None = None,
    ) -> "AgentRoutingPolicy":
        """Build policy from static directory config."""

        return cls(
            controller_agent_id=controller_agent_id or config.default_agent_id,
            default_agent_id=config.default_agent_id,
            routing_table=config.routing_table,
        )

    def resolve(
        self,
        request: AgentGatewayRunRequest,
        *,
        directory: AgentDirectory,
        source_agent_id: str | None = None,
    ) -> AgentRoutingDecision:
        """Resolve the initial agent for a gateway request."""

        mode = request.effective_collaboration_mode()
        source = source_agent_id or self.controller_agent_id

        if request.target_agent_id:
            return self._decision(
                route=directory.resolve(
                    AgentRouteRequest(
                        target_agent_id=request.target_agent_id,
                        source_agent_id=source,
                    )
                ),
                reason="explicit_target_agent_id",
                mode=mode,
                request=request,
            )

        if request.capability:
            table_target = self.routing_table_policy.target_for_capability(request.capability)
            if table_target:
                return self._decision(
                    route=directory.resolve(
                        AgentRouteRequest(
                            target_agent_id=table_target,
                            source_agent_id=source,
                        )
                    ),
                    reason="routing_table",
                    mode=mode,
                    request=request,
                )
            return self._decision(
                route=self.capability_policy.resolve(
                    directory=directory,
                    capability=request.capability,
                    source_agent_id=source,
                ),
                reason="capability_match",
                mode=mode,
                request=request,
            )

        if mode == "controller_delegate":
            return self._decision(
                route=directory.resolve(
                    AgentRouteRequest(
                        target_agent_id=self.controller_agent_id,
                        source_agent_id=source,
                    )
                ),
                reason="controller_delegate_default",
                mode=mode,
                request=request,
            )

        return self._decision(
            route=directory.resolve(
                AgentRouteRequest(
                    target_agent_id=self.default_agent_id,
                    source_agent_id=source,
                )
            ),
            reason="default_agent",
            mode=mode,
            request=request,
        )

    def _decision(
        self,
        *,
        route: AgentRouteResult,
        reason: AgentGatewayRouteReason,
        mode: AgentCollaborationMode,
        request: AgentGatewayRunRequest,
    ) -> AgentRoutingDecision:
        agent_id = route.instance.agent_id if route.instance is not None else None
        return AgentRoutingDecision(
            route=route,
            reason=reason,
            collaboration_mode=mode,
            use_controller_runtime=(
                mode == "controller_delegate"
                and agent_id == self.controller_agent_id
                and not request.target_agent_id
                and not request.capability
            ),
        )
