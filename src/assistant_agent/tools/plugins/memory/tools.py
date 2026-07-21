"""Governed memory retrieval and save tools."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.memory.manager import (
    MemoryConfirmationRequired,
    MemoryManager,
    MemorySaveCandidateResult,
)
from assistant_agent.memory.read_policy import memory_usage_hint, trust_policy_metadata
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.memory import MemoryQuery
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.tool_ids import (
    MEMORY_RETRIEVAL_CAPABILITY,
    MEMORY_RETRIEVAL_TOOL_NAME,
    MEMORY_SAVE_CAPABILITY,
    MEMORY_SAVE_TOOL_NAME,
)
from assistant_agent.tools.base import ToolBase, ToolContext


class _MemoryOperationInput(BaseModel):
    action: Literal["retrieve", "save"]
    user_id: str = Field(min_length=1)
    session_id: str | None = None
    query: str | None = None
    content: dict = Field(default_factory=dict)
    source_intent: (
        Literal["user_explicit", "assistant_candidate", "user_confirmed"] | None
    ) = None
    source_reason: str | None = None
    future_use: str | None = None
    evidence: str | None = None


class MemoryRetrievalInput(BaseModel):
    """Public input for the dedicated memory retrieval tool."""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = None
    session_id: str | None = None
    query: str = Field(min_length=1)
    content: dict = Field(default_factory=dict)


class MemorySaveInput(BaseModel):
    """Public input for the dedicated memory save tool."""

    model_config = ConfigDict(extra="ignore")

    user_id: str | None = None
    session_id: str | None = None
    query: str | None = None
    content: dict = Field(default_factory=dict)
    source_intent: Literal["user_explicit", "assistant_candidate"]
    source_reason: str = Field(min_length=1)
    future_use: str = Field(min_length=1)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_memory_content(self) -> "MemorySaveInput":
        if isinstance(self.query, str) and self.query.strip():
            return self
        if self.content.get("text") or self.content.get("summary"):
            return self
        raise ValueError("memory_save requires query, content.text, or content.summary")


class _MemoryOperationTool(ToolBase):
    """Shared implementation for the dedicated memory tools."""

    name = "memory_operation"
    description = "Internal memory save and retrieval implementation."
    input_schema = _MemoryOperationInput
    output_schema = MemoryItem

    def _run(self, input: _MemoryOperationInput, context: ToolContext) -> ToolResult:
        input = _bind_context_identity(input, context)
        if input.action == "retrieve":
            if not input.query:
                contract = build_capability_output_contract(
                    capability=MEMORY_RETRIEVAL_CAPABILITY,
                    status="failed",
                    errors=[
                        {
                            "code": "missing_required_input",
                            "message": "缺少检索 query，无法检索记忆",
                            "recoverable": True,
                        }
                    ],
                )
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    model_observation=_memory_error_model_observation(
                        "缺少检索 query，无法检索记忆",
                        code="missing_required_input",
                    ),
                    error="缺少检索 query，无法检索记忆",
                    contract=contract,
                )
            manager = _manager_from_context(context)
            if manager is not None:
                result = _retrieve_with_manager(input, manager, self.name, context)
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
                capability=MEMORY_RETRIEVAL_CAPABILITY,
                status="succeeded",
                output_ref="mock://memory/m1",
                data={
                    "items": [item.model_dump(mode="json")],
                    "memory_context": item.summary,
                },
                metadata={"provider": "mock", "source": "memory"},
            )
            return ToolResult(
                tool_name=self.name,
                success=True,
                data={**data, "contract": contract.model_dump(mode="json")},
                model_observation=_memory_retrieval_model_observation(
                    {
                        **data,
                        "memory_context": item.summary,
                        "total": 1,
                    }
                ),
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
        if input.source_intent == "assistant_candidate":
            return _candidate_recorded_result(
                self.name,
                MemorySaveCandidateResult(
                    candidate_id=f"memory_candidate_{uuid4().hex}",
                    user_id=input.user_id,
                    session_id=input.session_id
                    or str(input.content.get("session_id") or "default"),
                    summary=text,
                    source_reason=input.source_reason or "",
                    future_use=input.future_use or "",
                    evidence=input.evidence or "",
                    reason="assistant candidate recorded without durable write",
                ),
            )
        if input.source_intent == "user_confirmed":
            return _memory_save_rejected_result(
                self.name,
                "source_intent=user_confirmed is reserved for confirmation service",
            )

        return _mock_memory_saved_result(self.name, input, text)


class MemoryRetrievalTool(_MemoryOperationTool):
    name = MEMORY_RETRIEVAL_TOOL_NAME
    description = "Retrieve prior user memory when the request needs saved context."
    input_schema = MemoryRetrievalInput
    category = "read"
    toolset = "memory"
    requires_confirmation = False

    def _run(self, input: MemoryRetrievalInput, context: ToolContext) -> ToolResult:
        payload = _dedicated_memory_input("retrieve", input, context, self.name)
        if isinstance(payload, ToolResult):
            return payload
        return super()._run(payload, context)


class MemorySaveTool(_MemoryOperationTool):
    name = MEMORY_SAVE_TOOL_NAME
    description = "Save an explicit memory or record a candidate stable preference for later review."
    input_schema = MemorySaveInput
    category = "write"
    toolset = "memory"
    requires_confirmation = False

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
) -> _MemoryOperationInput | ToolResult:
    user_id = context.user_id or input.user_id
    if not user_id:
        return ToolResult(
            tool_name=tool_name,
            success=False,
            model_observation=_memory_error_model_observation(
                "缺少用户身份，无法访问记忆",
                code="missing_identity",
            ),
            error="缺少用户身份，无法访问记忆",
        )
    return _MemoryOperationInput(
        action=action,
        user_id=user_id,
        session_id=context.session_id or input.session_id,
        query=input.query,
        content=input.content,
        source_intent=getattr(input, "source_intent", None),
        source_reason=getattr(input, "source_reason", None),
        future_use=getattr(input, "future_use", None),
        evidence=getattr(input, "evidence", None),
    )


def _bind_context_identity(
    input: _MemoryOperationInput,
    context: ToolContext,
) -> _MemoryOperationInput:
    updates: dict[str, str] = {}
    if context.user_id:
        updates["user_id"] = context.user_id
    if context.session_id:
        updates["session_id"] = context.session_id
    return input.model_copy(update=updates) if updates else input


def _retrieve_with_manager(
    input: _MemoryOperationInput,
    manager: MemoryManager,
    tool_name: str,
    context: ToolContext,
) -> ToolResult | None:
    identity = RequestIdentity.for_user(
        user_id=input.user_id, session_id=input.session_id
    )
    query = MemoryQuery(
        user_id=input.user_id,
        query=input.query or "",
        capability=str(input.content.get("capability") or "") or None,
        top_k=_int_content(input.content, "top_k", default=5),
        max_context_chars=_int_content(
            input.content, "max_context_chars", default=500
        ),
    )
    result = manager.search_for_identity(identity, query)
    if not result.items:
        return None

    output_ref = f"local://memory/{result.items[0].memory_id}"
    trust_policy = trust_policy_metadata()
    usage_hint = memory_usage_hint()
    data = {
        "items": [item.model_dump(mode="json") for item in result.items],
        "memory_context": result.memory_context,
        "total": result.total,
        "trust_policy": trust_policy,
        "usage_hint": usage_hint,
    }
    contract = build_capability_output_contract(
        capability=MEMORY_RETRIEVAL_CAPABILITY,
        status="succeeded",
        output_ref=output_ref,
        data=data,
        metadata={
            "provider": "local",
            "source": "memory_manager",
            "trust_policy": trust_policy,
            "usage_hint": usage_hint,
        },
    )
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_memory_retrieval_model_observation(data),
        output_ref=output_ref,
        latency_ms=1,
        contract=contract,
    )


def _save_with_manager(
    input: _MemoryOperationInput,
    context: ToolContext,
    manager: MemoryManager,
    tool_name: str,
) -> ToolResult:
    session_id = (
        input.session_id
        or context.session_id
        or str(input.content.get("session_id") or "default")
    )
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
            source_intent=input.source_intent,
            source_reason=input.source_reason,
            future_use=input.future_use,
            evidence=input.evidence,
        )
    except MemoryConfirmationRequired as exc:
        return _memory_confirmation_required_result(tool_name, exc)
    except ValueError as exc:
        return _memory_save_rejected_result(tool_name, str(exc))
    if isinstance(item, MemorySaveCandidateResult):
        return _candidate_recorded_result(tool_name, item)
    framework_queued = item.content.get("_framework_retain_status") == "queued"
    result_status = "queued" if framework_queued else "saved"
    written = not framework_queued
    display_item = item.model_copy(update={"summary": "已保存用户偏好。"})
    data = {
        **display_item.model_dump(mode="json"),
        "status": result_status,
        "written": written,
        "durable_outbox": framework_queued,
        "source_intent": input.source_intent or "user_explicit",
        "source_reason": input.source_reason,
        "future_use": input.future_use,
        "evidence": input.evidence,
    }
    output_ref = f"local://memory/{item.memory_id}"
    contract = build_capability_output_contract(
        capability=MEMORY_SAVE_CAPABILITY,
        status="partial" if framework_queued else "succeeded",
        output_ref=output_ref,
        data={
            "status": result_status,
            "written": written,
            "durable_outbox": framework_queued,
            "memory_id": item.memory_id,
            "summary": display_item.summary,
            "memory_type": item.memory_type,
            "source_intent": input.source_intent or "user_explicit",
        },
        errors=(
            [
                {
                    "code": "memory_framework_retain_queued",
                    "message": "memory framework unavailable; approved write queued for retry",
                    "recoverable": True,
                }
            ]
            if framework_queued
            else []
        ),
        metadata={
            "provider": "local",
            "source": "memory_manager",
            "written": written,
            "durable_outbox": framework_queued,
        },
    )
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_memory_save_model_observation(data),
        output_ref=output_ref,
        latency_ms=1,
        contract=contract,
    )


def _memory_confirmation_required_result(
    tool_name: str, exc: MemoryConfirmationRequired
) -> ToolResult:
    confirmation = exc.confirmation
    data = {
        "status": "confirmation_required",
        "written": False,
        "requires_confirmation": True,
        "confirmation_id": confirmation.confirmation_id,
        "confirmation_status": confirmation.status,
        "confirmation_kind": confirmation.confirmation_kind,
        "fact_key": confirmation.fact_key,
        "summary": confirmation.summary,
        "reason": confirmation.reason,
        "expires_at": confirmation.expires_at.isoformat()
        if confirmation.expires_at
        else None,
    }
    output_ref = f"local://memory/confirmations/{confirmation.confirmation_id}"
    contract = build_capability_output_contract(
        capability=MEMORY_SAVE_CAPABILITY,
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
        metadata={
            "provider": "local",
            "source": "memory_manager",
            "requires_confirmation": True,
        },
    )
    return ToolResult(
        tool_name=tool_name,
        success=False,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_memory_save_model_observation(data),
        error="记忆包含需要用户确认的敏感内容，确认后才会保存。",
        output_ref=output_ref,
        latency_ms=1,
        contract=contract,
    )


def _memory_save_rejected_result(tool_name: str, reason: str) -> ToolResult:
    contract = build_capability_output_contract(
        capability=MEMORY_SAVE_CAPABILITY,
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
        data={
            "status": "rejected",
            "written": False,
            "contract": contract.model_dump(mode="json"),
        },
        model_observation=_memory_save_model_observation(
            {
                "status": "rejected",
                "written": False,
                "reason": reason or "记忆写入被策略拒绝。",
            }
        ),
    )


def _mock_memory_saved_result(
    tool_name: str, input: _MemoryOperationInput, text: str
) -> ToolResult:
    memory_id = "m_saved_1"
    session_id = input.session_id or str(input.content.get("session_id") or "default")
    data = {
        "memory_id": memory_id,
        "user_id": input.user_id,
        "session_id": session_id,
        "memory_type": "preference",
        "content": dict(input.content),
        "summary": "已保存用户偏好。",
        "status": "saved",
        "written": True,
        "source_intent": input.source_intent or "user_explicit",
        "source_reason": input.source_reason,
        "future_use": input.future_use,
        "evidence": input.evidence,
        "text_chars": len(text),
    }
    contract = build_capability_output_contract(
        capability=MEMORY_SAVE_CAPABILITY,
        status="succeeded",
        output_ref=f"mock://memory/{memory_id}",
        data={
            "status": "saved",
            "written": True,
            "memory_id": memory_id,
            "summary": data["summary"],
            "memory_type": data["memory_type"],
            "source_intent": data["source_intent"],
        },
        metadata={
            "provider": "mock",
            "source": "memory_tool_fallback",
            "written": True,
        },
    )
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_memory_save_model_observation(data),
        output_ref=f"mock://memory/{memory_id}",
        latency_ms=1,
        contract=contract,
    )


def _candidate_recorded_result(
    tool_name: str, candidate: MemorySaveCandidateResult
) -> ToolResult:
    data = {
        "status": "candidate_recorded",
        "written": False,
        "candidate_id": candidate.candidate_id,
        "source_intent": candidate.source_intent,
        "summary": candidate.summary,
        "memory_type": candidate.memory_type,
        "source_reason": candidate.source_reason,
        "future_use": candidate.future_use,
        "evidence": candidate.evidence,
        "reason": candidate.reason,
    }
    output_ref = f"local://memory/candidates/{candidate.candidate_id}"
    contract = build_capability_output_contract(
        capability=MEMORY_SAVE_CAPABILITY,
        status="skipped",
        output_ref=output_ref,
        data=data,
        metadata={"provider": "local", "source": "memory_manager", "written": False},
    )
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_memory_save_model_observation(data),
        output_ref=output_ref,
        latency_ms=1,
        contract=contract,
    )


def _explicit_save_text(input: _MemoryOperationInput) -> str:
    return str(
        input.query or input.content.get("text") or input.content.get("summary") or ""
    ).strip()


def _missing_memory_save_content(tool_name: str) -> ToolResult:
    contract = build_capability_output_contract(
        capability=MEMORY_SAVE_CAPABILITY,
        status="failed",
        errors=[
            {
                "code": "missing_required_input",
                "message": "缺少保存内容，无法写入记忆",
                "recoverable": True,
            }
        ],
    )
    return ToolResult(
        tool_name=tool_name,
        success=False,
        model_observation=_memory_error_model_observation(
            "缺少保存内容，无法写入记忆",
            code="missing_required_input",
        ),
        error="缺少保存内容，无法写入记忆",
        contract=contract,
    )


def _int_content(content: dict, key: str, *, default: int) -> int:
    value = content.get(key)
    if isinstance(value, int):
        return value
    return default


def _memory_retrieval_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "status": data.get("status") or ("succeeded" if data.get("items") else "empty"),
        "summary": data.get("memory_context")
        or data.get("summary")
        or "Memory retrieval completed.",
        "memory_context": data.get("memory_context"),
        "items": [
            _memory_item_model_observation(item)
            for item in data.get("items", [])
            if isinstance(item, dict)
        ],
        "total": data.get("total"),
        "trust_policy": data.get("trust_policy"),
        "usage_hint": data.get("usage_hint"),
        "read_policy": data.get("read_policy"),
    }
    return {
        key: value
        for key, value in observation.items()
        if value not in (None, "", [], {})
    }


def _memory_item_model_observation(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("memory_type", "content", "summary", "relevance", "reason", "created_at")
    return {key: item[key] for key in keys if item.get(key) not in (None, "", [], {})}


def _memory_save_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "status": data.get("status"),
        "summary": data.get("summary")
        or data.get("reason")
        or "Memory save completed.",
        "written": data.get("written"),
        "requires_confirmation": data.get("requires_confirmation"),
        "confirmation_status": data.get("confirmation_status"),
        "confirmation_kind": data.get("confirmation_kind"),
        "memory_type": data.get("memory_type"),
        "source_intent": data.get("source_intent"),
        "source_reason": data.get("source_reason"),
        "future_use": data.get("future_use"),
        "evidence": data.get("evidence"),
        "durable_outbox": data.get("durable_outbox"),
        "reason": data.get("reason"),
        "expires_at": data.get("expires_at"),
    }
    return {
        key: value
        for key, value in observation.items()
        if value not in (None, "", [], {})
    }


def _memory_error_model_observation(message: str, *, code: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "summary": message,
        "errors": [{"code": code, "message": message, "recoverable": True}],
    }
