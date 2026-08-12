"""Versioned, checkpoint-safe state contracts for ``AssistantTurnGraph``.

This module deliberately contains no runtime services.  It is the serialization
boundary between the product/runtime models and LangGraph checkpoints.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Mapping, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID
from assistant_agent.runtime.requests import UserRequest, resolve_response_style
from assistant_agent.runtime.state import AgentState


ASSISTANT_GRAPH_NAME = "AssistantTurnGraph"
ASSISTANT_GRAPH_VERSION = "2"
ASSISTANT_STATE_SCHEMA_VERSION = 1

AssistantGraphProfileName = Literal["standard", "planner", "worker", "verifier"]


class AssistantStateCompatibilityError(ValueError):
    """Raised when persisted state cannot be safely consumed by this graph."""

    code = "assistant_state_version_incompatible"

    def __init__(self, message: str = "Assistant graph state version is incompatible.") -> None:
        super().__init__(message)


class _CheckpointModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PersistedMessage(_CheckpointModel):
    role: Literal["system", "user", "assistant", "tool"]
    text: str = Field(max_length=32_000)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)


class PersistedRuntimeTaskFacts(_CheckpointModel):
    action: Literal["continue", "revise", "replace", "complete"]
    objective: str = Field(min_length=1, max_length=1_200)
    constraints: tuple[str, ...] = Field(default=(), max_length=12)


class PersistedMediaRef(_CheckpointModel):
    kind: Literal["image", "video", "audio", "artifact"]
    ref: str = Field(min_length=1, max_length=1_024)


class PersistedRequest(_CheckpointModel):
    user_id: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=512)
    text: str | None = Field(default=None, max_length=32_000)
    assistant_mode: Literal["standard", "deep_research"] = "standard"
    response_style: Literal["conversation", "concise", "structured", "voice"]
    task_execution_mode: Literal["auto", "foreground", "durable"] = "auto"
    messages: tuple[PersistedMessage, ...] = Field(default=(), max_length=128)
    runtime_task_facts: PersistedRuntimeTaskFacts | None = None
    media_refs: tuple[PersistedMediaRef, ...] = Field(default=(), max_length=16)
    capability_refs: tuple[str, ...] = Field(default=(), max_length=64)


class PersistedError(_CheckpointModel):
    code: str | None = Field(default=None, max_length=160)
    message: str = Field(min_length=1, max_length=2_000)
    source: str | None = Field(default=None, max_length=256)
    created_at: str | None = Field(default=None, max_length=64)


class PersistedToolArgument(_CheckpointModel):
    """One named Tool argument encoded as bounded canonical JSON."""

    name: str = Field(min_length=1, max_length=256)
    value_json: str = Field(min_length=1, max_length=16_000)


class PersistedToolCall(_CheckpointModel):
    tool_call_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=256)
    arguments: tuple[PersistedToolArgument, ...] = Field(default=(), max_length=128)
    status: Literal["pending", "running", "succeeded", "failed"]
    started_at: str = Field(min_length=1, max_length=64)
    finished_at: str | None = Field(default=None, max_length=64)
    output_ref: str | None = Field(default=None, max_length=1_024)
    error_summary: str | None = Field(default=None, max_length=2_000)


class PersistedToolResult(_CheckpointModel):
    tool_name: str = Field(min_length=1, max_length=256)
    status: Literal["succeeded", "failed"]
    summary: str = Field(max_length=2_000)
    operation_key: str | None = Field(default=None, max_length=512)
    output_ref: str | None = Field(default=None, max_length=1_024)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=32)
    error_summary: str | None = Field(default=None, max_length=2_000)


class PersistedRun(_CheckpointModel):
    run_id: str = Field(min_length=1, max_length=512)
    trace_id: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=512)
    status: Literal["created", "running", "waiting_user", "completed", "failed", "cancelled"]
    errors: tuple[PersistedError, ...] = Field(default=(), max_length=32)
    tool_calls: tuple[PersistedToolCall, ...] = Field(default=(), max_length=64)
    tool_results: tuple[PersistedToolResult, ...] = Field(default=(), max_length=64)


class PersistedStepOutput(_CheckpointModel):
    step_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=256)
    status: Literal["succeeded", "failed", "rejected"]
    summary: str = Field(max_length=2_000)
    output_ref: str | None = Field(default=None, max_length=1_024)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=32)


class PersistedToolCallRequest(_CheckpointModel):
    provider_call_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=256)
    arguments: tuple[PersistedToolArgument, ...] = Field(default=(), max_length=128)


class PersistedAssistantOutput(_CheckpointModel):
    kind: Literal["text", "tool_calls", "error"]
    text: str | None = Field(default=None, max_length=32_000)
    tool_calls: tuple[PersistedToolCallRequest, ...] = Field(default=(), max_length=64)
    error_code: str | None = Field(default=None, max_length=160)


class PersistedObservationError(_CheckpointModel):
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=2_000)
    retryable: bool = False


class PersistedToolObservation(_CheckpointModel):
    tool_name: str = Field(min_length=1, max_length=256)
    status: Literal["succeeded", "failed", "rejected"]
    summary: str = Field(min_length=1, max_length=2_000)
    outcome: Literal["success", "partial", "empty"] | None = None
    warnings: tuple[str, ...] = Field(default=(), max_length=16)
    is_complete: bool = True
    output_ref: str | None = Field(default=None, max_length=1_024)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=32)
    error: PersistedObservationError | None = None


class PersistedContextRef(_CheckpointModel):
    kind: Literal["context_section", "memory", "perception", "source_issue"]
    ref: str = Field(min_length=1, max_length=1_024)
    source: str | None = Field(default=None, max_length=160)
    version: str | None = Field(default=None, max_length=256)
    status_code: str | None = Field(default=None, max_length=160)


class PersistedRunToolCatalog(_CheckpointModel):
    schema_version: Literal["run_tool_catalog_v1"] = "run_tool_catalog_v1"
    available_tool_names: tuple[str, ...] = Field(default=(), max_length=256)
    selection_reason_codes: tuple[str, ...] = Field(default=(), max_length=256)
    exclusion_reason_codes: tuple[str, ...] = Field(default=(), max_length=512)


class PersistedInterrupt(_CheckpointModel):
    interrupt_id: str = Field(min_length=1, max_length=256)
    kind: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=2_000)


class PersistedCitation(_CheckpointModel):
    source_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=1_000)
    url: str = Field(min_length=1, max_length=4_096)
    start_index: int = Field(ge=0)
    end_index: int = Field(gt=0)


class PersistedResponse(_CheckpointModel):
    message: str = Field(max_length=32_000)
    followup_question: str | None = Field(default=None, max_length=4_000)
    output_refs: tuple[str, ...] = Field(default=(), max_length=64)
    citations: tuple[PersistedCitation, ...] = Field(default=(), max_length=64)


class _AssistantTurnStateModel(_CheckpointModel):
    graph_name: Literal["AssistantTurnGraph"]
    graph_version: Literal["2"]
    state_schema_version: Literal[1]
    profile: AssistantGraphProfileName
    request: PersistedRequest
    run: PersistedRun
    outputs_by_step: tuple[PersistedStepOutput, ...] = Field(default=(), max_length=128)
    current_step_index: int = Field(default=0, ge=0)
    assistant_output: PersistedAssistantOutput | None = None
    pending_tool_calls: tuple[PersistedToolCallRequest, ...] = Field(default=(), max_length=64)
    assistant_iterations: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    action_tool_calls_used: int = Field(default=0, ge=0)
    control_tool_calls_used: int = Field(default=0, ge=0)
    run_phase: str = Field(default="assistant", min_length=1, max_length=160)
    tool_observations: tuple[PersistedToolObservation, ...] = Field(default=(), max_length=64)
    context_refs: tuple[PersistedContextRef, ...] = Field(default=(), max_length=256)
    capability_refs: tuple[str, ...] = Field(default=(), max_length=64)
    catalog: PersistedRunToolCatalog = Field(default_factory=PersistedRunToolCatalog)
    pending_interrupt: PersistedInterrupt | None = None
    final_response: PersistedResponse | None = None
    assistant_stream_started: bool = False
    assistant_stream_finished: bool = False
    tool_stream_started: bool = False
    last_llm_span_id: str | None = Field(default=None, max_length=256)
    last_llm_attempt_kind: str | None = Field(default=None, max_length=160)
    max_assistant_iterations: int = Field(default=0, ge=0)
    max_tool_calls_per_run: int = Field(default=0, ge=0)
    max_action_tool_calls_per_run: int = Field(default=0, ge=0)
    max_control_tool_calls_per_run: int = Field(default=0, ge=0)


class AssistantTurnState(TypedDict):
    """LangGraph channel schema; values are JSON dumps of the strict DTOs above."""

    graph_name: Literal["AssistantTurnGraph"]
    graph_version: Literal["2"]
    state_schema_version: Literal[1]
    profile: AssistantGraphProfileName
    request: PersistedRequest
    run: PersistedRun
    outputs_by_step: tuple[PersistedStepOutput, ...]
    current_step_index: int
    assistant_output: PersistedAssistantOutput | None
    pending_tool_calls: tuple[PersistedToolCallRequest, ...]
    assistant_iterations: int
    tool_calls_used: int
    action_tool_calls_used: int
    control_tool_calls_used: int
    run_phase: str
    tool_observations: tuple[PersistedToolObservation, ...]
    context_refs: tuple[PersistedContextRef, ...]
    capability_refs: tuple[str, ...]
    catalog: PersistedRunToolCatalog
    pending_interrupt: PersistedInterrupt | None
    final_response: PersistedResponse | None
    assistant_stream_started: bool
    assistant_stream_finished: bool
    tool_stream_started: bool
    last_llm_span_id: str | None
    last_llm_attempt_kind: str | None
    max_assistant_iterations: int
    max_tool_calls_per_run: int
    max_action_tool_calls_per_run: int
    max_control_tool_calls_per_run: int


_STATE_ADAPTER = TypeAdapter(_AssistantTurnStateModel)


def persisted_request_from_user_request(request: UserRequest) -> dict[str, object]:
    """Project only explicitly admitted request fields; arbitrary metadata is dropped."""

    media_refs = [
        PersistedMediaRef(kind="image", ref=ref) for ref in request.image_ids
    ]
    media_refs.extend(PersistedMediaRef(kind="video", ref=ref) for ref in request.video_ids)
    if request.audio_id:
        media_refs.append(PersistedMediaRef(kind="audio", ref=request.audio_id))
    messages = _messages_from_request(request)
    runtime_task = request.runtime_task_update
    task_facts = None
    if runtime_task is not None:
        task_facts = PersistedRuntimeTaskFacts(
            action=runtime_task.action,
            objective=runtime_task.objective,
            constraints=tuple(runtime_task.constraints),
        )
    persisted = PersistedRequest(
        user_id=request.user_id,
        session_id=request.session_id,
        text=request.text,
        assistant_mode=request.assistant_mode,
        response_style=resolve_response_style(request),
        task_execution_mode=request.task_execution_mode,
        messages=messages,
        runtime_task_facts=task_facts,
        media_refs=tuple(media_refs),
        capability_refs=(),
    )
    return cast(dict[str, object], persisted.model_dump(mode="json"))


def assistant_turn_state_from_request(
    request: UserRequest,
    *,
    run_id: str,
    trace_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    profile: AssistantGraphProfileName = "standard",
) -> AssistantTurnState:
    """Create a complete new-turn state so checkpoint merge cannot retain old channels."""

    state = AgentState.from_request(
        request,
        run_id=run_id,
        trace_id=trace_id,
        agent_id=agent_id,
    )
    return assistant_turn_state_from_agent_state(state, profile=profile)


def assistant_turn_state_from_agent_state(
    state: AgentState,
    *,
    profile: AssistantGraphProfileName = "standard",
) -> AssistantTurnState:
    """Explicitly project a runtime ``AgentState`` into checkpoint-safe primitives."""

    request_payload = persisted_request_from_user_request(state.request)
    capability_refs = tuple(grant.grant_id for grant in state.capability_grants)
    request_payload["capability_refs"] = list(capability_refs)

    run = PersistedRun(
        run_id=state.run_id,
        trace_id=state.trace_id,
        agent_id=state.agent_id,
        status=state.status,
        errors=tuple(
            PersistedError(
                code=_safe_error_code(error.details.get("code")),
                message=_bounded(error.message, 2_000),
                source=_bounded_optional(error.source, 256),
                created_at=_datetime_text(error.created_at),
            )
            for error in state.errors
        ),
        tool_calls=tuple(_project_tool_call(record) for record in state.tool_calls),
        tool_results=tuple(_project_tool_result(result) for result in state.tool_results),
    )
    catalog = _project_catalog(state.run_tool_catalog)
    context_refs = _project_context_refs(state)
    final_response = _project_response(state.response)
    envelope = _AssistantTurnStateModel(
        graph_name=ASSISTANT_GRAPH_NAME,
        graph_version=ASSISTANT_GRAPH_VERSION,
        state_schema_version=ASSISTANT_STATE_SCHEMA_VERSION,
        profile=profile,
        request=PersistedRequest.model_validate_json(
            json.dumps(request_payload, ensure_ascii=False)
        ),
        run=run,
        context_refs=context_refs,
        capability_refs=capability_refs,
        catalog=catalog,
        final_response=final_response,
    )
    return cast(AssistantTurnState, envelope.model_dump(mode="json"))


def validate_assistant_turn_state(value: Mapping[str, object]) -> AssistantTurnState:
    """Validate a checkpoint payload and fail closed on all version mismatches."""

    if (
        value.get("graph_name") != ASSISTANT_GRAPH_NAME
        or value.get("graph_version") != ASSISTANT_GRAPH_VERSION
        or value.get("state_schema_version") != ASSISTANT_STATE_SCHEMA_VERSION
    ):
        raise AssistantStateCompatibilityError()
    try:
        # Strict DTOs intentionally distinguish Python tuples from lists.  A
        # checkpoint is a JSON document, so validation must use JSON semantics
        # (where bounded tuples are represented by arrays).
        validated = _STATE_ADAPTER.validate_json(
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("assistant_state_invalid") from exc
    return cast(AssistantTurnState, validated.model_dump(mode="json"))


def _messages_from_request(request: UserRequest) -> tuple[PersistedMessage, ...]:
    messages: list[PersistedMessage] = []
    history = request.metadata.get("conversation_history")
    if isinstance(history, list):
        for item in history[:127]:
            if not isinstance(item, Mapping):
                continue
            role = item.get("role")
            text = item.get("text", item.get("content"))
            if role in {"user", "assistant"} and isinstance(text, str):
                messages.append(PersistedMessage(role=role, text=_bounded(text, 32_000)))
    if request.text is not None:
        messages.append(PersistedMessage(role="user", text=request.text))
    return tuple(messages[-128:])


def _project_tool_call(record: object) -> PersistedToolCall:
    raw_input = getattr(record, "input", {})
    arguments: list[PersistedToolArgument] = []
    if isinstance(raw_input, Mapping):
        for name, value in raw_input.items():
            if not isinstance(name, str):
                raise ValueError("assistant_state_tool_argument_name_invalid")
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
            arguments.append(PersistedToolArgument(name=name, value_json=encoded))
    return PersistedToolCall(
        tool_call_id=record.tool_call_id,
        tool_name=record.tool_name,
        arguments=tuple(arguments),
        status=record.status,
        started_at=_datetime_text(record.started_at) or "invalid",
        finished_at=_datetime_text(record.finished_at),
        output_ref=_bounded_optional(record.output_ref, 1_024),
        error_summary=_bounded_optional(record.error_message, 2_000),
    )


def _project_tool_result(result: object) -> PersistedToolResult:
    error = getattr(result, "error", None)
    voice_summary = getattr(result, "voice_summary", None)
    summary = voice_summary if isinstance(voice_summary, str) else error or ""
    contract = getattr(result, "contract", None)
    contract_ref = getattr(contract, "output_ref", None) if contract is not None else None
    return PersistedToolResult(
        tool_name=result.tool_name,
        status="succeeded" if result.success else "failed",
        summary=_bounded(str(summary), 2_000),
        output_ref=_bounded_optional(result.output_ref or contract_ref, 1_024),
        artifact_refs=tuple(
            ref
            for ref in (getattr(result, "raw_data_ref", None),)
            if isinstance(ref, str) and ref
        ),
        error_summary=_bounded_optional(error, 2_000),
    )


def _project_catalog(catalog: object | None) -> PersistedRunToolCatalog:
    if catalog is None:
        return PersistedRunToolCatalog()
    excluded = getattr(catalog, "excluded_reasons", {})
    exclusion_codes: list[str] = []
    if isinstance(excluded, Mapping):
        for tool_name, reasons in excluded.items():
            if isinstance(tool_name, str) and isinstance(reasons, list):
                exclusion_codes.extend(
                    f"{tool_name}:{reason}"
                    for reason in reasons
                    if isinstance(reason, str)
                )
    return PersistedRunToolCatalog(
        schema_version=catalog.schema_version,
        available_tool_names=tuple(catalog.available_tool_names),
        selection_reason_codes=tuple(catalog.selection_reasons),
        exclusion_reason_codes=tuple(exclusion_codes),
    )


def _project_context_refs(state: AgentState) -> tuple[PersistedContextRef, ...]:
    refs: list[PersistedContextRef] = []
    for section in state.context_source_result.sections:
        refs.append(PersistedContextRef(
            kind="context_section",
            ref=section.source_ref or section.section_id,
            source=section.source_type,
            version=section.source_version or None,
        ))
    for issue in state.context_source_result.issues:
        refs.append(PersistedContextRef(
            kind="source_issue",
            ref=issue.source_ref or issue.section_id or issue.code,
            status_code=issue.code,
        ))
    snapshot = state.session_memory_snapshot
    if snapshot is not None:
        refs.extend(
            PersistedContextRef(kind="memory", ref=item.memory_id, source=item.source)
            for item in snapshot.memories
        )
    visual = getattr(state.perception, "visual", None)
    if visual is not None and visual.output_ref:
        refs.append(PersistedContextRef(
            kind="perception",
            ref=visual.output_ref,
            source=visual.media_kind,
        ))
    return tuple(refs[:256])


def _project_response(response: object | None) -> PersistedResponse | None:
    if response is None:
        return None
    return PersistedResponse(
        message=_bounded(response.message, 32_000),
        followup_question=_bounded_optional(response.followup_question, 4_000),
        output_refs=tuple(response.output_refs),
        citations=tuple(
            PersistedCitation(
                source_id=item.source_id,
                title=item.title,
                url=item.url,
                start_index=item.start_index,
                end_index=item.end_index,
            )
            for item in response.annotations
        ),
    )


def _safe_error_code(value: object) -> str | None:
    return _bounded_optional(value if isinstance(value, str) else None, 160)


def _bounded(value: str, limit: int) -> str:
    return value[:limit]


def _bounded_optional(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _datetime_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "ASSISTANT_GRAPH_NAME",
    "ASSISTANT_GRAPH_VERSION",
    "ASSISTANT_STATE_SCHEMA_VERSION",
    "AssistantGraphProfileName",
    "AssistantStateCompatibilityError",
    "AssistantTurnState",
    "PersistedMediaRef",
    "PersistedRequest",
    "PersistedRun",
    "assistant_turn_state_from_agent_state",
    "assistant_turn_state_from_request",
    "persisted_request_from_user_request",
    "validate_assistant_turn_state",
]
