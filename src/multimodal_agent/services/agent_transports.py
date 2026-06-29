"""Transports for optional agent-to-agent communication."""

from __future__ import annotations

from typing import Any, Protocol

from multimodal_agent.agent.state import AgentState
from multimodal_agent.schemas.agent_communication import (
    AgentArtifact,
    AgentCommunicationError,
    AgentTask,
    AgentTaskResult,
)
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message


class AgentTransport(Protocol):
    """Protocol-neutral transport for sending a task to an agent instance."""

    name: str

    def send_task(self, task: AgentTask) -> AgentTaskResult:
        """Execute or deliver one agent task."""


class LocalAgentTransport:
    """In-process transport backed by local AgentGraphRuntime-like objects."""

    name = "local"

    def __init__(self, runtimes: dict[str, Any]) -> None:
        self._runtimes = dict(runtimes)

    def send_task(self, task: AgentTask) -> AgentTaskResult:
        runtime = self._runtimes.get(task.target_agent_id)
        if runtime is None:
            return _failed_result(
                task,
                "agent_runtime_not_found",
                f"No local runtime registered for agent: {task.target_agent_id}",
                detail={"agent_id": task.target_agent_id, "transport": self.name},
                recoverable=True,
            )
        try:
            state = runtime.run_state(_request_from_task(task))
        except Exception as exc:  # pragma: no cover - defensive transport boundary
            return _failed_result(
                task,
                "agent_transport_failed",
                exc,
                detail={"agent_id": task.target_agent_id, "transport": self.name},
            )
        return _result_from_state(task, state, transport_name=self.name)


def _request_from_task(task: AgentTask) -> UserRequest:
    metadata = {
        **task.message.metadata,
        **task.metadata,
        "agent_communication": {
            "task_id": task.task_id,
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "parent_run_id": task.session.parent_run_id,
            "parent_trace_id": task.session.parent_trace_id,
            "correlation_id": task.session.correlation_id,
            "delegation_depth": task.delegation_depth,
            "max_delegation_depth": task.max_delegation_depth,
            "transport": "local",
        },
    }
    return UserRequest(
        user_id=task.session.user_id,
        session_id=task.session.session_id,
        text=task.message.text,
        image_ids=list(task.message.image_ids),
        video_ids=list(task.message.video_ids),
        audio_id=task.message.audio_id,
        metadata=sanitize_error_detail(metadata),
    )


def _result_from_state(task: AgentTask, state: AgentState, *, transport_name: str) -> AgentTaskResult:
    errors = [
        AgentCommunicationError(
            code=str(error.details.get("code") or "agent_run_error"),
            message=sanitize_error_message(error.message),
            detail=sanitize_error_detail(error.details),
            recoverable=bool(error.details.get("retryable", False)),
        )
        for error in state.errors
    ]
    artifacts = []
    if state.response is not None:
        artifacts.append(
            AgentArtifact(
                kind="text",
                text=state.response.message,
                data=sanitize_error_detail(state.response.data or {}),
                output_refs=list(state.response.output_refs),
                metadata={"source": "agent_response"},
            )
        )
    status = "failed" if state.status == "failed" else "completed"
    return AgentTaskResult(
        task_id=task.task_id,
        target_agent_id=task.target_agent_id,
        status=status,
        artifacts=artifacts,
        run_id=state.run_id,
        trace_id=state.trace_id,
        errors=errors,
        metadata={
            "transport": transport_name,
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "correlation_id": task.session.correlation_id,
        },
    )


def _failed_result(
    task: AgentTask,
    code: str,
    message: object,
    *,
    detail: dict[str, Any] | None = None,
    recoverable: bool = False,
) -> AgentTaskResult:
    return AgentTaskResult(
        task_id=task.task_id,
        target_agent_id=task.target_agent_id,
        status="failed",
        errors=[
            AgentCommunicationError(
                code=code,
                message=sanitize_error_message(message),
                detail=sanitize_error_detail(detail or {}),
                recoverable=recoverable,
            )
        ],
        metadata={
            "transport": "local",
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "correlation_id": task.session.correlation_id,
        },
    )
