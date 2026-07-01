"""External request contract for the optional multi-agent gateway."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.schemas.requests import UserRequest


AgentCollaborationMode = Literal["single", "controller_delegate"]
AgentGatewayRouteReason = Literal[
    "explicit_target_agent_id",
    "capability_match",
    "routing_table",
    "controller_delegate_default",
    "default_agent",
]
AgentGatewayRouteStatus = Literal["routed", "failed"]


class AgentGatewayDelegatedTaskSummary(BaseModel):
    """Public summary for one delegated child task."""

    task_id: str | None = None
    target_agent_id: str | None = None
    status: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    artifact_count: int = 0
    error_codes: list[str] = Field(default_factory=list)


class AgentGatewayRouteDecision(BaseModel):
    """Deterministic route decision exposed by the gateway control plane."""

    selected_agent_id: str | None = None
    requested_target_agent_id: str | None = None
    requested_capability: str | None = None
    collaboration_mode: AgentCollaborationMode
    reason: AgentGatewayRouteReason
    status: AgentGatewayRouteStatus
    delegation_enabled: bool = False
    error_code: str | None = None
    error_message: str | None = None


class AgentGatewayRunMetadata(BaseModel):
    """Stable gateway metadata embedded in AgentRunResponse data/runtime_info."""

    route_decision: AgentGatewayRouteDecision
    delegated_tasks: list[AgentGatewayDelegatedTaskSummary] = Field(default_factory=list)
    route: dict[str, Any] | None = None

    def public_payload(self) -> dict[str, Any]:
        """Return metadata with legacy flat keys retained for compatibility."""

        payload = self.model_dump(mode="json")
        decision = self.route_decision
        payload.update(
            {
                "agent_id": decision.selected_agent_id,
                "collaboration_mode": decision.collaboration_mode,
                "target_agent_id": decision.requested_target_agent_id,
                "capability": decision.requested_capability,
                "delegation_enabled": decision.delegation_enabled,
            }
        )
        return payload


class AgentGatewayRunRequest(UserRequest):
    """Request accepted by the optional `/agents/run` gateway entrypoint."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": "demo_user",
                    "session_id": "demo_session",
                    "text": "Summarize this with the worker agent.",
                    "target_agent_id": "agent.worker",
                    "collaboration_mode": "single",
                },
                {
                    "user_id": "demo_user",
                    "session_id": "demo_session",
                    "text": "Coordinate this task and delegate if useful.",
                    "collaboration_mode": "controller_delegate",
                },
                {
                    "user_id": "auth_bound_user",
                    "session_id": "body_session",
                    "text": "Run through the gateway with header-auth pilot enabled.",
                    "target_agent_id": "agent.worker",
                    "collaboration_mode": "single",
                    "metadata": {
                        "auth_contract": (
                            "When MULTIMODAL_AGENT_AUTH_HEADER_ENABLED is set, "
                            "X-Multimodal-Agent-User-Id must match this user_id; "
                            "X-Multimodal-Agent-Session-Id becomes the bound session."
                        )
                    },
                },
            ]
        }
    )

    target_agent_id: str | None = None
    capability: str | None = None
    collaboration_mode: AgentCollaborationMode = "single"
    mode: AgentCollaborationMode | None = Field(
        default=None,
        description="Compatibility alias for collaboration_mode.",
    )

    def effective_collaboration_mode(self) -> AgentCollaborationMode:
        return self.mode or self.collaboration_mode

    def to_user_request(self, *, metadata: dict[str, Any] | None = None) -> UserRequest:
        """Drop gateway-only fields before entering an AgentGraphRuntime."""

        return UserRequest(
            user_id=self.user_id,
            session_id=self.session_id,
            text=self.text,
            image_ids=list(self.image_ids),
            video_ids=list(self.video_ids),
            audio_id=self.audio_id,
            execution_strategy=self.execution_strategy,
            metadata=dict(self.metadata if metadata is None else metadata),
        )
