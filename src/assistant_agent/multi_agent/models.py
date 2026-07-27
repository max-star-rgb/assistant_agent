"""Internal contracts for optional agent-to-agent communication."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.identifiers import new_prefixed_uuid7


AgentTransportName = Literal["local", "a2a_json_rpc"]
AgentTaskStatus = Literal["created", "running", "completed", "failed", "cancelled"]
AgentMessageRole = Literal["user", "assistant", "system", "tool"]
AgentArtifactKind = Literal["text", "data", "output_ref", "error"]
AgentDelegationAuditEventType = Literal[
    "delegation_requested",
    "delegation_rejected",
    "delegation_dispatched",
    "delegation_completed",
]


DEFAULT_AGENT_ID = "agent.default"


def new_agent_task_id() -> str:
    """Create an internal agent task identifier."""

    return new_prefixed_uuid7("agent_task")


def new_agent_correlation_id() -> str:
    """Create a correlation identifier for cross-agent routing."""

    return new_prefixed_uuid7("agent_corr")


def new_agent_artifact_id() -> str:
    """Create an internal agent artifact identifier."""

    return new_prefixed_uuid7("agent_artifact")


class AgentCommunicationError(BaseModel):
    """Structured error returned by agent communication boundaries."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False


class AgentInstance(BaseModel):
    """One configured agent runtime identity."""

    agent_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str = ""
    role: str = "worker"
    capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True
    transports: list[AgentTransportName] = Field(default_factory=lambda: ["local"])
    endpoint_url: str | None = None
    can_delegate: bool = False
    allowed_targets: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInstanceConfig(BaseModel):
    """Configuration entry used to build an agent directory."""

    agent_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str = ""
    role: str = "worker"
    capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True
    transports: list[AgentTransportName] = Field(default_factory=lambda: ["local"])
    endpoint_url: str | None = None
    can_delegate: bool = False
    allowed_targets: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_instance(self) -> AgentInstance:
        """Convert static config into a runtime directory identity."""

        return AgentInstance(
            agent_id=self.agent_id,
            display_name=self.display_name,
            description=self.description,
            role=self.role,
            capabilities=list(self.capabilities),
            enabled=self.enabled,
            transports=list(self.transports),
            endpoint_url=self.endpoint_url,
            can_delegate=self.can_delegate,
            allowed_targets=list(self.allowed_targets),
            metadata=dict(self.metadata),
        )


class AgentDirectoryConfig(BaseModel):
    """Static config for deterministic local agent routing."""

    instances: list[AgentInstanceConfig] = Field(default_factory=list)
    default_agent_id: str = DEFAULT_AGENT_ID
    routing_table: dict[str, str] = Field(
        default_factory=dict,
        description="Capability name to target agent id mapping.",
    )


class AgentRouteRequest(BaseModel):
    """Routing request for selecting a target agent."""

    target_agent_id: str | None = None
    capability: str | None = None
    source_agent_id: str = DEFAULT_AGENT_ID


class AgentRouteResult(BaseModel):
    """Result of resolving an agent route."""

    status: Literal["routed", "failed"]
    instance: AgentInstance | None = None
    error: AgentCommunicationError | None = None


class AgentSessionRef(BaseModel):
    """User/session identity propagated across agent boundaries."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    parent_run_id: str | None = None
    parent_trace_id: str | None = None
    correlation_id: str = Field(default_factory=new_agent_correlation_id)


class AgentMessage(BaseModel):
    """Protocol-neutral message passed between agent instances."""

    role: AgentMessageRole = "user"
    text: str | None = None
    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    audio_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTask(BaseModel):
    """Protocol-neutral task envelope for one delegated agent run."""

    task_id: str = Field(default_factory=new_agent_task_id)
    source_agent_id: str = DEFAULT_AGENT_ID
    target_agent_id: str = DEFAULT_AGENT_ID
    session: AgentSessionRef
    message: AgentMessage
    timeout_ms: int = Field(default=30_000, ge=1)
    delegation_depth: int = Field(default=0, ge=0)
    max_delegation_depth: int = Field(default=1, ge=0)
    token_budget: int | None = Field(default=None, ge=0)
    tool_budget: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentDelegationAuditEvent(BaseModel):
    """Redacted audit event for one delegation policy decision."""

    event_type: AgentDelegationAuditEventType
    task_id: str = Field(min_length=1)
    source_agent_id: str = Field(min_length=1)
    target_agent_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    status: Literal["allowed", "blocked", "completed", "failed", "cancelled"]
    policy_code: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AgentArtifact(BaseModel):
    """Structured result item from a delegated agent task."""

    artifact_id: str = Field(default_factory=new_agent_artifact_id)
    kind: AgentArtifactKind = "text"
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    output_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTaskResult(BaseModel):
    """Normalized result from an agent communication transport."""

    task_id: str = Field(min_length=1)
    target_agent_id: str = Field(min_length=1)
    status: Literal["completed", "failed", "cancelled"]
    artifacts: list[AgentArtifact] = Field(default_factory=list)
    run_id: str | None = None
    trace_id: str | None = None
    errors: list[AgentCommunicationError] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
