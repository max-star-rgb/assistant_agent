"""Safety policy for local agent-to-agent delegation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from multimodal_agent.schemas.agent_communication import (
    AgentCommunicationError,
    AgentDelegationAuditEvent,
    AgentDelegationAuditEventType,
    AgentTask,
)
from multimodal_agent.services.agent_directory import AgentDirectory
from multimodal_agent.services.provider_errors import sanitize_error_detail


class DelegationPolicyResult(BaseModel):
    """Result of validating one delegation task before transport dispatch."""

    accepted: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    audit_event: AgentDelegationAuditEvent
    error: AgentCommunicationError | None = None


class DelegationDepthPolicy:
    """Bound recursive agent delegation depth."""

    def validate(self, task: AgentTask) -> AgentCommunicationError | None:
        if task.delegation_depth > task.max_delegation_depth:
            return AgentCommunicationError(
                code="agent_delegation_depth_exceeded",
                message="Agent delegation depth exceeded.",
                detail={
                    "delegation_depth": task.delegation_depth,
                    "max_delegation_depth": task.max_delegation_depth,
                },
                recoverable=False,
            )
        return None


class TimeoutPolicy:
    """Bound task-level timeout requests before dispatch."""

    def __init__(self, *, max_timeout_ms: int = 300_000) -> None:
        self.max_timeout_ms = max_timeout_ms

    def validate(self, task: AgentTask) -> AgentCommunicationError | None:
        if task.timeout_ms > self.max_timeout_ms:
            return AgentCommunicationError(
                code="agent_delegation_timeout_too_large",
                message="Agent delegation timeout exceeds policy limit.",
                detail={"timeout_ms": task.timeout_ms, "max_timeout_ms": self.max_timeout_ms},
                recoverable=True,
            )
        return None


class DelegationInputValidator:
    """Validate source, target, message, and directory permission rules."""

    def validate(self, task: AgentTask, *, directory: AgentDirectory) -> AgentCommunicationError | None:
        if task.source_agent_id == task.target_agent_id:
            return AgentCommunicationError(
                code="agent_self_delegation_blocked",
                message="Agent delegation target must differ from the source agent.",
                detail={"source_agent_id": task.source_agent_id, "target_agent_id": task.target_agent_id},
                recoverable=True,
            )
        if not _has_message_payload(task):
            return AgentCommunicationError(
                code="agent_delegation_input_empty",
                message="Agent delegation requires text, image_ids, video_ids, or audio_id.",
                detail={"target_agent_id": task.target_agent_id},
                recoverable=True,
            )
        source = directory.get(task.source_agent_id)
        if source is None:
            return None
        if not source.can_delegate:
            return AgentCommunicationError(
                code="agent_delegation_not_allowed",
                message=f"Agent is not allowed to delegate: {task.source_agent_id}",
                detail={"source_agent_id": task.source_agent_id, "target_agent_id": task.target_agent_id},
                recoverable=True,
            )
        if source.allowed_targets and task.target_agent_id not in set(source.allowed_targets):
            return AgentCommunicationError(
                code="agent_delegation_target_not_allowed",
                message=f"Agent is not allowed to delegate to target: {task.target_agent_id}",
                detail={
                    "source_agent_id": task.source_agent_id,
                    "target_agent_id": task.target_agent_id,
                    "allowed_targets": list(source.allowed_targets),
                },
                recoverable=True,
            )
        return None


class PingPongLoopDetector:
    """Detect repeated or immediately reversed delegation pairs."""

    def validate(self, task: AgentTask) -> AgentCommunicationError | None:
        previous_pairs = _delegation_pairs(task.metadata)
        current_pair = [task.source_agent_id, task.target_agent_id]
        reverse_pair = [task.target_agent_id, task.source_agent_id]
        if current_pair in previous_pairs:
            return AgentCommunicationError(
                code="agent_delegation_loop_detected",
                message="Repeated agent delegation pair blocked.",
                detail={"source_agent_id": task.source_agent_id, "target_agent_id": task.target_agent_id},
                recoverable=False,
            )
        if reverse_pair in previous_pairs:
            return AgentCommunicationError(
                code="agent_delegation_ping_pong_blocked",
                message="Ping-pong agent delegation blocked.",
                detail={"source_agent_id": task.source_agent_id, "target_agent_id": task.target_agent_id},
                recoverable=False,
            )
        return None


class ParentChildRunBudget:
    """Normalize optional child run budget metadata."""

    def metadata(self, task: AgentTask) -> dict[str, Any]:
        return {
            "token_budget": task.token_budget,
            "tool_budget": task.tool_budget,
        }


class DelegationRedactor:
    """Sanitize metadata that crosses an agent boundary."""

    def sanitize_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return sanitize_error_detail(metadata)


class AgentDelegationPolicy:
    """Validate local agent delegation before transport dispatch."""

    def __init__(
        self,
        *,
        depth_policy: DelegationDepthPolicy | None = None,
        timeout_policy: TimeoutPolicy | None = None,
        input_validator: DelegationInputValidator | None = None,
        loop_detector: PingPongLoopDetector | None = None,
        budget: ParentChildRunBudget | None = None,
        redactor: DelegationRedactor | None = None,
    ) -> None:
        self.depth_policy = depth_policy or DelegationDepthPolicy()
        self.timeout_policy = timeout_policy or TimeoutPolicy()
        self.input_validator = input_validator or DelegationInputValidator()
        self.loop_detector = loop_detector or PingPongLoopDetector()
        self.budget = budget or ParentChildRunBudget()
        self.redactor = redactor or DelegationRedactor()

    def validate(self, task: AgentTask, *, directory: AgentDirectory) -> DelegationPolicyResult:
        metadata = self._metadata_for_dispatch(task)
        for check in (
            self.depth_policy.validate(task),
            self.timeout_policy.validate(task),
            self.input_validator.validate(task, directory=directory),
            self.loop_detector.validate(task),
        ):
            if check is not None:
                return DelegationPolicyResult(
                    accepted=False,
                    metadata=metadata,
                    audit_event=_audit_event(task, event_type="delegation_rejected", status="blocked", error=check),
                    error=check,
                )
        return DelegationPolicyResult(
            accepted=True,
            metadata=metadata,
            audit_event=_audit_event(task, event_type="delegation_dispatched", status="allowed"),
        )

    def completion_event(self, task: AgentTask, *, status: str) -> AgentDelegationAuditEvent:
        return _audit_event(
            task,
            event_type="delegation_completed",
            status="failed" if status == "failed" else "completed",
        )

    def _metadata_for_dispatch(self, task: AgentTask) -> dict[str, Any]:
        pairs = _delegation_pairs(task.metadata)
        pairs.append([task.source_agent_id, task.target_agent_id])
        metadata = {
            **task.metadata,
            "delegation_pairs": pairs,
            "delegation_budget": self.budget.metadata(task),
        }
        return self.redactor.sanitize_metadata(metadata)


def _has_message_payload(task: AgentTask) -> bool:
    message = task.message
    return bool(message.text or message.image_ids or message.video_ids or message.audio_id)


def _delegation_pairs(metadata: dict[str, Any]) -> list[list[str]]:
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
            and pair[0]
            and pair[1]
        ):
            pairs.append([pair[0], pair[1]])
    return pairs


def _audit_event(
    task: AgentTask,
    *,
    event_type: AgentDelegationAuditEventType,
    status: Literal["allowed", "blocked", "completed", "failed"],
    error: AgentCommunicationError | None = None,
) -> AgentDelegationAuditEvent:
    return AgentDelegationAuditEvent(
        event_type=event_type,
        task_id=task.task_id,
        source_agent_id=task.source_agent_id,
        target_agent_id=task.target_agent_id,
        correlation_id=task.session.correlation_id,
        status=status,
        policy_code=error.code if error is not None else None,
        detail=error.detail if error is not None else {},
    )
