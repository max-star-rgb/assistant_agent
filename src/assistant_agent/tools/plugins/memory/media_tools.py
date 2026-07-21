"""Agent tools for governed Memory Server media ingestion."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.memory_media_ingestion import (
    MemoryMediaIngestionFile,
    MemoryMediaIngestionResult,
    MemoryMediaIngestionService,
    MemoryMediaTaskStatusResult,
)
from assistant_agent.schemas.tool_ids import MEMORY_INGEST_STATUS_TOOL_NAME, MEMORY_MEDIA_INGEST_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext


class MemoryMediaIngestInput(BaseModel):
    """Public input for submitting media references to the Memory Server."""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = None
    session_id: str | None = None
    files: list[MemoryMediaIngestionFile] = Field(default_factory=list, min_length=1)


class MemoryIngestStatusInput(BaseModel):
    """Public input for checking a Memory Server ingestion task."""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = None
    session_id: str | None = None
    task_id: str = Field(min_length=1)


class MemoryMediaIngestTool(ToolBase):
    name = MEMORY_MEDIA_INGEST_TOOL_NAME
    description = "Submit safe media file references to the configured Memory Server ingestion pipeline."
    input_schema = MemoryMediaIngestInput
    output_schema = MemoryMediaIngestionResult
    category = "write"
    toolset = "memory"
    requires_confirmation = True

    def __init__(self, service: Any | None = None) -> None:
        self.service = service or MemoryMediaIngestionService(remote_client=None)

    def _run(self, input: MemoryMediaIngestInput, context: ToolContext) -> ToolResult:
        identity = _identity_from_context_or_input(
            context, input.user_id, input.session_id
        )
        if identity is None:
            return _missing_identity_result(self.name, capability=MEMORY_MEDIA_INGEST_TOOL_NAME)
        result = self.service.ingest(identity=identity, files=input.files)
        return _ingest_tool_result(result)


class MemoryIngestStatusTool(ToolBase):
    name = MEMORY_INGEST_STATUS_TOOL_NAME
    description = "Check the status of a Memory Server media ingestion task."
    input_schema = MemoryIngestStatusInput
    output_schema = MemoryMediaTaskStatusResult
    category = "read"
    toolset = "memory"
    requires_confirmation = False

    def __init__(self, service: Any | None = None) -> None:
        self.service = service or MemoryMediaIngestionService(remote_client=None)

    def _run(self, input: MemoryIngestStatusInput, context: ToolContext) -> ToolResult:
        identity = _identity_from_context_or_input(
            context, input.user_id, input.session_id
        )
        if identity is None:
            return _missing_identity_result(
                self.name, capability=MEMORY_INGEST_STATUS_TOOL_NAME
            )
        result = self.service.task_status(identity=identity, task_id=input.task_id)
        return _status_tool_result(result)


def _identity_from_context_or_input(
    context: ToolContext,
    user_id: str | None,
    session_id: str | None,
) -> RequestIdentity | None:
    resolved_user_id = context.user_id or user_id
    if not resolved_user_id:
        return None
    return RequestIdentity.for_user(
        user_id=resolved_user_id,
        session_id=context.session_id or session_id,
    )


def _ingest_tool_result(result: MemoryMediaIngestionResult) -> ToolResult:
    data = result.model_dump(mode="json")
    success = not result.errors and result.status not in {
        "failed",
        "provider_unconfigured",
    }
    contract = build_capability_output_contract(
        capability=MEMORY_MEDIA_INGEST_TOOL_NAME,
        status="succeeded" if success else "failed",
        output_ref=result.output_ref,
        data=data,
        errors=result.errors,
        metadata={"provider": "memory_server"},
    )
    return ToolResult(
        tool_name=MemoryMediaIngestTool.name,
        success=success,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_memory_media_model_observation(data),
        error=_first_error(result.errors),
        output_ref=result.output_ref,
        contract=contract,
    )


def _status_tool_result(result: MemoryMediaTaskStatusResult) -> ToolResult:
    data = result.model_dump(mode="json")
    success = not result.errors and result.status not in {
        "failed",
        "provider_unconfigured",
        "not_found",
    }
    contract = build_capability_output_contract(
        capability=MEMORY_INGEST_STATUS_TOOL_NAME,
        status="succeeded" if success else "failed",
        output_ref=result.output_ref,
        data=data,
        errors=result.errors,
        metadata={"provider": "memory_server", "scope_warning": result.scope_warning},
    )
    return ToolResult(
        tool_name=MemoryIngestStatusTool.name,
        success=success,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_memory_media_model_observation(data),
        error=_first_error(result.errors),
        output_ref=result.output_ref,
        contract=contract,
    )


def _missing_identity_result(tool_name: str, *, capability: str) -> ToolResult:
    message = "缺少用户身份，无法访问 Memory Server 记忆摄入服务"
    contract = build_capability_output_contract(
        capability=capability,
        status="failed",
        errors=[{"code": "missing_identity", "message": message, "recoverable": True}],
    )
    return ToolResult(
        tool_name=tool_name,
        success=False,
        error=message,
        data={
            "status": "missing_identity",
            "contract": contract.model_dump(mode="json"),
        },
        model_observation={
            "status": "missing_identity",
            "summary": message,
            "errors": [
                {"code": "missing_identity", "message": message, "recoverable": True}
            ],
        },
        contract=contract,
    )


def _first_error(errors: list[dict[str, Any]]) -> str | None:
    if not errors:
        return None
    first = errors[0]
    code = str(first.get("code") or "tool_failed")
    message = str(first.get("message") or "Tool failed.")
    return f"{code}: {message}"


def _memory_media_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    file_count = data.get("file_count")
    if file_count is None and isinstance(data.get("files"), list):
        file_count = len(data["files"])
    observation: dict[str, Any] = {
        "status": data.get("status"),
        "summary": data.get("summary")
        or _memory_media_summary(data, file_count=file_count),
        "task_id": data.get("task_id"),
        "file_count": file_count,
        "scope_warning": data.get("scope_warning"),
        "output_ref": data.get("output_ref"),
        "errors": data.get("errors"),
    }
    return {
        key: value
        for key, value in observation.items()
        if value not in (None, "", [], {})
    }


def _memory_media_summary(data: dict[str, Any], *, file_count: Any) -> str:
    status = data.get("status") or "unknown"
    if status in {"failed", "provider_unconfigured", "not_found"}:
        errors = data.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or "Memory media ingestion failed.")
        return "Memory media ingestion failed."
    if file_count:
        return f"Memory media ingestion status: {status}; files: {file_count}."
    return f"Memory media ingestion status: {status}."
