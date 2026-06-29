"""Mock memory tool."""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from multimodal_agent.memory.manager import MemoryConfirmationRequired, MemoryManager
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.schemas.memory import MemoryQuery
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.schemas.capability_output import build_capability_output_contract
from multimodal_agent.memory.write_policy import build_explicit_memory_item
from multimodal_agent.tools.base import MockTool, ToolContext


class MemoryInput(BaseModel):
    action: Literal["retrieve", "save"]
    user_id: str = Field(min_length=1)
    session_id: str | None = None
    query: str | None = None
    content: dict = Field(default_factory=dict)


class MemoryRetrievalInput(BaseModel):
    """Public input for the dedicated memory retrieval tool."""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = None
    session_id: str | None = None
    query: str | None = None
    content: dict = Field(default_factory=dict)


class MemorySaveInput(BaseModel):
    """Public input for the dedicated memory save tool."""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = None
    session_id: str | None = None
    query: str | None = None
    content: dict = Field(default_factory=dict)


class MemoryTool(MockTool):
    name = "memory"
    description = "Mock memory save and retrieval."
    input_schema = MemoryInput
    output_schema = MemoryItem

    def _run(self, input: MemoryInput, context: ToolContext) -> ToolResult:
        input = _bind_context_identity(input, context)
        if input.action == "retrieve":
            if not input.query:
                contract = build_capability_output_contract(
                    capability="memory_retrieval",
                    status="failed",
                    errors=[{"code": "missing_required_input", "message": "缺少检索 query，无法检索记忆", "recoverable": True}],
                )
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error="缺少检索 query，无法检索记忆",
                    contract=contract,
                )
            manager = _manager_from_context(context)
            if manager is not None:
                result = _retrieve_with_manager(input, manager, self.name)
                if result is not None:
                    return result
            item = MemoryItem(
                memory_id="m1",
                user_id=input.user_id,
                memory_type="product",
                content={"item": "黑色包", "style": "通勤"},
                summary="用户上次关注了一个黑色通勤包。",
                relevance=0.9,
                reason="query 命中历史商品描述",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            data = {"items": [item.model_dump()]}
            contract = build_capability_output_contract(
                capability="memory_retrieval",
                status="succeeded",
                output_ref="mock://memory/m1",
                data={"items": [item.model_dump(mode="json")], "memory_context": item.summary},
                metadata={"provider": "mock", "source": "memory"},
            )
            return ToolResult(
                tool_name=self.name,
                success=True,
                data={**data, "contract": contract.model_dump(mode="json")},
                output_ref="mock://memory/m1",
                latency_ms=1,
                contract=contract,
            )

        text = _explicit_save_text(input)
        if not text:
            return _missing_memory_save_content(self.name)

        manager = _manager_from_context(context)
        if manager is not None:
            return _save_with_manager(input, context, manager, self.name)

        item = build_explicit_memory_item(
            memory_id="m_saved_1",
            user_id=input.user_id,
            session_id=input.session_id or str(input.content.get("session_id") or "default"),
            text=text,
            content=input.content,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        item = item.model_copy(update={"summary": "已保存用户偏好。"})
        data = item.model_dump()
        contract = build_capability_output_contract(
            capability="memory_save",
            status="succeeded",
            output_ref="mock://memory/m_saved_1",
            data={"memory_id": item.memory_id, "summary": item.summary, "memory_type": item.memory_type},
            metadata={"provider": "mock", "source": "memory"},
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={**data, "contract": contract.model_dump(mode="json")},
            output_ref="mock://memory/m_saved_1",
            latency_ms=1,
            contract=contract,
        )


class MemoryRetrievalTool(MemoryTool):
    name = "memory_retrieval"
    input_schema = MemoryRetrievalInput

    def _run(self, input: MemoryRetrievalInput, context: ToolContext) -> ToolResult:
        payload = _dedicated_memory_input("retrieve", input, context, self.name)
        if isinstance(payload, ToolResult):
            return payload
        return super()._run(payload, context)


class MemorySaveTool(MemoryTool):
    name = "memory_save"
    input_schema = MemorySaveInput

    def _run(self, input: MemorySaveInput, context: ToolContext) -> ToolResult:
        payload = _dedicated_memory_input("save", input, context, self.name)
        if isinstance(payload, ToolResult):
            return payload
        return super()._run(payload, context)


def _manager_from_context(context: ToolContext) -> MemoryManager | None:
    manager = context.metadata.get("memory_manager")
    return manager if isinstance(manager, MemoryManager) else None


def _dedicated_memory_input(
    action: Literal["retrieve", "save"],
    input: MemoryRetrievalInput | MemorySaveInput,
    context: ToolContext,
    tool_name: str,
) -> MemoryInput | ToolResult:
    user_id = context.user_id or input.user_id
    if not user_id:
        return ToolResult(tool_name=tool_name, success=False, error="缺少用户身份，无法访问记忆")
    return MemoryInput(
        action=action,
        user_id=user_id,
        session_id=context.session_id or input.session_id,
        query=input.query,
        content=input.content,
    )


def _bind_context_identity(input: MemoryInput, context: ToolContext) -> MemoryInput:
    updates: dict[str, str] = {}
    if context.user_id:
        updates["user_id"] = context.user_id
    if context.session_id:
        updates["session_id"] = context.session_id
    return input.model_copy(update=updates) if updates else input


def _retrieve_with_manager(input: MemoryInput, manager: MemoryManager, tool_name: str) -> ToolResult | None:
    identity = RequestIdentity.for_user(user_id=input.user_id, session_id=input.session_id)
    query = MemoryQuery(
        user_id=input.user_id,
        query=input.query or "",
        capability=str(input.content.get("capability") or "") or None,
        top_k=_int_content(input.content, "top_k", default=5),
        max_context_chars=_int_content(input.content, "max_context_chars", default=500),
    )
    result = manager.search_for_identity(identity, query)
    if not result.items:
        return None

    output_ref = f"local://memory/{result.items[0].memory_id}"
    data = {
        "items": [item.model_dump(mode="json") for item in result.items],
        "memory_context": result.memory_context,
        "total": result.total,
    }
    contract = build_capability_output_contract(
        capability="memory_retrieval",
        status="succeeded",
        output_ref=output_ref,
        data=data,
        metadata={"provider": "local", "source": "memory_manager"},
    )
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={**data, "contract": contract.model_dump(mode="json")},
        output_ref=output_ref,
        latency_ms=1,
        contract=contract,
    )


def _save_with_manager(
    input: MemoryInput,
    context: ToolContext,
    manager: MemoryManager,
    tool_name: str,
) -> ToolResult:
    session_id = input.session_id or context.session_id or str(input.content.get("session_id") or "default")
    text = _explicit_save_text(input)
    if not text:
        return _missing_memory_save_content(tool_name)
    identity = RequestIdentity.for_user(user_id=input.user_id, session_id=session_id)
    try:
        item = manager.save_explicit_for_identity(
            identity,
            memory_id=f"explicit_memory_{uuid4().hex}",
            text=text,
            content=input.content,
        )
    except MemoryConfirmationRequired as exc:
        return _memory_confirmation_required_result(tool_name, exc)
    except ValueError as exc:
        return _memory_save_rejected_result(tool_name, str(exc))
    display_item = item.model_copy(update={"summary": "已保存用户偏好。"})
    data = display_item.model_dump(mode="json")
    output_ref = f"local://memory/{item.memory_id}"
    contract = build_capability_output_contract(
        capability="memory_save",
        status="succeeded",
        output_ref=output_ref,
        data={"memory_id": item.memory_id, "summary": display_item.summary, "memory_type": item.memory_type},
        metadata={"provider": "local", "source": "memory_manager"},
    )
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={**data, "contract": contract.model_dump(mode="json")},
        output_ref=output_ref,
        latency_ms=1,
        contract=contract,
    )


