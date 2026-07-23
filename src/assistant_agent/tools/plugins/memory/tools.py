"""Read-only Agent tools for searchable daily memory records."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.memory.manager import MemoryManager
from assistant_agent.memory.read_policy import memory_usage_hint, trust_policy_metadata
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.tool_ids import (
    MEMORY_GET_TOOL_NAME,
    MEMORY_RETRIEVAL_CAPABILITY,
    MEMORY_RETRIEVAL_TOOL_NAME,
)
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import ToolInputBinding


class MemorySearchInput(BaseModel):
    """每日记忆搜索的公开输入。"""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = None
    session_id: str | None = None
    query: str = Field(min_length=1)


class MemoryGetInput(BaseModel):
    """按 ID 精确读取每日记忆的公开输入。"""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = None
    session_id: str | None = None
    memory_id: str = Field(min_length=1)


class MemorySearchTool(ToolBase):
    name = MEMORY_RETRIEVAL_TOOL_NAME
    description = "搜索过往对话中保存的详细每日记忆记录。"
    input_schema = MemorySearchInput
    output_schema = MemoryItem
    category = "read"
    toolset = "memory"
    requires_confirmation = False
    input_bindings = (
        ToolInputBinding(field="user_id", source="runtime_identity", key="user_id"),
        ToolInputBinding(field="session_id", source="runtime_identity", key="session_id"),
    )

    def _run(self, input: MemorySearchInput, context: ToolContext) -> ToolResult:
        manager = _manager_from_context(context)
        identity = _identity(input.user_id, input.session_id, context)
        if manager is None or identity is None:
            return _memory_error(self.name, "memory service is unavailable", "memory_unavailable")
        result = manager.search_for_identity(
            identity,
            MemoryQuery(
                user_id=identity.user_id,
                query=input.query,
                top_k=5,
                max_context_chars=2000,
                allowed_scopes=["project"],
                record_kinds=["daily"],
            ),
        )
        return _search_result(
            self.name,
            result.items,
            result.memory_context,
            errors=result.errors,
        )


class MemoryGetTool(ToolBase):
    name = MEMORY_GET_TOOL_NAME
    description = "根据记忆 ID 读取一条完整的每日记忆记录。"
    input_schema = MemoryGetInput
    output_schema = MemoryItem
    category = "read"
    toolset = "memory"
    requires_confirmation = False
    input_bindings = (
        ToolInputBinding(field="user_id", source="runtime_identity", key="user_id"),
        ToolInputBinding(field="session_id", source="runtime_identity", key="session_id"),
    )

    def _run(self, input: MemoryGetInput, context: ToolContext) -> ToolResult:
        manager = _manager_from_context(context)
        identity = _identity(input.user_id, input.session_id, context)
        if manager is None or identity is None:
            return _memory_error(self.name, "memory service is unavailable", "memory_unavailable")
        item = manager.get_for_identity(identity, input.memory_id)
        if item is None or not _is_daily(item):
            return _memory_error(self.name, "daily memory record was not found", "memory_not_found")
        return _search_result(self.name, [item], item.summary)


# Compatibility import for code that still names the old retrieval class.
MemoryRetrievalTool = MemorySearchTool


def _manager_from_context(context: ToolContext) -> MemoryManager | None:
    manager = context.metadata.get("memory_manager")
    return manager if isinstance(manager, MemoryManager) else None


def _identity(
    input_user_id: str | None,
    input_session_id: str | None,
    context: ToolContext,
) -> RequestIdentity | None:
    trusted_identity = context.metadata.get("request_identity")
    if isinstance(trusted_identity, dict):
        try:
            identity = RequestIdentity.model_validate(trusted_identity)
        except ValueError:
            identity = None
        if identity is not None and (context.user_id is None or identity.user_id == context.user_id):
            return identity
    user_id = context.user_id or input_user_id
    if not user_id:
        return None
    return RequestIdentity.for_user(
        user_id=user_id,
        session_id=context.session_id or input_session_id,
    )


def _is_daily(item: MemoryItem) -> bool:
    return item.content.get("record_kind") == "daily" or "daily" in item.tags


def _search_result(
    tool_name: str,
    items: list[MemoryItem],
    memory_context: str,
    *,
    errors: list[dict[str, Any]] | None = None,
) -> ToolResult:
    safe_errors = _safe_memory_errors(errors or [])
    if safe_errors and not items:
        first = safe_errors[0]
        return _memory_error(
            tool_name,
            "memory service is temporarily unavailable",
            str(first["code"]),
            errors=safe_errors,
        )
    trust_policy = trust_policy_metadata()
    usage_hint = memory_usage_hint()
    result_status = "partial" if safe_errors else "succeeded"
    data: dict[str, Any] = {
        "status": "partial" if safe_errors else ("succeeded" if items else "empty"),
        "items": [item.model_dump(mode="json") for item in items],
        "memory_context": memory_context,
        "total": len(items),
        "errors": safe_errors,
        "trust_policy": trust_policy,
        "usage_hint": usage_hint,
    }
    output_ref = f"memory://daily/{items[0].memory_id}" if items else None
    contract = build_capability_output_contract(
        capability=MEMORY_RETRIEVAL_CAPABILITY,
        status=result_status,
        output_ref=output_ref,
        data=data,
        errors=safe_errors,
        metadata={"source": "memory_manager", "record_kind": "daily"},
    )
    observation = {
        "status": data["status"],
        "summary": memory_context or "No matching daily memory records.",
        "items": [
            {
                key: payload[key]
                for key in ("memory_id", "summary", "created_at", "relevance", "content")
                if payload.get(key) not in (None, "", [], {})
            }
            for payload in data["items"]
        ],
        "total": len(items),
        "errors": safe_errors,
        "trust_policy": trust_policy,
        "usage_hint": usage_hint,
    }
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=observation,
        output_ref=output_ref,
        contract=contract,
    )


def _memory_error(
    tool_name: str,
    message: str,
    code: str,
    *,
    errors: list[dict[str, Any]] | None = None,
) -> ToolResult:
    safe_errors = errors or [{"code": code, "message": message, "recoverable": True}]
    contract = build_capability_output_contract(
        capability=MEMORY_RETRIEVAL_CAPABILITY,
        status="failed",
        errors=safe_errors,
        metadata={"source": "memory_manager", "record_kind": "daily"},
    )
    return ToolResult(
        tool_name=tool_name,
        success=False,
        error=message,
        data={
            "status": "failed",
            "errors": safe_errors,
            "contract": contract.model_dump(mode="json"),
        },
        model_observation={
            "status": "failed",
            "summary": message,
            "errors": safe_errors,
        },
        contract=contract,
    )


def _safe_memory_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for error in errors:
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str) or not code:
            continue
        item: dict[str, Any] = {
            "code": code,
            "message": str(error.get("message") or "memory service operation failed"),
            "recoverable": bool(error.get("recoverable", True)),
        }
        phase = error.get("phase")
        if isinstance(phase, str) and phase:
            item["detail"] = {"phase": phase}
        safe.append(item)
    return safe
