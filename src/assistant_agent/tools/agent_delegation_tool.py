"""Opt-in agent delegation tool backed by AgentCommunicationService."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentMessage,
    AgentSessionRef,
    AgentTaskResult,
)
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.agent_communication import AgentCommunicationService
from assistant_agent.services.provider_errors import sanitize_error_detail
from assistant_agent.tools.base import ToolBase, ToolContext


class DelegateToAgentInput(BaseModel):
    """Input for delegating one task to another enabled local agent."""

    model_config = ConfigDict(extra="ignore")

    target_agent_id: str = Field(min_length=1)
    text: str | None = None
    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    audio_id: str | None = None
    context_refs: list[str] = Field(default_factory=list)
    timeout_ms: int = Field(default=30_000, ge=1, le=300_000)
    max_delegation_depth: int = Field(default=1, ge=0, le=5)
    token_budget: int | None = Field(default=None, ge=0)
    tool_budget: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_delegation_payload(self) -> "DelegateToAgentInput":
        if self.text or self.image_ids or self.video_ids or self.audio_id:
            return self
        raise ValueError(
            "delegate_to_agent requires text, image_ids, video_ids, or audio_id"
        )


class DelegateToAgentOutput(BaseModel):
    """Structured output from a delegated agent task."""

    task_id: str
    target_agent_id: str
    status: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    run_id: str | None = None
    trace_id: str | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentDelegationTool(ToolBase):
    """Delegate one request to another agent through the communication service."""

    name = "delegate_to_agent"
    description = "Delegate a bounded task to another enabled local agent instance."
    input_schema = DelegateToAgentInput
    output_schema = DelegateToAgentOutput
    category = "generate"
    requires_confirmation = False

    def __init__(self, service: AgentCommunicationService | None = None) -> None:
        self.service = service

    def _run(self, input: DelegateToAgentInput, context: ToolContext) -> ToolResult:
        service = self.service or _service_from_context(context)
        if service is None:
            return _failed_tool_result(
                "agent_delegation_service_unavailable",
                "Agent delegation service is not configured.",
                detail={"target_agent_id": input.target_agent_id},
                recoverable=True,
            )
        if not context.user_id or not context.session_id:
            return _failed_tool_result(
                "agent_delegation_identity_missing",
                "Agent delegation requires user_id and session_id.",
                detail={"target_agent_id": input.target_agent_id},
                recoverable=True,
            )
        source_agent_id = _source_agent_id(context)
        if input.target_agent_id == source_agent_id:
            return _failed_tool_result(
                "agent_self_delegation_blocked",
                "Agent delegation target must differ from the source agent.",
                detail={
                    "source_agent_id": source_agent_id,
                    "target_agent_id": input.target_agent_id,
                },
                recoverable=True,
            )
        if not _has_message_payload(input):
            return _failed_tool_result(
                "agent_delegation_input_empty",
                "Agent delegation requires text, image_ids, video_ids, or audio_id.",
                detail={"target_agent_id": input.target_agent_id},
                recoverable=True,
            )

        result = service.send_message(
            target_agent_id=input.target_agent_id,
            source_agent_id=source_agent_id,
            session=AgentSessionRef(
                user_id=context.user_id,
                session_id=context.session_id,
                parent_run_id=context.run_id,
                parent_trace_id=_context_trace_id(context),
                correlation_id=_context_correlation_id(context),
            ),
            message=AgentMessage(
                text=input.text,
                image_ids=list(input.image_ids),
                video_ids=list(input.video_ids),
                audio_id=input.audio_id,
                metadata=_message_metadata(input),
            ),
            timeout_ms=input.timeout_ms,
            delegation_depth=_next_delegation_depth(context),
            max_delegation_depth=input.max_delegation_depth,
            token_budget=input.token_budget,
            tool_budget=input.tool_budget,
            metadata=_task_metadata(context, input, self.name),
        )
        return _tool_result_from_task(result)


def _service_from_context(context: ToolContext) -> AgentCommunicationService | None:
    service = context.metadata.get("agent_communication_service")
    return service if isinstance(service, AgentCommunicationService) else None


def _source_agent_id(context: ToolContext) -> str:
    value = context.metadata.get("agent_id")
    return str(value) if isinstance(value, str) and value else DEFAULT_AGENT_ID


def _context_trace_id(context: ToolContext) -> str | None:
    value = context.metadata.get("trace_id")
    return str(value) if isinstance(value, str) and value else None


def _context_correlation_id(context: ToolContext) -> str:
    metadata = context.metadata.get("agent_communication")
    if isinstance(metadata, dict):
        value = metadata.get("correlation_id")
        if isinstance(value, str) and value:
            return value
    return AgentSessionRef(
        user_id=context.user_id or "unknown", session_id=context.session_id or "unknown"
    ).correlation_id


def _next_delegation_depth(context: ToolContext) -> int:
    metadata = context.metadata.get("agent_communication")
    if isinstance(metadata, dict):
        value = metadata.get("delegation_depth")
        if isinstance(value, int):
            return value + 1
    return 1


def _message_metadata(input: DelegateToAgentInput) -> dict[str, Any]:
    metadata = dict(input.metadata)
    if input.context_refs:
        metadata["context_refs"] = list(input.context_refs)
    return metadata


def _task_metadata(
    context: ToolContext, input: DelegateToAgentInput, tool_name: str
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"tool": tool_name}
    pairs = _delegation_pairs_from_context(context)
    if pairs:
        metadata["delegation_pairs"] = pairs
    if input.context_refs:
        metadata["context_refs"] = list(input.context_refs)
    return metadata


def _delegation_pairs_from_context(context: ToolContext) -> list[list[str]]:
    metadata = context.metadata.get("agent_communication")
    if not isinstance(metadata, dict):
        return []
    raw_pairs = metadata.get("delegation_pairs")
    if not isinstance(raw_pairs, list):
        return []
    pairs: list[list[str]] = []
    for pair in raw_pairs:
        if (
            isinstance(pair, list)
            and len(pair) == 2
            and isinstance(pair[0], str)
            and isinstance(pair[1], str)
        ):
            pairs.append([pair[0], pair[1]])
    return pairs


def _has_message_payload(input: DelegateToAgentInput) -> bool:
    return bool(input.text or input.image_ids or input.video_ids or input.audio_id)


def _tool_result_from_task(result: AgentTaskResult) -> ToolResult:
    data = _task_result_payload(result)
    output_ref = f"local://agent-task/{result.task_id}"
    errors = [error.model_dump(mode="json") for error in result.errors]
    error_message = (
        result.errors[0].message
        if result.errors
        else ("Agent task cancelled." if result.status == "cancelled" else None)
    )
    contract = build_capability_output_contract(
        capability="agent_delegation",
        status="succeeded" if result.status == "completed" else "failed",
        output_ref=output_ref,
        data=data,
        errors=errors,
        metadata={
            "target_agent_id": result.target_agent_id,
            "transport": result.metadata.get("transport", "local"),
        },
    )
    return ToolResult(
        tool_name=AgentDelegationTool.name,
        success=result.status == "completed",
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_agent_delegation_model_observation(data),
        error=error_message,
        output_ref=output_ref,
        contract=contract,
    )


def _failed_tool_result(
    code: str,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
    recoverable: bool = False,
) -> ToolResult:
    error = {
        "code": code,
        "message": message,
        "detail": detail or {},
        "recoverable": recoverable,
    }
    data = {"status": "failed", "errors": [sanitize_error_detail(error)]}
    contract = build_capability_output_contract(
        capability="agent_delegation",
        status="failed",
        data=data,
        errors=[error],
    )
    return ToolResult(
        tool_name=AgentDelegationTool.name,
        success=False,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_agent_delegation_model_observation(data),
        error=message,
        contract=contract,
    )


def _task_result_payload(result: AgentTaskResult) -> dict[str, Any]:
    return sanitize_error_detail(
        {
            "task_id": result.task_id,
            "target_agent_id": result.target_agent_id,
            "status": result.status,
            "artifacts": [
                artifact.model_dump(mode="json") for artifact in result.artifacts
            ],
            "run_id": result.run_id,
            "trace_id": result.trace_id,
            "errors": [error.model_dump(mode="json") for error in result.errors],
            "metadata": result.metadata,
        }
    )


def _agent_delegation_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "task_id": data.get("task_id"),
        "target_agent_id": data.get("target_agent_id"),
        "status": data.get("status"),
        "artifacts": data.get("artifacts"),
        "errors": data.get("errors"),
    }
    if data.get("status") == "failed" and not observation.get("errors"):
        observation["errors"] = [
            {"code": "agent_delegation_failed", "message": "Agent delegation failed."}
        ]
    return {
        key: value
        for key, value in observation.items()
        if value not in (None, "", [], {})
    }
