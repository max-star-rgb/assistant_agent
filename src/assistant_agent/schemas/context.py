"""Assistant context assembly contracts."""

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSpec


class AssistantPlanContext(BaseModel):
    """Serializable plan-mode context exposed to prompt renderers."""

    plan_mode_active: bool = False
    plan_status: str = "none"
    current_step_id: str | None = None
    plan_revision_count: int = Field(default=0, ge=0)
    current_plan: dict[str, Any] | None = None


class ContextBudgetReport(BaseModel):
    """Approximate character and optional token budget for one assistant context pack."""

    request_chars: int = Field(default=0, ge=0)
    conversation_chars: int = Field(default=0, ge=0)
    memory_chars: int = Field(default=0, ge=0)
    realtime_task_state_chars: int = Field(default=0, ge=0)
    plan_chars: int = Field(default=0, ge=0)
    observations_chars: int = Field(default=0, ge=0)
    tool_spec_chars: int = Field(default=0, ge=0)
    tool_capability_chars: int = Field(default=0, ge=0)
    total_chars: int = Field(default=0, ge=0)
    max_chars: int = Field(default=0, ge=0)
    over_budget: bool = False
    context_usage_ratio: float = Field(default=0.0, ge=0.0)
    compaction_triggered: bool = False
    trimmed_chars: int = Field(default=0, ge=0)
    trimmed_sections: list[str] = Field(default_factory=list)
    compression_stage: str = "none"
    compression_reasons: list[str] = Field(default_factory=list)
    request_tokens: int = Field(default=0, ge=0)
    conversation_tokens: int = Field(default=0, ge=0)
    memory_tokens: int = Field(default=0, ge=0)
    plan_tokens: int = Field(default=0, ge=0)
    observations_tokens: int = Field(default=0, ge=0)
    tool_spec_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    token_usage_ratio: float = Field(default=0.0, ge=0.0)
    token_budget_source: str = "none"
    provider_prompt_tokens: int = Field(default=0, ge=0)
    provider_completion_tokens: int = Field(default=0, ge=0)
    provider_total_tokens: int = Field(default=0, ge=0)


class ContextReportSection(BaseModel):
    """Prompt-safe context compiler section accounting."""

    chars: int = Field(default=0, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    item_count: int = Field(default=0, ge=0)
    included: bool = False
    compacted: bool = False
    trimmed: bool = False
    source: str = ""
    notes: list[str] = Field(default_factory=list)


class SkillExposureSkip(BaseModel):
    """Prompt-safe reason why a skill-style capability was not exposed."""

    skill_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    tool_name: str | None = None
    permission: str | None = None


class SkillExposureReport(BaseModel):
    """Prompt-safe Skill System v1 exposure report."""

    schema_version: str = "skill_report_v1"
    loaded_skill_ids: list[str] = Field(default_factory=list)
    selected_skill_ids: list[str] = Field(default_factory=list)
    skipped: list[SkillExposureSkip] = Field(default_factory=list)
    builtin_fallback_skill_ids: list[str] = Field(default_factory=list)
    override_skill_ids: list[str] = Field(default_factory=list)
    governed_tool_names: list[str] = Field(default_factory=list)
    permission_issue_count: int = Field(default=0, ge=0)
    unavailable_tool_count: int = Field(default=0, ge=0)


class ContextReport(BaseModel):
    """Prompt-safe v1 context compiler report for one LLM call."""

    schema_version: str = "context_report_v1"
    sections: dict[str, ContextReportSection] = Field(default_factory=dict)
    total_chars: int = Field(default=0, ge=0)
    max_chars: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    selected_tool_names: list[str] = Field(default_factory=list)
    memory_item_ids: list[str] = Field(default_factory=list)
    skill_report: SkillExposureReport = Field(default_factory=SkillExposureReport)
    compression_stage: str = "none"
    compression_reasons: list[str] = Field(default_factory=list)
    was_compacted: bool = False


class ContextPolicy(BaseModel):
    """Context assembly and compaction thresholds."""

    max_context_chars: int = Field(default=12_000, ge=500)
    compact_at_ratio: float = Field(default=0.80, ge=0.0, le=1.0)
    hard_compact_at_ratio: float = Field(default=0.92, ge=0.0, le=1.0)
    keep_recent_turns: int = Field(default=2, ge=1)
    max_tool_result_chars: int = Field(default=1_200, ge=100)
    max_memory_context_chars: int = Field(default=500, ge=50)


class ContextSummary(BaseModel):
    """Session-scoped semantic summary used as current context, not long-term memory."""

    task_state: str = ""
    user_constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    open_todos: list[str] = Field(default_factory=list)
    important_refs: list[str] = Field(default_factory=list)
    dropped_context_note: str = ""
    source_turn_count: int = Field(default=0, ge=0)


class ToolCatalogSummary(BaseModel):
    """Summary of prompt tool-spec recall for trace/debug views."""

    total_tool_count: int = Field(default=0, ge=0)
    prompt_tool_count: int = Field(default=0, ge=0)
    filtered_tool_count: int = Field(default=0, ge=0)
    selected_tool_names: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    fallback_used: bool = False


class ToolCapabilityDescriptor(BaseModel):
    """Prompt-safe skill-style capability descriptor backed by governed tools."""

    name: str = Field(min_length=1)
    description: str = ""
    governed_tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    required_inputs_by_tool: dict[str, list[str]] = Field(default_factory=dict)
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    safe_examples: list[str] = Field(default_factory=list)
    runtime_constraints: list[str] = Field(default_factory=list)


class ToolCapabilityCatalogSelection(BaseModel):
    """Selected capability descriptors for one assistant context pack."""

    capabilities: list[ToolCapabilityDescriptor] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    skill_report: SkillExposureReport = Field(default_factory=SkillExposureReport)


class AssistantContextPack(BaseModel):
    """All materials needed to render one assistant loop context."""

    request: UserRequest
    context_summary: ContextSummary | None = None
    compactor_type: str = "none"
    conversation_text: str = ""
    memory_summaries: list[str] = Field(default_factory=list)
    memory_text: str = ""
    memory_blocks: list[dict[str, Any]] = Field(default_factory=list)
    realtime_task_state: dict[str, Any] | None = None
    plan_state: AssistantPlanContext = Field(default_factory=AssistantPlanContext)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    tool_specs: list[ToolSpec] = Field(default_factory=list)
    prompt_tool_specs: list[ToolSpec] = Field(default_factory=list)
    tool_catalog_summary: ToolCatalogSummary = Field(default_factory=ToolCatalogSummary)
    tool_capabilities: list[ToolCapabilityDescriptor] = Field(default_factory=list)
    skill_report: SkillExposureReport = Field(default_factory=SkillExposureReport)
    iteration: int = Field(default=0, ge=0)
    max_iterations: int = Field(default=1, ge=1)
    source_counts: dict[str, int] = Field(default_factory=dict)
    budget: ContextBudgetReport = Field(default_factory=ContextBudgetReport)


class RenderedAssistantContext(BaseModel):
    """Rendered context fragments for native tools and legacy prompt-json tests."""

    prompt_json: str | None = None
    native_user_message: str | None = None
    final_only_prompt: str | None = None
    sections: list[str] = Field(default_factory=list)
