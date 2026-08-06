"""Assistant context assembly contracts."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.runtime.requests import ResponseStyle, UserRequest
from assistant_agent.tools.models import RunToolCatalog, ToolSpec


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
    "skill_index",
    "skill_summary",
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
    """Bounded runtime and observability projection of one rolling video snapshot."""

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
    h264_decode_latency_ms: int | None = Field(default=None, ge=0)
    keyframe_selection_latency_ms: int | None = Field(default=None, ge=0)
    queue_wait_latency_ms: int | None = Field(default=None, ge=0)
    text_embedding_latency_ms: int | None = Field(default=None, ge=0)
    visual_memory_index_latency_ms: int | None = Field(default=None, ge=0)
    semantic_store_write_latency_ms: int | None = Field(default=None, ge=0)
    semantic_publish_latency_ms: int | None = Field(default=None, ge=0)
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
    jpeg_prepare_latency_ms: int | None = Field(default=None, ge=0)
    connection_setup_latency_ms: int | None = Field(default=None, ge=0)
    instruction_update_latency_ms: int | None = Field(default=None, ge=0)
    media_commit_latency_ms: int | None = Field(default=None, ge=0)
    response_first_delta_latency_ms: int | None = Field(default=None, ge=0)
    response_tail_latency_ms: int | None = Field(default=None, ge=0)
    response_latency_ms: int | None = Field(default=None, ge=0)
    result_parse_latency_ms: int | None = Field(default=None, ge=0)


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
    owner_persona_chars: int = Field(default=0, ge=0)
    procedural_guidance_chars: int = Field(default=0, ge=0)
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
    procedural_guidance_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    token_usage_ratio: float = Field(default=0.0, ge=0.0)
    token_budget_source: str = "none"
    provider_prompt_tokens: int = Field(default=0, ge=0)
    provider_completion_tokens: int = Field(default=0, ge=0)
    provider_total_tokens: int = Field(default=0, ge=0)
    accounting_basis: str = "precompile_estimate"


class ContextReportSection(BaseModel):
    """Prompt-safe accounting for one included or transformed context section."""

    chars: int = Field(ge=0)
    estimated_tokens: int | None = Field(default=None, ge=0)
    item_count: int | None = Field(default=None, ge=0)
    compaction: Literal["rolling_summary", "prompt_projection"] | None = None
    trimmed: bool = False
    source: str | None = None
    notes: list[str] = Field(default_factory=list)


class ContextReport(BaseModel):
    """Prompt-safe v2 context compiler report for one LLM call."""

    schema_version: Literal["context_report_v2"]
    sections: dict[str, ContextReportSection] = Field(default_factory=dict)
    compiled_accounting_status: Literal["available", "unavailable"]
    compiled_request_chars: int | None = Field(default=None, ge=0)
    compiled_message_chars: int | None = Field(default=None, ge=0)
    compiled_tool_schema_chars: int | None = Field(default=None, ge=0)
    compiled_response_format_chars: int | None = Field(default=None, ge=0)
    token_accounting_status: Literal["available", "unavailable"]
    compiled_input_tokens: int | None = Field(default=None, ge=0)
    effective_input_limit: int | None = Field(default=None, ge=0)
    selected_tool_names: list[str] = Field(default_factory=list)
    memory_item_ids: list[str] = Field(default_factory=list)
    context_sources: ContextSourceReport | None = None
    compression_stage: str = "none"
    compression_reasons: list[str] = Field(default_factory=list)
    precompile_estimated_chars: int = Field(ge=0)
    precompile_max_chars: int = Field(ge=0)


class ContextPolicy(BaseModel):
    """Context assembly and compaction thresholds."""

    max_context_chars: int = Field(default=12_000, ge=500)
    compact_at_ratio: float = Field(default=0.80, ge=0.0, le=1.0)
    hard_compact_at_ratio: float = Field(default=0.92, ge=0.0, le=1.0)
    keep_recent_turns: int = Field(default=2, ge=1)
    max_tool_result_chars: int = Field(default=1_200, ge=100)


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
    """Session-scoped rolling summary used as context, not long-term memory."""

    schema_version: str = "context_summary_v1"
    summary_text: str = ""
    summary_revision: int = Field(default=0, ge=0)
    covered_turn_count: int = Field(default=0, ge=0)
    source_token_count: int = Field(default=0, ge=0)
    summary_token_count: int = Field(default=0, ge=0)
    compactor_model: str = ""
    last_summarized_run_id: str = ""
    last_summarized_trace_id: str = ""
    # Legacy structured fields remain readable so existing JSONL summaries can
    # be migrated by the next rolling compaction.
    task_state: str = ""
    user_constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    open_todos: list[str] = Field(default_factory=list)
    important_refs: list[str] = Field(default_factory=list)
    dropped_context_note: str = ""
    source_turn_count: int = Field(default=0, ge=0)
    handoff_v2: SessionHandoffV2 | None = None


class ToolCatalogSummary(BaseModel):
    """Summary of prompt ToolSpec selection for trace/debug views."""

    total_tool_count: int = Field(default=0, ge=0)
    prompt_tool_count: int = Field(default=0, ge=0)
    filtered_tool_count: int = Field(default=0, ge=0)
    selected_tool_names: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    registry_generation: str | None = None


class AssistantContextPack(BaseModel):
    """All materials needed to render one assistant loop context."""

    request: UserRequest
    response_style: ResponseStyle = "conversation"
    context_summary: ContextSummary | None = None
    compactor_type: str = "none"
    conversation_text: str = ""
    memory_summaries: list[str] = Field(default_factory=list)
    memory_text: str = ""
    memory_source_ids: list[str] = Field(default_factory=list)
    memory_blocks: list[dict[str, Any]] = Field(default_factory=list)
    realtime_task_state: dict[str, Any] | None = None
    realtime_video_context: RealtimeVideoContext | None = None
    durable_task_state: dict[str, Any] | None = None
    plan_state: AssistantPlanContext = Field(default_factory=AssistantPlanContext)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    tool_specs: list[ToolSpec] = Field(default_factory=list)
    prompt_tool_specs: list[ToolSpec] = Field(default_factory=list)
    run_tool_catalog: RunToolCatalog = Field(default_factory=RunToolCatalog)
    tool_catalog_summary: ToolCatalogSummary = Field(default_factory=ToolCatalogSummary)
    active_skill_ids: list[str] = Field(default_factory=list)
    context_sections: list[ContextSection] = Field(default_factory=list)
    context_source_report: ContextSourceReport = Field(default_factory=ContextSourceReport)
    iteration: int = Field(default=0, ge=0)
    max_iterations: int = Field(default=1, ge=1)
    source_counts: dict[str, int] = Field(default_factory=dict)
    budget: ContextBudgetReport = Field(default_factory=ContextBudgetReport)


class RenderedAssistantContext(BaseModel):
    """Rendered context fragments for native tools and legacy prompt-json tests."""

    prompt_json: str | None = None
    native_context_message: str | None = None
    native_user_message: str | None = None
    sections: list[str] = Field(default_factory=list)
