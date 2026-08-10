"""Intent and task planning schemas."""

from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.planning_contracts import PlanDisplayTitle


IntentName = Literal[
    "direct_chat",
    "image_generation",
    "image_understanding",
    "video_understanding",
    "web_search",
    "web_fetch",
    "shopping_search",
    "multi_step_orchestration",
    "chat",
    "understand_image",
    "understand_video",
    "search_web",
    "fetch_web",
    "read_url",
    "generate_image",
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
    """Agent 任务中的单个计划步骤。"""

    step_id: str = Field(min_length=1)
    display_title: PlanDisplayTitle = None
    action: str = Field(min_length=1)
    tool_name: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    optional: bool = False
    reason: str = ""


class TaskPlan(BaseModel):
    """根据意图构建的可执行任务计划。"""

    goal: str = Field(min_length=1)
    steps: list[TaskStep] = Field(default_factory=list)
    requires_followup: bool = False
    followup_question: str | None = None
