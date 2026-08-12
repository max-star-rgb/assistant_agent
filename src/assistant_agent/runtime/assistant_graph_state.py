"""Versioned, checkpoint-safe state contracts for ``AssistantTurnGraph``.

This module deliberately contains no runtime services.  It is the serialization
boundary between the product/runtime models and LangGraph checkpoints.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, TypedDict, cast
from urllib.parse import parse_qsl, urlparse

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID
from assistant_agent.runtime.assistant_graph_profiles import AssistantGraphProfileName
from assistant_agent.runtime.citations import UrlCitationAnnotation
from assistant_agent.runtime.requests import (
    AgentResponse,
    UserRequest,
    resolve_response_style,
)
from assistant_agent.runtime.output_models import AssistantTextOutput, AssistantToolCall
from assistant_agent.runtime.run_phase import RunPhase
from assistant_agent.runtime.state import AgentError, AgentState
from assistant_agent.tools.models import RunToolCatalog, ToolCallRecord, ToolResult
from assistant_agent.tools.observation_safety import sanitize_tool_observation_detail
from assistant_agent.tools.capability_output import (
    CapabilityOutputContract,
    CapabilityOutputError,
)


ASSISTANT_GRAPH_NAME = "AssistantTurnGraph"
ASSISTANT_GRAPH_VERSION = "2"
ASSISTANT_STATE_SCHEMA_VERSION = 1

class AssistantStateCompatibilityError(ValueError):
    """Raised when persisted state cannot be safely consumed by this graph."""

    code = "assistant_state_version_incompatible"

    def __init__(
        self, message: str = "Assistant graph state version is incompatible."
    ) -> None:
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
    capability_contract: "PersistedCapabilityContract | None" = None


class PersistedCapabilityError(_CheckpointModel):
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=2_000)
    recoverable: bool = False


class PersistedCapabilityContract(_CheckpointModel):
    capability: str = Field(min_length=1, max_length=256)
    status: Literal["succeeded", "failed", "partial", "skipped"]
    output_ref: str | None = Field(default=None, max_length=1_024)
    data_facts: tuple[PersistedObservationDetail, ...] = Field(
        default=(), max_length=64
    )
    metadata_facts: tuple[PersistedObservationDetail, ...] = Field(
        default=(), max_length=32
    )
    errors: tuple[PersistedCapabilityError, ...] = Field(default=(), max_length=32)


class PersistedRun(_CheckpointModel):
    run_id: str = Field(min_length=1, max_length=512)
    trace_id: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=512)
    status: Literal[
        "created", "running", "waiting_user", "completed", "failed", "cancelled"
    ]
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
    operation_scope_id: str = Field(min_length=1, max_length=256)
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


class PersistedObservationDetail(_CheckpointModel):
    """One explicitly safety-projected model-visible observation field."""

    name: str = Field(min_length=1, max_length=256)
    value_json: str = Field(min_length=1, max_length=16_000)


class PersistedToolObservation(_CheckpointModel):
    tool_name: str = Field(min_length=1, max_length=256)
    status: Literal["succeeded", "failed", "rejected"]
    summary: str = Field(min_length=1, max_length=2_000)
    outcome: Literal["success", "partial", "empty"] | None = None
    warnings: tuple[str, ...] = Field(default=(), max_length=16)
    is_complete: bool = True
    output_ref: str | None = Field(default=None, max_length=1_024)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=32)
    provider_call_id: str | None = Field(default=None, min_length=1, max_length=256)
    safe_details: tuple[PersistedObservationDetail, ...] = Field(
        default=(), max_length=128
    )
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
    schema_version: Literal[1] = 1
    kind: Literal["approval", "input"]
    prompt: str = Field(min_length=1, max_length=2_000)
    action_ref: str = Field(min_length=1, max_length=256)
    allowed_resume_kinds: tuple[
        Literal["approve", "reject", "provide_input"], ...
    ] = Field(min_length=1, max_length=2)


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
    turn_origin_id: str = Field(min_length=1, max_length=512)
    request: PersistedRequest
    run: PersistedRun
    outputs_by_step: tuple[PersistedStepOutput, ...] = Field(default=(), max_length=128)
    current_step_index: int = Field(default=0, ge=0)
    assistant_output: PersistedAssistantOutput | None = None
    pending_tool_calls: tuple[PersistedToolCallRequest, ...] = Field(
        default=(), max_length=64
    )
    assistant_iterations: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    action_tool_calls_used: int = Field(default=0, ge=0)
    control_tool_calls_used: int = Field(default=0, ge=0)
    run_phase: str = Field(default="assistant", min_length=1, max_length=160)
    tool_observations: tuple[PersistedToolObservation, ...] = Field(
        default=(), max_length=64
    )
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
    turn_origin_id: str
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

    media_refs = [PersistedMediaRef(kind="image", ref=ref) for ref in request.image_ids]
    media_refs.extend(
        PersistedMediaRef(kind="video", ref=ref) for ref in request.video_ids
    )
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
        tool_results=tuple(
            _project_tool_result(result) for result in state.tool_results
        ),
    )
    catalog = _project_catalog(state.run_tool_catalog)
    context_refs = _project_context_refs(state)
    final_response = _project_response(state.response)
    envelope = _AssistantTurnStateModel(
        graph_name=ASSISTANT_GRAPH_NAME,
        graph_version=ASSISTANT_GRAPH_VERSION,
        state_schema_version=ASSISTANT_STATE_SCHEMA_VERSION,
        profile=profile,
        turn_origin_id=state.run_id,
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


def assistant_turn_state_from_loop_state(
    graph_state: Mapping[str, Any],
    *,
    profile: AssistantGraphProfileName = "standard",
) -> AssistantTurnState:
    """Project one legacy node result into the strict checkpoint contract.

    Legacy models may exist while a node is executing, but this function is the
    mandatory boundary before control returns to LangGraph.
    """

    state = graph_state.get("state")
    if not isinstance(state, AgentState):
        raise TypeError("assistant node result requires AgentState")
    projected = dict(assistant_turn_state_from_agent_state(state, profile=profile))
    projected.update(
        {
            "turn_origin_id": _bounded(
                str(graph_state.get("turn_origin_id") or state.run_id), 512
            ),
            "outputs_by_step": [
                _project_step_output(step_id, result).model_dump(mode="json")
                for step_id, result in _tool_result_items(
                    graph_state.get("outputs_by_step")
                )
            ],
            "current_step_index": _non_negative_int(
                graph_state.get("current_step_index")
            ),
            "assistant_output": _project_assistant_output(
                graph_state.get("assistant_output")
            ),
            "pending_tool_calls": [
                _project_tool_call_request(call).model_dump(mode="json")
                for call in _assistant_tool_calls(graph_state.get("pending_tool_calls"))
            ],
            "assistant_iterations": _non_negative_int(
                graph_state.get("assistant_iterations")
            ),
            "tool_calls_used": _non_negative_int(graph_state.get("tool_calls_used")),
            "action_tool_calls_used": _non_negative_int(
                graph_state.get("action_tool_calls_used")
            ),
            "control_tool_calls_used": _non_negative_int(
                graph_state.get("control_tool_calls_used")
            ),
            "run_phase": _run_phase_text(graph_state.get("run_phase")),
            "tool_observations": [
                _project_tool_observation(observation).model_dump(mode="json")
                for observation in _observation_mappings(
                    graph_state.get("tool_observations")
                )
            ],
            "assistant_stream_started": bool(
                graph_state.get("assistant_stream_started", False)
            ),
            "assistant_stream_finished": bool(
                graph_state.get("assistant_stream_finished", False)
            ),
            "tool_stream_started": bool(graph_state.get("tool_stream_started", False)),
            "last_llm_span_id": _bounded_optional(
                graph_state.get("last_llm_span_id"), 256
            ),
            "last_llm_attempt_kind": _bounded_optional(
                graph_state.get("last_llm_attempt_kind"), 160
            ),
            "max_assistant_iterations": _non_negative_int(
                graph_state.get("max_tool_iterations")
            ),
            "max_tool_calls_per_run": _non_negative_int(
                graph_state.get("max_tool_iterations")
            ),
            "max_action_tool_calls_per_run": _non_negative_int(
                graph_state.get("max_tool_iterations")
            ),
            "max_control_tool_calls_per_run": _non_negative_int(
                graph_state.get("max_control_tool_iterations")
            ),
        }
    )
    return validate_assistant_turn_state(projected)


def assistant_loop_state_from_turn_state(
    value: Mapping[str, object],
    *,
    runtime_state: AgentState,
    expected_context_refs: tuple[tuple[object, ...], ...] | None = None,
    expected_capability_refs: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Hydrate a temporary legacy node input from strict checkpoint channels.

    ``runtime_state`` is invocation-local and is never returned to LangGraph.
    It retains rich request/context objects that are prepared by the runtime;
    only the explicitly reconstructed trajectory below crosses node boundaries.
    """

    persisted = validate_assistant_turn_state(value)
    if (
        expected_context_refs is not None
        and _context_ref_identity(persisted["context_refs"]) != expected_context_refs
    ):
        raise AssistantStateCompatibilityError(
            "Runtime context refs do not match the checkpoint turn."
        )
    if (
        expected_capability_refs is not None
        and tuple(persisted["capability_refs"]) != expected_capability_refs
    ):
        raise AssistantStateCompatibilityError(
            "Runtime capability refs do not match the checkpoint turn."
        )
    run = cast(Mapping[str, Any], persisted["run"])
    if (
        runtime_state.run_id != run["run_id"]
        or runtime_state.trace_id != run["trace_id"]
        or runtime_state.agent_id != run["agent_id"]
    ):
        raise AssistantStateCompatibilityError(
            "Runtime state identity does not match the checkpoint turn."
        )
    runtime_request = persisted_request_from_user_request(runtime_state.request)
    runtime_request["capability_refs"] = list(
        assistant_capability_ref_identity(runtime_state)
    )
    if runtime_request != persisted["request"]:
        raise AssistantStateCompatibilityError(
            "Runtime request does not match the checkpoint turn."
        )
    apply_assistant_turn_state_to_agent_state(
        persisted,
        runtime_state=runtime_state,
    )
    assistant_output = _hydrate_assistant_output(persisted.get("assistant_output"))
    pending = [
        _hydrate_tool_call_request(item)
        for item in cast(list[Mapping[str, Any]], persisted["pending_tool_calls"])
    ]
    if pending and not runtime_state.request.metadata.get("native_tool_calls"):
        runtime_state.request.metadata["native_tool_calls"] = [
            {
                "id": call.provider_tool_call_id,
                "name": call.tool_name,
                "arguments": dict(call.tool_input),
                "provider_format": "checkpoint",
                "raw": {},
                "assistant_turn_id": (
                    f"assistant_loop_turn_{int(persisted['assistant_iterations'])}"
                ),
            }
            for call in pending
        ]
    outputs_by_step = {
        str(item["step_id"]): _hydrate_step_output(item)
        for item in cast(list[Mapping[str, Any]], persisted["outputs_by_step"])
    }
    observations = [
        _hydrate_tool_observation(item)
        for item in cast(list[Mapping[str, Any]], persisted["tool_observations"])
    ]
    # Preserve the richer, policy-projected model observation while this
    # invocation still owns the governed ToolResult.  It remains runtime-only;
    # a rebuilt process receives only the strict checkpoint projection above.
    remaining_results = list(runtime_state.tool_results)
    for observation in observations:
        if observation["status"] == "rejected":
            continue
        expected_success = observation["status"] == "succeeded"
        result_index = next(
            (
                index
                for index, result in enumerate(remaining_results)
                if result.tool_name == observation["tool_name"]
                and result.success == expected_success
            ),
            None,
        )
        if result_index is None:
            continue
        rich_observation = remaining_results.pop(result_index).model_observation
        if isinstance(rich_observation, Mapping):
            observation["data"] = dict(rich_observation)
    return {
        "request": runtime_state.request,
        "state": runtime_state,
        "turn_origin_id": str(persisted["turn_origin_id"]),
        "graph_profile": str(persisted["profile"]),
        "outputs_by_step": outputs_by_step,
        "current_step_index": int(persisted["current_step_index"]),
        "trace_id": runtime_state.trace_id,
        "assistant_output": assistant_output,
        "pending_tool_calls": pending,
        "assistant_iterations": int(persisted["assistant_iterations"]),
        "tool_calls_used": int(persisted["tool_calls_used"]),
        "action_tool_calls_used": int(persisted["action_tool_calls_used"]),
        "control_tool_calls_used": int(persisted["control_tool_calls_used"]),
        "run_phase": RunPhase(str(persisted["run_phase"])),
        "tool_observations": observations,
        "last_llm_span_id": str(persisted.get("last_llm_span_id") or ""),
        "last_llm_attempt_kind": str(persisted.get("last_llm_attempt_kind") or ""),
        "max_tool_iterations": int(persisted["max_tool_calls_per_run"]),
        "max_control_tool_iterations": int(persisted["max_control_tool_calls_per_run"]),
        "max_plan_steps": int(persisted["max_assistant_iterations"]),
        "response_stream_current_call_emitted": False,
        "response_stream_ends_with_newline": False,
        "response_stream_separator_pending": False,
    }


