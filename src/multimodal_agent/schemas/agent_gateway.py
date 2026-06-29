"""External request contract for the optional multi-agent gateway."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from multimodal_agent.schemas.requests import UserRequest


AgentCollaborationMode = Literal["single", "controller_delegate"]


class AgentGatewayRunRequest(UserRequest):
    """Request accepted by the optional `/agents/run` gateway entrypoint."""

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