def _memory_confirmation_required_result(tool_name: str, exc: MemoryConfirmationRequired) -> ToolResult:
    confirmation = exc.confirmation
    data = {
        "requires_confirmation": True,
        "confirmation_id": confirmation.confirmation_id,
        "status": confirmation.status,
        "summary": confirmation.summary,
        "reason": confirmation.reason,
        "expires_at": confirmation.expires_at.isoformat() if confirmation.expires_at else None,
    }
    output_ref = f"local://memory/confirmations/{confirmation.confirmation_id}"
    contract = build_capability_output_contract(
        capability="memory_save",
        status="partial",
        output_ref=output_ref,
        data=data,
        errors=[
            {
                "code": "memory_confirmation_required",
                "message": "记忆包含需要用户确认的敏感内容，确认后才会保存。",
                "recoverable": True,
            }
        ],
        metadata={"provider": "local", "source": "memory_manager", "requires_confirmation": True},
    )
    return ToolResult(
        tool_name=tool_name,
        success=False,
        data={**data, "contract": contract.model_dump(mode="json")},
        error="记忆包含需要用户确认的敏感内容，确认后才会保存。",
        output_ref=output_ref,
        latency_ms=1,
        contract=contract,
    )


def _memory_save_rejected_result(tool_name: str, reason: str) -> ToolResult:
    contract = build_capability_output_contract(
        capability="memory_save",
        status="failed",
        errors=[
            {
                "code": "memory_write_rejected",
                "message": reason or "记忆写入被策略拒绝。",
                "recoverable": False,
            }
        ],
        metadata={"provider": "local", "source": "memory_manager"},
    )
    return ToolResult(
        tool_name=tool_name,
        success=False,
        error=reason or "记忆写入被策略拒绝。",
        latency_ms=1,
        contract=contract,
        data={"contract": contract.model_dump(mode="json")},
    )


def _explicit_save_text(input: MemoryInput) -> str:
    return str(input.query or input.content.get("text") or input.content.get("summary") or "").strip()


def _missing_memory_save_content(tool_name: str) -> ToolResult:
    contract = build_capability_output_contract(
        capability="memory_save",
        status="failed",
        errors=[{"code": "missing_required_input", "message": "缺少保存内容，无法写入记忆", "recoverable": True}],
    )
    return ToolResult(
        tool_name=tool_name,
        success=False,
        error="缺少保存内容，无法写入记忆",
        contract=contract,
    )


def _int_content(content: dict, key: str, *, default: int) -> int:
    value = content.get(key)
    if isinstance(value, int):
        return value
    return default
