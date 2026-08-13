"""Agent state and state transition helpers."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.runtime.cancellation import (
    CANCELLATION_ERROR_CODE,
    DEFAULT_CANCELLATION_MESSAGE,
)
from assistant_agent.context.models import ContextSourceResult
from assistant_agent.media.vision.models import PerceptionBundle
from assistant_agent.runtime.capability_grants import (
    CapabilityGrantValue,
    validate_capability_grant,
)
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.tools.models import RunToolCatalog, ToolCallRecord, ToolResult
from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID
from assistant_agent.identifiers import (
    new_run_id,
    new_tool_call_id,
    new_trace_id,
)


AgentStatus = Literal[
    "created", "running", "waiting_user", "completed", "failed", "cancelled"
]
TurnProvenance = Literal["product_turn", "time_travel"]


class AgentError(BaseModel):
    """Structured error recorded during an agent run."""

    message: str = Field(min_length=1)
    source: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentState(BaseModel):
    """Single state carrier for one user request."""

    run_id: str = Field(default_factory=new_run_id)
    trace_id: str = Field(default_factory=new_trace_id)
    agent_id: str = Field(default=DEFAULT_AGENT_ID, min_length=1)
    request: UserRequest

    memory_texts: tuple[str, ...] = Field(default=(), exclude=True)
    response_publish_status: str = Field(default="not_requested", exclude=True)
    response_final_fact_id: str | None = Field(default=None, exclude=True)
    memory_origin_run_id: str | None = Field(default=None, exclude=True)
    turn_provenance: TurnProvenance = Field(
        default="product_turn",
        exclude=True,
    )
    context_source_result: ContextSourceResult = Field(
        default_factory=ContextSourceResult
    )
    perception: PerceptionBundle | None = None
    capability_grants: list[CapabilityGrantValue] = Field(default_factory=list)
    session_restored_grant_ids: list[str] = Field(default_factory=list)
    run_tool_catalog: RunToolCatalog | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    response: AgentResponse | None = None
    errors: list[AgentError] = Field(default_factory=list)
    status: AgentStatus = "created"

    @classmethod
    def from_request(
        cls,
        request: UserRequest,
        run_id: str | None = None,
        trace_id: str | None = None,
        agent_id: str = DEFAULT_AGENT_ID,
    ) -> "AgentState":
        """Create state from a normalized user request."""

        return cls(
            run_id=run_id or new_run_id(),
            trace_id=trace_id or new_trace_id(),
            agent_id=agent_id,
            request=request,
        )

    @property
    def user_id(self) -> str:
        return self.request.user_id

    @property
    def session_id(self) -> str:
        return self.request.session_id

    def add_tool_call(
        self,
        tool_name: str,
        input: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
    ) -> ToolCallRecord:
        """Append a running tool call record and mark the run as active."""

        record = ToolCallRecord(
            tool_call_id=tool_call_id or new_tool_call_id(),
            tool_name=tool_name,
            input=input or {},
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        self.tool_calls.append(record)
        self.status = "running"
        return record

    def upsert_capability_grant(self, grant: CapabilityGrantValue) -> None:
        """Add or replace one trusted grant in deterministic order."""

        grant = validate_capability_grant(grant)
        self.capability_grants = [
            existing
            for existing in self.capability_grants
            if existing.grant_id != grant.grant_id
        ]
        self.capability_grants.append(grant)

    def complete_tool_call(
        self,
        tool_call_id: str,
        result: ToolResult,
        output_ref: str | None = None,
    ) -> ToolCallRecord:
        """Mark a tool call as succeeded and append its result."""

        record = self._get_tool_call(tool_call_id)
        record.status = "succeeded"
        record.finished_at = datetime.now(timezone.utc)
        record.output_ref = output_ref or result.output_ref
        self.tool_results.append(result)
        self.status = "running"
        return record

    def fail_tool_call(
        self,
        tool_call_id: str,
        error_message: str,
        result: ToolResult | None = None,
        error_details: dict[str, Any] | None = None,
        stop_run: bool = True,
    ) -> ToolCallRecord:
        """Mark a tool call as failed and record a structured error."""

        record = self._get_tool_call(tool_call_id)
        record.status = "failed"
        record.finished_at = datetime.now(timezone.utc)
        record.error_message = error_message
        details = {"tool_call_id": tool_call_id}
        if error_details is not None:
            details.update(error_details)
        self.errors.append(
            AgentError(
                message=error_message,
                source=record.tool_name,
                details=details,
            )
        )
        if result is not None:
            self.tool_results.append(result)
        self.status = "failed" if stop_run else "running"
        return record

    def set_response(self, response: AgentResponse) -> None:
        """Set final response and complete the run."""

        self.response = response
        self.status = "completed"

    def cancel(
        self,
        message: str = DEFAULT_CANCELLATION_MESSAGE,
        *,
        source: str = "agent_runtime",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Mark the run as cancelled without preserving a stale final response."""

        error_details = {"code": CANCELLATION_ERROR_CODE}
        if details is not None:
            error_details.update(details)
        if (
            not self.errors
            or self.errors[-1].details.get("code") != CANCELLATION_ERROR_CODE
        ):
            self.errors.append(
                AgentError(
                    message=message,
                    source=source,
                    details=error_details,
                )
            )
        self.response = None
        self.status = "cancelled"

    def _get_tool_call(self, tool_call_id: str) -> ToolCallRecord:
        for record in self.tool_calls:
            if record.tool_call_id == tool_call_id:
                return record
        raise ValueError(f"Tool call not found: {tool_call_id}")