def assistant_context_ref_identity(
    state: AgentState,
) -> tuple[tuple[object, ...], ...]:
    """Return deterministic identity facts for the runtime-prepared context."""

    refs = [item.model_dump(mode="json") for item in _project_context_refs(state)]
    return _context_ref_identity(refs)


def assistant_capability_ref_identity(state: AgentState) -> tuple[str, ...]:
    """Return deterministic capability grant identities for one prepared run."""

    return tuple(grant.grant_id for grant in state.capability_grants)


def validate_assistant_runtime_refs(
    persisted: Mapping[str, object],
    runtime_state: AgentState,
) -> None:
    """Fail closed unless freshly prepared refs/catalog match the checkpoint."""

    checkpoint = validate_assistant_turn_state(persisted)
    if _context_ref_identity(
        checkpoint["context_refs"]
    ) != assistant_context_ref_identity(runtime_state):
        raise AssistantStateCompatibilityError(
            "Runtime context refs do not match the checkpoint turn."
        )
    if tuple(checkpoint["capability_refs"]) != assistant_capability_ref_identity(
        runtime_state
    ):
        raise AssistantStateCompatibilityError(
            "Runtime capability refs do not match the checkpoint turn."
        )


def apply_assistant_turn_state_to_agent_state(
    value: Mapping[str, object],
    *,
    runtime_state: AgentState,
) -> AgentState:
    """Apply persisted run facts to a fresh invocation-local product state.

    This is intentionally an explicit field-by-field projection.  Runtime-only
    context already prepared on ``runtime_state`` remains local, while all
    observable run trajectory is reconstructed from the graph result.
    """

    persisted = validate_assistant_turn_state(value)
    run = cast(Mapping[str, Any], persisted["run"])
    if (
        runtime_state.run_id != run["run_id"]
        or runtime_state.trace_id != run["trace_id"]
        or runtime_state.agent_id != run["agent_id"]
    ):
        raise AssistantStateCompatibilityError(
            "Runtime state identity does not match the checkpoint turn."
        )
    runtime_state.status = cast(Any, run["status"])
    runtime_state.errors = [
        AgentError(
            message=str(item["message"]),
            source=cast(str | None, item.get("source")),
            details=({"code": item["code"]} if item.get("code") is not None else {}),
            created_at=_parse_datetime(item.get("created_at")),
        )
        for item in cast(list[Mapping[str, Any]], run["errors"])
    ]
    runtime_state.tool_calls = [
        ToolCallRecord(
            tool_call_id=str(item["tool_call_id"]),
            tool_name=str(item["tool_name"]),
            input={
                str(argument["name"]): json.loads(str(argument["value_json"]))
                for argument in cast(
                    list[Mapping[str, Any]], item.get("arguments") or []
                )
            },
            status=cast(Any, item["status"]),
            started_at=_parse_datetime(item.get("started_at")),
            finished_at=(
                _parse_datetime(item.get("finished_at"))
                if item.get("finished_at") is not None
                else None
            ),
            output_ref=cast(str | None, item.get("output_ref")),
            error_message=cast(str | None, item.get("error_summary")),
        )
        for item in cast(list[Mapping[str, Any]], run["tool_calls"])
    ]
    local_results = list(runtime_state.tool_results)
    runtime_state.tool_results = [
        (
            local_results[index]
            if index < len(local_results)
            and local_results[index].tool_name == item["tool_name"]
            and local_results[index].success == (item["status"] == "succeeded")
            else ToolResult(
                tool_name=str(item["tool_name"]),
                success=item["status"] == "succeeded",
                voice_summary=str(item.get("summary") or ""),
                output_ref=cast(str | None, item.get("output_ref")),
                raw_data_ref=(
                    str(item["artifact_refs"][0]) if item.get("artifact_refs") else None
                ),
                error=cast(str | None, item.get("error_summary")),
                trace_summary=(
                    {"operation_key": str(item["operation_key"])}
                    if item.get("operation_key")
                    else None
                ),
                contract=_hydrate_capability_contract(item.get("capability_contract")),
            )
        )
        for index, item in enumerate(cast(list[Mapping[str, Any]], run["tool_results"]))
    ]
    catalog = cast(Mapping[str, Any], persisted["catalog"])
    excluded: dict[str, list[str]] = {}
    for encoded in cast(list[str], catalog["exclusion_reason_codes"]):
        tool_name, separator, reason = encoded.partition(":")
        if separator:
            excluded.setdefault(tool_name, []).append(reason)
    runtime_state.run_tool_catalog = RunToolCatalog(
        schema_version="run_tool_catalog_v1",
        available_tool_names=list(catalog["available_tool_names"]),
        selection_reasons=list(catalog["selection_reason_codes"]),
        excluded_reasons=excluded,
    )
    response = persisted.get("final_response")
    local_response = runtime_state.response
    reconstructed_response = (
        AgentResponse(
            message=str(response["message"]),
            followup_question=cast(str | None, response.get("followup_question")),
            output_refs=list(response.get("output_refs") or []),
            annotations=[
                UrlCitationAnnotation(
                    source_id=str(item["source_id"]),
                    title=str(item["title"]),
                    url=str(item["url"]),
                    start_index=int(item["start_index"]),
                    end_index=int(item["end_index"]),
                )
                for item in cast(
                    list[Mapping[str, Any]], response.get("citations") or []
                )
            ],
        )
        if isinstance(response, Mapping)
        else None
    )
    runtime_state.response = (
        local_response
        if local_response is not None
        and reconstructed_response is not None
        and local_response.message == reconstructed_response.message
        and local_response.output_refs == reconstructed_response.output_refs
        else reconstructed_response
    )
    return runtime_state


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


