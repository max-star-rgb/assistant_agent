"""Public aggregate entrypoint for explicit multi-agent routing.

The implementation remains in the existing schema/service/tool layers so the
governance boundaries stay visible. Import application-facing multi-agent
objects from this package when a caller needs one stable discovery point.
"""

from assistant_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentArtifact,
    AgentCommunicationError,
    AgentDirectoryConfig,
    AgentInstance,
    AgentInstanceConfig,
    AgentMessage,
    AgentRouteRequest as DirectoryRouteRequest,
    AgentRouteResult,
    AgentSessionRef,
    AgentTask,
    AgentTaskResult,
)
from assistant_agent.schemas.agent_router import (
    AgentCollaborationMode,
    AgentRouteDecision,
    AgentRouteDelegatedTaskSummary,
    AgentRouteMetadata,
    AgentRouteReason,
    AgentRouteRequest,
    AgentRouteStatus,
)
from assistant_agent.services.agent_communication import (
    AgentCommunicationService,
    create_local_agent_communication_service,
)
from assistant_agent.services.agent_directory import AgentDirectory, default_agent_instance
from assistant_agent.services.agent_router import (
    ROUTER_METADATA_KEY,
    WORKER_AGENT_ID,
    AgentRouter,
    create_default_agent_router,
)
from assistant_agent.services.agent_routing_policy import (
    AgentRoutingDecision,
    AgentRoutingPolicy,
    CapabilityMatchPolicy,
    RoutingTablePolicy,
)
from assistant_agent.services.agent_transports import (
    A2AJsonRpcTransport,
    AgentTransport,
    LocalAgentTransport,
    RemoteAgentAllowlist,
)

__all__ = [
    "A2AJsonRpcTransport",
    "AgentArtifact",
    "AgentCollaborationMode",
    "AgentCommunicationError",
    "AgentCommunicationService",
    "AgentDirectory",
    "AgentDirectoryConfig",
    "AgentInstance",
    "AgentInstanceConfig",
    "AgentMessage",
    "AgentRouteDecision",
    "AgentRouteDelegatedTaskSummary",
    "AgentRouteMetadata",
    "AgentRouteReason",
    "AgentRouteRequest",
    "AgentRouteResult",
    "AgentRouteStatus",
    "AgentRouter",
    "AgentRoutingDecision",
    "AgentRoutingPolicy",
    "AgentSessionRef",
    "AgentTask",
    "AgentTaskResult",
    "AgentTransport",
    "CapabilityMatchPolicy",
    "DEFAULT_AGENT_ID",
    "DirectoryRouteRequest",
    "LocalAgentTransport",
    "ROUTER_METADATA_KEY",
    "RemoteAgentAllowlist",
    "RoutingTablePolicy",
    "WORKER_AGENT_ID",
    "create_default_agent_router",
    "create_local_agent_communication_service",
    "default_agent_instance",
]
