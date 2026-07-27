"""Request and response schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field


TaskExecutionMode = Literal["auto", "durable", "foreground"]


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
    execution_strategy: Literal["react", "plan_and_solve"] = "react"
    task_execution_mode: TaskExecutionMode = "auto"
    runtime_task_update: RuntimeTaskUpdate | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def normalize_task_execution_mode(
    request: UserRequest,
    *,
    durable_tasks_enabled: bool,
) -> UserRequest:
    """Resolve the effective task mode without changing closed-flag behavior."""

    explicit = "task_execution_mode" in request.model_fields_set
    effective = request.task_execution_mode
    if not explicit and durable_tasks_enabled and request.execution_strategy == "plan_and_solve":
        effective = "durable"
    return request.model_copy(update={"task_execution_mode": effective})


class AgentResponse(BaseModel):
    """Structured response produced by the agent."""

    message: str = Field(min_length=1)
    data: dict[str, Any] | None = None
    followup_question: str | None = None
    output_refs: list[str] = Field(default_factory=list)