def route_after_assistant_turn_state(value: Mapping[str, object]) -> str:
    """Route using only checkpoint-safe facts, without hydrating runtime models."""

    state = validate_assistant_turn_state(value)
    run = cast(Mapping[str, Any], state["run"])
    if state.get("pending_interrupt") is not None:
        return "await_input"
    if run["status"] in {"failed", "completed", "cancelled"}:
        return "finish"
    output = state.get("assistant_output")
    if isinstance(output, Mapping) and output.get("kind") == "tool_calls":
        return "execute_tool"
    return "finish"


def route_after_await_input_turn_state(value: Mapping[str, object]) -> str:
    """Continue from a resolved input gate without consulting request text."""

    state = validate_assistant_turn_state(value)
    if state.get("pending_interrupt") is not None:
        raise ValueError("assistant_interrupt_not_resolved")
    if state.get("pending_tool_calls"):
        return "execute_tool"
    return "assistant"


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
                messages.append(
                    PersistedMessage(role=role, text=_bounded(text, 32_000))
                )
    if request.text is not None:
        messages.append(PersistedMessage(role="user", text=request.text))
    return tuple(messages[-128:])


def _project_assistant_output(value: object) -> dict[str, object] | None:
    if isinstance(value, AssistantTextOutput):
        return cast(
            dict[str, object],
            PersistedAssistantOutput(
                kind="text",
                text=_bounded(value.text, 32_000),
            ).model_dump(mode="json"),
        )
    if isinstance(value, AssistantToolCall):
        return cast(
            dict[str, object],
            PersistedAssistantOutput(
                kind="tool_calls",
                tool_calls=(_project_tool_call_request(value),),
            ).model_dump(mode="json"),
        )
    if value is None:
        return None
    raise TypeError("assistant_output must use the strict assistant output contract")


