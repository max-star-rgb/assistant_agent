"""Durable task plan schemas."""

from pydantic import BaseModel, Field

from assistant_agent.planning_contracts import PlanDisplayTitle


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
