"""Request and response schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.runtime.citations import UrlCitationAnnotation


TaskExecutionMode = Literal["auto", "durable", "foreground"]
ResponseStyle = Literal["conversation", "concise", "structured", "voice"]
AssistantMode = Literal["standard", "deep_research"]


class RuntimeTaskUpdate(BaseModel):
    """Explicit API/runtime contract for updating session task state."""

    action: Literal["continue", "revise", "replace", "complete"]
    objective: str = Field(min_length=1, max_length=1200)
    constraints: list[str] = Field(default_factory=list, max_length=12)


class UserRequest(BaseModel):
    """Normalized user input for one agent run."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    text: str | None = None
    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    audio_id: str | None = None
    assistant_mode: AssistantMode = "standard"
    task_execution_mode: TaskExecutionMode = "auto"
    response_style: ResponseStyle | None = None
    runtime_task_update: RuntimeTaskUpdate | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def resolve_response_style(request: UserRequest) -> ResponseStyle:
    """Resolve style only from explicit request or structured entry facts."""

    if request.response_style is not None:
        return request.response_style
    if request.metadata.get("interaction_mode") == "realtime":
        return "voice"
    return "conversation"


def normalize_task_execution_mode(
    request: UserRequest,
    *,
    durable_tasks_enabled: bool,
) -> UserRequest:
    """Resolve the effective task mode without changing closed-flag behavior."""

    _ = durable_tasks_enabled
    return request


class AgentResponse(BaseModel):
    """Structured response produced by the agent."""

    message: str = Field(min_length=1)
    data: dict[str, Any] | None = None
    followup_question: str | None = None
    output_refs: list[str] = Field(default_factory=list)
    annotations: list[UrlCitationAnnotation] = Field(default_factory=list)
