"""Agent state and state transition helpers."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from multimodal_agent.services.trace_store import new_trace_id
from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.schemas.perception import PerceptionBundle
from multimodal_agent.schemas.planning import IntentResult, TaskPlan
from multimodal_agent.schemas.requests import AgentResponse, UserRequest
from multimodal_agent.schemas.tools import ToolCallRecord, ToolResult, ToolSelection
from multimodal_agent.services.provider_budget import ProviderCallBudget


AgentStatus = Literal["created", "running", "waiting_user", "completed", "failed"]
ExecutionStrategyName = Literal["react", "plan_and_solve"]
PlanStatus = Literal["none", "active", "replanning", "completed", "failed"]


class AgentError(BaseModel):
    """Structured error recorded during an agent run."""

    message: str = Field(min_length=1)
    source: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def new_run_id() -> str:
    """Create a new run identifier."""

    return f"run_{uuid4().hex}"


def new_session_id() -> str:
    """Create a new session identifier."""

    return f"session_{uuid4().hex}"


class AgentState(BaseModel):
    """Single state carrier for one user request."""

    run_id: str = Field(default_factory=new_run_id)
    trace_id: str = Field(default_factory=new_trace_id)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request: UserRequest
    execution_strategy: ExecutionStrategyName = "react"

    memory_context: list[MemoryItem] = Field(default_factory=list)
    perception: PerceptionBundle | None = None
    intent: IntentResult | None = None
    plan: TaskPlan | None = None
    plan_status: PlanStatus = "none"
    current_step_id: str | None = None
    plan_revision_count: int = Field(default=0, ge=0)

    selected_tools: list[ToolSelection] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    provider_budget: ProviderCallBudget = Field(default_factory=ProviderCallBudget)

    response: AgentResponse | None = None
    errors: list[AgentError] = Field(default_factory=list)
    status: AgentStatus = "created"

    @classmethod
    def from_request(cls, request: UserRequest, run_id: str | None = None) -> "AgentState":
        """Create state from a normalized user request."""

        return cls(
            run_id=run_id or new_run_id(),
            user_id=request.user_id,
            session_id=request.session_id,
            request=request,
            execution_strategy=request.execution_strategy,
        )

    def add_tool_call(
        self,
        tool_name: str,
        input: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> ToolCallRecord:
        """Append a running tool call record and mark the run as active."""

        record = ToolCallRecord(
            call_id=call_id or f"call_{uuid4().hex}",
            tool_name=tool_name,
            input=input or {},
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        self.tool_calls.append(record)
        self.status = "running"
        return record

    def complete_tool_call(
        self,
        call_id: str,
        result: ToolResult,
        output_ref: str | None = None,
    ) -> ToolCallRecord:
        """Mark a tool call as succeeded and append its result."""

        record = self._get_tool_call(call_id)
        record.status = "succeeded"
        record.finished_at = datetime.now(timezone.utc)
        record.output_ref = output_ref or result.output_ref
        self.tool_results.append(result)
        self.status = "running"
        return record

    def fail_tool_call(
        self,
        call_id: str,
        error_message: str,
        result: ToolResult | None = None,
        error_details: dict[str, Any] | None = None,
        stop_run: bool = True,
    ) -> ToolCallRecord:
        """Mark a tool call as failed and record a structured error."""

        record = self._get_tool_call(call_id)
        record.status = "failed"
        record.finished_at = datetime.now(timezone.utc)
        record.error_message = error_message
        details = {"call_id": call_id}
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

    def set_intent(self, intent: IntentResult) -> None:
        """Set detected intent and mark the run as active."""

        self.intent = intent
        self.status = "running"

    def set_plan(self, plan: TaskPlan) -> None:
        """Set the current task plan."""

        self.plan = plan
        self.plan_status = "active"
        self.status = "waiting_user" if plan.requires_followup else "running"

    def set_response(self, response: AgentResponse) -> None:
        """Set final response and complete the run."""

        self.response = response
        self.status = "completed"

    def _get_tool_call(self, call_id: str) -> ToolCallRecord:
        for record in self.tool_calls:
            if record.call_id == call_id:
                return record
        raise ValueError(f"Tool call not found: {call_id}")