def _project_tool_call_request(call: AssistantToolCall) -> PersistedToolCallRequest:
    arguments = tuple(
        PersistedToolArgument(
            name=name,
            value_json=_checkpoint_argument_json(
                name,
                value,
                tool_name=call.tool_name,
            ),
        )
        for name, value in call.tool_input.items()
    )
    return PersistedToolCallRequest(
        provider_call_id=(
            call.provider_tool_call_id
            or f"local:{call.operation_scope_id or call.tool_name}"
        ),
        operation_scope_id=(
            call.operation_scope_id
            or _missing_operation_scope_id(call.tool_name, arguments)
        ),
        tool_name=call.tool_name,
        arguments=arguments,
    )


def _missing_operation_scope_id(
    tool_name: str, arguments: tuple[PersistedToolArgument, ...]
) -> str:
    """Bound legacy in-process callers without claiming graph resume identity."""

    encoded = json.dumps(
        [
            "legacy-unscoped",
            tool_name,
            [item.model_dump(mode="json") for item in arguments],
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"legacy:{hashlib.sha256(encoded).hexdigest()}"


def _hydrate_assistant_output(
    value: object,
) -> AssistantTextOutput | AssistantToolCall | None:
    if value is None:
        return None
    item = cast(Mapping[str, Any], value)
    kind = item.get("kind")
    if kind == "text":
        return AssistantTextOutput(text=str(item.get("text") or ""))
    if kind == "tool_calls":
        calls = cast(list[Mapping[str, Any]], item.get("tool_calls") or [])
        return _hydrate_tool_call_request(calls[0]) if calls else None
    return None


def _hydrate_tool_call_request(item: Mapping[str, Any]) -> AssistantToolCall:
    return AssistantToolCall(
        tool_name=str(item["tool_name"]),
        tool_input={
            str(argument["name"]): json.loads(str(argument["value_json"]))
            for argument in cast(list[Mapping[str, Any]], item.get("arguments") or [])
        },
        provider_tool_call_id=str(item["provider_call_id"]),
        operation_scope_id=str(item["operation_scope_id"]),
        safety_notes=["checkpoint_hydrated"],
    )


def _assistant_tool_calls(value: object) -> tuple[AssistantToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TypeError("pending_tool_calls must be a sequence")
    calls = tuple(value)
    if not all(isinstance(item, AssistantToolCall) for item in calls):
        raise TypeError("pending_tool_calls must contain AssistantToolCall")
    return cast(tuple[AssistantToolCall, ...], calls)


def _project_tool_observation(value: Mapping[str, Any]) -> PersistedToolObservation:
    raw_error = value.get("error")
    error = None
    if isinstance(raw_error, Mapping):
        error = PersistedObservationError(
            code=str(raw_error.get("code") or "tool_failed"),
            message=_bounded(str(raw_error.get("message") or "Tool failed."), 2_000),
            retryable=bool(raw_error.get("retryable", False)),
        )
    warnings = tuple(
        _bounded(item, 2_000)
        for item in cast(list[object], value.get("warnings") or [])
        if isinstance(item, str) and item
    )
    status = value.get("status")
    if status not in {"succeeded", "failed", "rejected"}:
        raise ValueError("assistant_state_tool_observation_status_invalid")
    outcome = value.get("outcome")
    if outcome not in {"success", "partial", "empty", None}:
        outcome = None
    raw_data = value.get("data")
    safe_details: list[PersistedObservationDetail] = []
    if isinstance(raw_data, Mapping):
        safe_data = sanitize_tool_observation_detail(raw_data)
        if not isinstance(safe_data, Mapping):
            safe_data = {}
        safe_details.extend(
            _project_scalar_facts(
                safe_data,
                allowed_names=_OBSERVATION_SCALAR_FACT_NAMES,
                limit=128,
            )
        )
    return PersistedToolObservation(
        tool_name=str(value.get("tool_name") or "unknown"),
        status=cast(Any, status),
        summary=_bounded(str(value.get("summary") or "Tool observation."), 2_000),
        outcome=cast(Any, outcome),
        warnings=warnings[:16],
        is_complete=bool(value.get("is_complete", status == "succeeded")),
        output_ref=_bounded_optional(value.get("output_ref"), 1_024),
        artifact_refs=(),
        provider_call_id=_bounded_optional(value.get("_provider_tool_call_id"), 256),
        safe_details=tuple(safe_details[:128]),
        error=error,
    )


def _hydrate_tool_observation(item: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_name": item["tool_name"],
        "status": item["status"],
        "summary": item["summary"],
        "outcome": item.get("outcome"),
        "warnings": list(item.get("warnings") or []),
        "is_complete": bool(item.get("is_complete", False)),
        "output_ref": item.get("output_ref"),
        "data": {
            str(fact["name"]): json.loads(str(fact["value_json"]))
            for fact in cast(list[Mapping[str, Any]], item.get("safe_details") or [])
        },
    }
    if item.get("error") is not None:
        payload["error"] = dict(cast(Mapping[str, Any], item["error"]))
    if item.get("provider_call_id") is not None:
        payload["_provider_tool_call_id"] = item["provider_call_id"]
    return payload


def _observation_mappings(value: object) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TypeError("tool_observations must be a sequence")
    observations = tuple(value)
    if not all(isinstance(item, Mapping) for item in observations):
        raise TypeError("tool_observations must contain mappings")
    return cast(tuple[Mapping[str, Any], ...], observations)


def _tool_result_items(value: object) -> tuple[tuple[str, ToolResult], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise TypeError("outputs_by_step must be a mapping")
    items: list[tuple[str, ToolResult]] = []
    for step_id, result in value.items():
        if not isinstance(step_id, str) or not isinstance(result, ToolResult):
            raise TypeError("outputs_by_step must map strings to ToolResult")
        items.append((step_id, result))
    return tuple(items)


def _project_step_output(step_id: str, result: ToolResult) -> PersistedStepOutput:
    projected = _project_tool_result(result)
    return PersistedStepOutput(
        step_id=step_id,
        tool_name=result.tool_name,
        status="succeeded" if result.success else "failed",
        summary=projected.summary,
        output_ref=projected.output_ref,
        artifact_refs=projected.artifact_refs,
    )


def _hydrate_step_output(item: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        tool_name=str(item["tool_name"]),
        success=item["status"] == "succeeded",
        voice_summary=str(item.get("summary") or ""),
        output_ref=cast(str | None, item.get("output_ref")),
        raw_data_ref=(
            str(item["artifact_refs"][0]) if item.get("artifact_refs") else None
        ),
        error=(
            str(item.get("summary") or "Tool failed.")
            if item["status"] != "succeeded"
            else None
        ),
    )


def _run_phase_text(value: object) -> str:
    if isinstance(value, RunPhase):
        return value.value
    if isinstance(value, str) and value:
        return value
    return RunPhase.ACT.value


def _context_ref_identity(
    refs: object,
) -> tuple[tuple[object, ...], ...]:
    if not isinstance(refs, (list, tuple)):
        raise AssistantStateCompatibilityError("Checkpoint context refs are invalid.")
    return tuple(
        (
            item.get("kind"),
            item.get("ref"),
            item.get("source"),
            item.get("version"),
            item.get("status_code"),
        )
        for item in refs
        if isinstance(item, Mapping)
    )


def _non_negative_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _project_tool_call(record: object) -> PersistedToolCall:
    raw_input = getattr(record, "input", {})
    arguments: list[PersistedToolArgument] = []
    if isinstance(raw_input, Mapping):
        for name, value in raw_input.items():
            if not isinstance(name, str):
                raise ValueError("assistant_state_tool_argument_name_invalid")
            encoded = _checkpoint_argument_json(
                name,
                value,
                tool_name=record.tool_name,
            )
            arguments.append(PersistedToolArgument(name=name, value_json=encoded))
    return PersistedToolCall(
        tool_call_id=record.tool_call_id,
        tool_name=record.tool_name,
        arguments=tuple(arguments),
        status=record.status,
        started_at=_datetime_text(record.started_at) or "invalid",
        finished_at=_datetime_text(record.finished_at),
        output_ref=_safe_checkpoint_ref(record.output_ref),
        error_summary=_bounded_optional(record.error_message, 2_000),
    )


def _project_tool_result(result: object) -> PersistedToolResult:
    error = getattr(result, "error", None)
    voice_summary = getattr(result, "voice_summary", None)
    summary = voice_summary if isinstance(voice_summary, str) else error or ""
    contract = getattr(result, "contract", None)
    trace_summary = getattr(result, "trace_summary", None)
    operation_key = (
        trace_summary.get("operation_key")
        if isinstance(trace_summary, Mapping)
        and isinstance(trace_summary.get("operation_key"), str)
        else None
    )
    return PersistedToolResult(
        tool_name=result.tool_name,
        status="succeeded" if result.success else "failed",
        summary=_bounded(str(summary), 2_000),
        operation_key=_bounded_optional(operation_key, 512),
        output_ref=_safe_checkpoint_ref(result.output_ref),
        artifact_refs=tuple(
            safe_ref
            for ref in (getattr(result, "raw_data_ref", None),)
            if (safe_ref := _safe_checkpoint_ref(ref)) is not None
        ),
        error_summary=_bounded_optional(error, 2_000),
        capability_contract=_project_capability_contract(contract),
    )


_OBSERVATION_SCALAR_FACT_NAMES = frozenset(
    {
        "count",
        "outcome",
        "provider",
        "query_used",
        "status",
        "total",
        "value",
    }
)
_CAPABILITY_DATA_SCALAR_FACT_NAMES = frozenset(
    {
        "best_price",
        "count",
        "outcome",
        "product_title",
        "query_used",
        "render_ref",
        "summary",
        "total",
        "value",
    }
)
_CAPABILITY_METADATA_SCALAR_FACT_NAMES = frozenset(
    {"latency_ms", "model", "provider", "query_count"}
)


def _project_capability_contract(
    contract: object | None,
) -> PersistedCapabilityContract | None:
    if not isinstance(contract, CapabilityOutputContract):
        return None
    return PersistedCapabilityContract(
        capability=_bounded(contract.capability, 256),
        status=contract.status,
        output_ref=_safe_checkpoint_ref(contract.output_ref),
        data_facts=_project_scalar_facts(
            contract.data,
            allowed_names=_CAPABILITY_DATA_SCALAR_FACT_NAMES,
            limit=64,
        ),
        metadata_facts=_project_scalar_facts(
            contract.metadata,
            allowed_names=_CAPABILITY_METADATA_SCALAR_FACT_NAMES,
            limit=32,
        ),
        errors=tuple(
            PersistedCapabilityError(
                code=_bounded(error.code, 160),
                message=_bounded(error.message, 2_000),
                recoverable=error.recoverable,
            )
            for error in contract.errors[:32]
        ),
    )


def _hydrate_capability_contract(
    value: object,
) -> CapabilityOutputContract | None:
    if not isinstance(value, Mapping):
        return None
    return CapabilityOutputContract(
        capability=str(value["capability"]),
        status=cast(Any, value["status"]),
        output_ref=cast(str | None, value.get("output_ref")),
        data=_hydrate_scalar_facts(value.get("data_facts")),
        metadata=_hydrate_scalar_facts(value.get("metadata_facts")),
        errors=[
            CapabilityOutputError(
                code=str(item["code"]),
                message=str(item["message"]),
                recoverable=bool(item.get("recoverable", False)),
            )
            for item in cast(list[Mapping[str, Any]], value.get("errors") or [])
        ],
    )


def _project_scalar_facts(
    value: object,
    *,
    allowed_names: frozenset[str],
    limit: int,
) -> tuple[PersistedObservationDetail, ...]:
    if not isinstance(value, Mapping):
        return ()
    facts: list[PersistedObservationDetail] = []
    for name, child in value.items():
        if name not in allowed_names or not _is_safe_scalar(child):
            continue
        if isinstance(child, str):
            if name.endswith("_ref"):
                child = _safe_checkpoint_ref(child)
            elif not _checkpoint_string_is_safe(child):
                child = None
            if child is None:
                continue
        encoded = json.dumps(child, ensure_ascii=False, allow_nan=False)
        if len(encoded) <= 4_000:
            facts.append(PersistedObservationDetail(name=name, value_json=encoded))
    return tuple(facts[:limit])


def _hydrate_scalar_facts(value: object) -> dict[str, Any]:
    if not isinstance(value, (list, tuple)):
        return {}
    return {
        str(item["name"]): json.loads(str(item["value_json"]))
        for item in value
        if isinstance(item, Mapping)
    }


def _is_safe_scalar(value: object) -> bool:
    return value is None or (
        isinstance(value, (str, bool, int, float)) and not isinstance(value, bytes)
    )


_CHECKPOINT_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")
_CHECKPOINT_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z][A-Za-z0-9_-]{0,127})\s*[:=]\s*\S+"
)
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "authorization",
        "accesstoken",
        "token",
        "cookie",
        "clientsecret",
        "session",
        "sessionid",
        "sessionkey",
        "sessiontoken",
        "signature",
        "sig",
        "apikey",
        "password",
        "secret",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
        "xgoogcredential",
        "xgoogsignature",
        "ossaccesskeyid",
        "securitytoken",
    }
)
_SIGNED_QUERY_KEY_NAMES = _SENSITIVE_KEY_NAMES | frozenset({"se", "sp", "sv"})
_CHECKPOINT_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_RELATIVE_MEDIA_PATH_RE = re.compile(
    r"^(?:(?:\.\.?|assets?|media|uploads?|images?|videos?|audio)/).+"
    r"\.(?:png|jpe?g|gif|webp|svg|mp[34]|mov|avi|wav|m4a|ogg|pdf|zip)$",
    re.IGNORECASE,
)


