"""Intent and task planning schemas."""

from typing import Literal

from pydantic import BaseModel, Field


IntentName = Literal[
    "direct_chat",
    "image_generation",
    "image_understanding",
    "video_understanding",
    "product_search",
    "price_compare",
    "memory_retrieval",
    "multi_step_orchestration",
    "chat",
    "understand_image",
    "understand_video",
    "search_product",
    "compare_price",
    "generate_image",
    "render_3d",
    "retrieve_memory",
    "save_memory",
    "multi_tool_task",
    "ask_followup",
]


class IntentResult(BaseModel):
    """Detected user intent and its confidence."""

    intent: IntentName
    confidence: float = Field(ge=0.0, le=1.0)
    missing_slots: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class TaskStep(BaseModel):
    """A single planned step in an agent task."""

    step_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    tool_name: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    optional: bool = False


class TaskPlan(BaseModel):
    """Executable task plan built from an intent."""

    goal: str = Field(min_length=1)
    steps: list[TaskStep] = Field(default_factory=list)
    requires_followup: bool = False
    followup_question: str | None = None
