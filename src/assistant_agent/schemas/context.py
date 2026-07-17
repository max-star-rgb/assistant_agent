"""Assistant context assembly contracts."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import RunToolSet, ToolSpec


ContextAuthority = Literal[
    "system_policy",
    "owner_persona",
    "procedural_guidance",
    "user_profile_data",
    "user_history_evidence",
    "session_state",
    "runtime_evidence",
    "tool_contract",
]
ContextStability = Literal["invariant", "semi_stable", "volatile"]
ContextSectionKind = Literal[
    "soul",
    "user_profile",
    "core_memory",
    "skill_index",
    "skill_body",
    "skill_reference",
    "session_summary",
    "recent_transcript",
    "retrieved_memory",
    "realtime_task_state",
    "durable_task_state",
    "plan_state",
    "tool_observation",
    "tool_schema",
    "tool_capability",
]
ContextSourceType = Literal[
    "runtime",
    "editable_file",
    "memory_service",
    "skill_loader",
    "tool_registry",
]
ContextIdentityScope = Literal["runtime", "local_owner", "user", "project", "tenant"]


RealtimeVideoContextStatus = Literal[
    "ready",
    "refreshing",
    "pending",
    "stale",
    "failed",
    "unavailable",
]


class RealtimeVideoContext(BaseModel):
    """Bounded provider-facing projection of one rolling video snapshot."""

    model_config = ConfigDict(frozen=True)

    status: RealtimeVideoContextStatus = "unavailable"
    summary: str = ""
    objects: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    scene: str | None = None
    snapshot_sequence: int | None = Field(default=None, ge=0)
    target_sequence: int | None = Field(default=None, ge=0)
    sequence_gap: int | None = Field(default=None, ge=0)
    snapshot_age_ms: int | None = Field(default=None, ge=0)
    frame_capture_age_ms: int | None = Field(default=None, ge=0)
    snapshot_publish_age_ms: int | None = Field(default=None, ge=0)
    observation_latency_ms: int | None = Field(default=None, ge=0)
    provider: str | None = None
    model: str | None = None
    pending_count: int = Field(default=0, ge=0)
    in_flight: bool = False
    error_code: str | None = None
    transport: str | None = Field(default=None, max_length=40)
    session_generation: int | None = Field(default=None, ge=1)
    connection_reused: bool | None = None
    reconnect_count: int | None = Field(default=None, ge=0)
    completed_sequence: int | None = Field(default=None, ge=0)
    first_delta_latency_ms: int | None = Field(default=None, ge=0)
    total_observation_latency_ms: int | None = Field(default=None, ge=0)


class ContextSection(BaseModel):
    """Validated prompt material with explicit authority and provenance."""

    schema_version: Literal["context_section_v1"] = "context_section_v1"
    section_id: str = Field(min_length=1)
    kind: ContextSectionKind
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    authority: ContextAuthority
    stability: ContextStability
    source_type: ContextSourceType
    source_ref: str = ""
    source_version: str = ""
    identity_scope: ContextIdentityScope = "runtime"
    priority: int = Field(default=100, ge=0)
    max_chars: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    sensitive: bool = False
    notes: list[str] = Field(default_factory=list)


class ContextSourceIssue(BaseModel):
    """Prompt-safe source loading issue that never contains source content."""

    code: str = Field(min_length=1)
    source_ref: str = ""
    section_id: str | None = None
    recoverable: bool = True
    public_message: str = Field(min_length=1)


class ContextSourceResult(BaseModel):
    """Validated context sections frozen for one assistant run."""

    sections: list[ContextSection] = Field(default_factory=list)
    issues: list[ContextSourceIssue] = Field(default_factory=list)
    used_last_known_good: bool = False


class ContextSourceReport(BaseModel):
    """Redacted source accounting safe for traces and public debugging."""

    schema_version: Literal["context_source_report_v1"] = "context_source_report_v1"
    count_by_kind: dict[str, int] = Field(default_factory=dict)
    chars_by_authority: dict[str, int] = Field(default_factory=dict)
    chars_by_stability: dict[str, int] = Field(default_factory=dict)
    source_issue_count: int = Field(default=0, ge=0)
    source_issue_codes: list[str] = Field(default_factory=list)
    used_last_known_good: bool = False
    source_versions_changed: int = Field(default=0, ge=0)
    omitted_section_count: int = Field(default=0, ge=0)
    cache_layout_version: str = "editable_context_v1"


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
    realtime_video_context_chars: int = Field(default=0, ge=0)
    durable_task_state_chars: int = Field(default=0, ge=0)
    plan_chars: int = Field(default=0, ge=0)
    observations_chars: int = Field(default=0, ge=0)
    tool_spec_chars: int = Field(default=0, ge=0)
    tool_capability_chars: int = Field(default=0, ge=0)
    owner_persona_chars: int = Field(default=0, ge=0)
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
    realtime_video_context_tokens: int = Field(default=0, ge=0)
    durable_task_state_tokens: int = Field(default=0, ge=0)
    plan_tokens: int = Field(default=0, ge=0)
    observations_tokens: int = Field(default=0, ge=0)
    tool_spec_tokens: int = Field(default=0, ge=0)
    owner_persona_tokens: int = Field(default=0, ge=0)
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
    explicit_skill_ids: list[str] = Field(default_factory=list)
    auto_candidate_skill_ids: list[str] = Field(default_factory=list)
    selected_skill_ids: list[str] = Field(default_factory=list)
    skipped: list[SkillExposureSkip] = Field(default_factory=list)
    builtin_fallback_skill_ids: list[str] = Field(default_factory=list)
    override_skill_ids: list[str] = Field(default_factory=list)
    governed_tool_names: list[str] = Field(default_factory=list)
    auto_recall_reasons: dict[str, list[str]] = Field(default_factory=dict)
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
    context_sources: ContextSourceReport = Field(default_factory=ContextSourceReport)
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


class SessionHandoffV2(BaseModel):
    """Compact additive session handoff, kept separate from long-term memory."""

    objective: str = ""
    active_constraints: list[str] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class ContextSummary(BaseModel):
    """Session-scoped semantic summary used as current context, not long-term memory."""

    task_state: str = ""
    user_constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    open_todos: list[str] = Field(default_factory=list)
    important_refs: list[str] = Field(default_factory=list)
    dropped_context_note: str = ""
    source_turn_count: int = Field(default=0, ge=0)
    handoff_v2: SessionHandoffV2 | None = None


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
    realtime_video_context: RealtimeVideoContext | None = None
    durable_task_state: dict[str, Any] | None = None
    plan_state: AssistantPlanContext = Field(default_factory=AssistantPlanContext)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    tool_specs: list[ToolSpec] = Field(default_factory=list)
    prompt_tool_specs: list[ToolSpec] = Field(default_factory=list)
    run_tool_set: RunToolSet = Field(default_factory=RunToolSet)
    tool_catalog_summary: ToolCatalogSummary = Field(default_factory=ToolCatalogSummary)
    tool_capabilities: list[ToolCapabilityDescriptor] = Field(default_factory=list)
    skill_report: SkillExposureReport = Field(default_factory=SkillExposureReport)
    context_sections: list[ContextSection] = Field(default_factory=list)
    context_source_report: ContextSourceReport = Field(default_factory=ContextSourceReport)
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