def _normalized_checkpoint_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _checkpoint_key_parts(value: str) -> tuple[str, ...]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return tuple(part for part in re.split(r"[^A-Za-z0-9]+", separated.lower()) if part)


def _checkpoint_key_is_sensitive(value: str) -> bool:
    normalized = _normalized_checkpoint_key(value)
    if normalized in {"tokencount", "tokenbudget", "accessibility"}:
        return False
    if normalized in _SENSITIVE_KEY_NAMES:
        return True
    parts = _checkpoint_key_parts(value)
    if any(
        part
        in {
            "authorization",
            "auth",
            "cookie",
            "credential",
            "password",
            "secret",
            "session",
            "signature",
            "token",
        }
        for part in parts
    ):
        return True
    return any(
        parts[index : index + 2] in {("private", "key"), ("access", "key")}
        for index in range(max(0, len(parts) - 1))
    )


def _is_schema_bound_opaque_ref(
    key: str,
    value: object,
    *,
    tool_name: str | None,
) -> bool:
    """Allow only explicit Tool-schema references that are not bearer authority.

    Website Guidance binds this identifier to the request owner again inside
    its governed backend.  Cookie/token/key variants remain rejected by the
    semantic classifier above.
    """

    return (
        tool_name == "web_page_explore"
        and key == "browser_session_id"
        and isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) is not None
    )


