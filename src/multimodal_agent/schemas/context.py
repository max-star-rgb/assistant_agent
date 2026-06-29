"""Assistant context assembly contracts."""

from typing import Any

from pydantic import BaseModel, Field

from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolSpec


class AssistantPlanContext(BaseModel):
    """Serializable plan-mode context exposed to prompt renderers."""

    plan_mode_active: bool = False
    plan_status: str = "none"
    current_step_id: str | None = None
    plan_revision_count: int = Field(default=0, ge=0)
    current_plan: dict[str, Any] | None = None


class ContextBudgetReport(BaseModel):
    """Approximate character budget for one assistant context pack."""

    request_chars: int = Field(default=0, ge=0)
    conversation_chars: int = Field(default=0, ge=0)
    memory_chars: int = Field(default=0, ge=0)
    plan_chars: int = Field(default=0, ge=0)
    observations_chars: int = Field(default=0, ge=0)
    tool_spec_chars: int = Field(default=0, ge=0)
    total_chars: int = Field(default=0, ge=0)
    max_chars: int = Field(default=0, ge=0)
    over_budget: bool = False
    trimmed_chars: int = Field(default=0, ge=0)
    trimmed_sections: list[str] = Field(default_factory=list)


class ToolCatalogSummary(BaseModel):
    """Summary of prompt tool-spec recall for trace/debug views."""

    total_tool_count: int = Field(default=0, ge=0)
    prompt_tool_count: int = Field(default=0, ge=0)
    filtered_tool_count: int = Field(default=0, ge=0)
    selected_tool_names: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    fallback_used: bool = False


class AssistantContextPack(BaseModel):
    """All materials needed to render one assistant loop context."""

    request: UserRequest
    conversation_text: str = ""
    memory_summaries: list[str] = Field(default_factory=list)
    memory_text: str = ""
    memory_blocks: list[dict[str, Any]] = Field(default_factory=list)
    plan_state: AssistantPlanContext = Field(default_factory=AssistantPlanContext)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    tool_specs: list[ToolSpec] = Field(default_factory=list)
    prompt_tool_specs: list[ToolSpec] = Field(default_factory=list)
    tool_catalog_summary: ToolCatalogSummary = Field(default_factory=ToolCatalogSummary)
    iteration: int = Field(default=0, ge=0)
    max_iterations: int = Field(default=1, ge=1)
    source_counts: dict[str, int] = Field(default_factory=dict)
    budget: ContextBudgetReport = Field(default_factory=ContextBudgetReport)


class RenderedAssistantContext(BaseModel):
    """Rendered prompt fragments for prompt-json or native-tool modes."""

    prompt_json: str | None = None
    native_user_message: str | None = None
    final_only_prompt: str | None = None
    sections: list[str] = Field(default_factory=list)
