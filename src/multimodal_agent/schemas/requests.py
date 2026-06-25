"""Request and response schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class UserRequest(BaseModel):
    """Normalized user input for one agent run."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    text: str | None = None
    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    audio_id: str | None = None
    execution_strategy: Literal["react", "plan_and_solve"] = "react"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    """Structured response produced by the agent."""

    message: str = Field(min_length=1)
    data: dict[str, Any] | None = None
    followup_question: str | None = None
    output_refs: list[str] = Field(default_factory=list)