def _url_contains_credentials_or_signature(value: str) -> bool:
    parsed = urlparse(value.rstrip(".,);]"))
    if parsed.username is not None or parsed.password is not None:
        return True
    query_keys = [key for key, _ in parse_qsl(parsed.query)]
    return any(
        _normalized_checkpoint_key(key) in _SIGNED_QUERY_KEY_NAMES
        or _checkpoint_key_is_sensitive(key)
        for key in query_keys
    )


def _checkpoint_string_is_safe(value: str, *, allow_empty: bool = False) -> bool:
    text = value.strip()
    if (not text and not allow_empty) or len(text) > 16_000:
        return False
    lowered = text.lower()
    if _CHECKPOINT_BEARER_RE.search(text) or any(
        _checkpoint_key_is_sensitive(match.group(1))
        for match in _CHECKPOINT_ASSIGNMENT_RE.finditer(text)
    ):
        return False
    if lowered.startswith(("data:", "file:")) or re.search(
        r"(?i)(?:;|\b)\s*base64\s*[, :]", text
    ):
        return False
    if lowered.startswith(("/home/", "/users/", "/tmp/", "/var/", "/mnt/", "/media/")):
        return False
    if _WINDOWS_PATH_RE.match(text) or _RELATIVE_MEDIA_PATH_RE.match(text):
        return False
    if "artifact body" in lowered or "media body" in lowered:
        return False
    urls = _CHECKPOINT_URL_RE.findall(text)
    if any(_url_contains_credentials_or_signature(url) for url in urls):
        return False
    return True


