"""Schemas for intent router adapter requests."""

from pydantic import BaseModel, Field

from assistant_agent.runtime.capability_models import CapabilityName
from assistant_agent.runtime.requests import UserRequest


class IntentRouterRequest(BaseModel):
    """Normalized request passed to rule, mock, or optional LLM intent routers."""

    user_query: str | None = None
    has_image: bool = False
    has_video: bool = False
    has_audio: bool = False
    memory_context: str = ""
    available_capabilities: list[CapabilityName] = Field(default_factory=list)
    current_state_summary: str = ""
    request: UserRequest

    @classmethod
    def from_user_request(
        cls,
        request: UserRequest,
        available_capabilities: list[CapabilityName] | None = None,
        memory_context: str = "",
        current_state_summary: str = "",
    ) -> "IntentRouterRequest":
        return cls(
            user_query=request.text,
            has_image=bool(request.image_ids),
            has_video=bool(request.video_ids),
            has_audio=bool(request.audio_id),
            memory_context=memory_context,
            available_capabilities=available_capabilities or [],
            current_state_summary=current_state_summary,
            request=request,
        )