def _checkpoint_json_is_safe(value: object, *, depth: int = 0) -> bool:
    """Validate bounded JSON for checkpoint-sensitive execution facts."""

    if depth > 8:
        return False
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return _checkpoint_string_is_safe(value, allow_empty=True)
    if isinstance(value, Mapping):
        if len(value) > 128:
            return False
        for key, child in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 256
                or _checkpoint_key_is_sensitive(key)
                or not _checkpoint_json_is_safe(child, depth=depth + 1)
            ):
                return False
        return True
    if isinstance(value, (list, tuple)):
        return len(value) <= 128 and all(
            _checkpoint_json_is_safe(child, depth=depth + 1) for child in value
        )
    return False


def _checkpoint_argument_json(
    name: object,
    value: object,
    *,
    tool_name: str | None = None,
) -> str:
    value = _normalize_checkpoint_json_value(value)
    if (
        not isinstance(name, str)
        or (
            _checkpoint_key_is_sensitive(name)
            and not _is_schema_bound_opaque_ref(
                name,
                value,
                tool_name=tool_name,
            )
        )
        or not _checkpoint_json_is_safe(value)
    ):
        raise ValueError("assistant_state_checkpoint_value_unsafe")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("assistant_state_checkpoint_value_unsafe") from exc
    if len(encoded) > 16_000:
        raise ValueError("assistant_state_checkpoint_value_unsafe")
    return encoded


def _normalize_checkpoint_json_value(value: object) -> object:
    """Normalize explicitly supported Pydantic JSON scalar wrappers only."""

    if isinstance(value, AnyUrl):
        return str(value)
    if isinstance(value, Mapping):
        return {
            key: _normalize_checkpoint_json_value(child) for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_checkpoint_json_value(child) for child in value]
    return value


def _safe_checkpoint_ref(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _checkpoint_string_is_safe(text):
        return None
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        return text if parsed.netloc else None
    if parsed.scheme and parsed.scheme not in {"artifact", "memory", "media", "output"}:
        return None
    if parsed.scheme:
        return text if parsed.netloc or parsed.path else None
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,1023}", text) else None


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
        refs.append(
            PersistedContextRef(
                kind="context_section",
                ref=section.source_ref or section.section_id,
                source=section.source_type,
                version=section.source_version or None,
            )
        )
    for issue in state.context_source_result.issues:
        refs.append(
            PersistedContextRef(
                kind="source_issue",
                ref=issue.source_ref or issue.section_id or issue.code,
                status_code=issue.code,
            )
        )
    snapshot = state.session_memory_snapshot
    if snapshot is not None:
        refs.extend(
            PersistedContextRef(kind="memory", ref=item.memory_id, source=item.source)
            for item in snapshot.memories
        )
    visual = getattr(state.perception, "visual", None)
    if visual is not None and visual.output_ref:
        refs.append(
            PersistedContextRef(
                kind="perception",
                ref=visual.output_ref,
                source=visual.media_kind,
            )
        )
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


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return parsed
    return datetime.now(timezone.utc)


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
    "apply_assistant_turn_state_to_agent_state",
    "assistant_capability_ref_identity",
    "assistant_context_ref_identity",
    "assistant_loop_state_from_turn_state",
    "assistant_turn_state_from_agent_state",
    "assistant_turn_state_from_loop_state",
    "assistant_turn_state_from_request",
    "persisted_request_from_user_request",
    "route_after_await_input_turn_state",
    "route_after_assistant_turn_state",
    "validate_assistant_turn_state",
    "validate_assistant_runtime_refs",
]
