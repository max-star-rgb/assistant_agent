import importlib
from pathlib import Path

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.tool_risk_gate import InMemoryToolIdempotencyLedger
from assistant_agent.tools.loader import load_local_tools, register_local_tools
from assistant_agent.tools.registry import ToolRegistry


def test_calendar_create_event_requires_confirmation_before_external_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_calendar_write_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("calendar_write_tools")
    registry = _registry_from_module()
    state = _state(metadata={"realtime": {"run_id": "run-1"}})

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        "calendar.create_event",
        {"title": "Team sync", "start_time": "2026-07-10T10:00:00+08:00"},
    )

    assert result.success is True
    assert result.data["requires_confirmation"] is True
    assert result.data["risk_gate"]["reason"] == "confirmation_required"
    assert result.output_ref == "local://tool-confirmations/calendar.create_event"
    assert module.CALLS == []


def test_calendar_create_event_confirmation_requires_idempotency_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_calendar_write_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("calendar_write_tools")
    registry = _registry_from_module()
    state = _state(metadata=_confirmed_metadata())

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        "calendar.create_event",
        {"title": "Team sync", "start_time": "2026-07-10T10:00:00+08:00"},
    )

    assert result.success is True
    assert result.data["requires_confirmation"] is True
    assert result.data["risk_gate"]["reason"] == "idempotency_key_required_after_confirmation"
    assert module.CALLS == []


def test_calendar_create_event_confirmed_write_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_calendar_write_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("calendar_write_tools")
    registry = _registry_from_module()
    ledger = InMemoryToolIdempotencyLedger()
    executor = ToolExecutor(registry=registry, idempotency_ledger=ledger)
    tool_input = {
        "title": "Team sync",
        "start_time": "2026-07-10T10:00:00+08:00",
        "idempotency_key": "calendar-event-1",
    }

    first = executor.run_tool(
        _state(metadata=_confirmed_metadata()),
        "step-1",
        "calendar.create_event",
        tool_input,
    )
    second = executor.run_tool(
        _state(metadata=_confirmed_metadata()),
        "step-1",
        "calendar.create_event",
        tool_input,
    )

    assert first.success is True
    assert first.output_ref == "calendar://events/calendar-event-1"
    assert first.data["side_effect_level"] == "committed"
    assert second.success is True
    assert second.data["status"] == "duplicate_suppressed"
    assert second.output_ref == first.output_ref
    assert module.CALLS == ["Team sync"]


def _registry_from_module() -> ToolRegistry:
    result = load_local_tools(["calendar_write_tools"])
    registry = ToolRegistry()
    register_local_tools(registry, result.tools)
    return registry


def _state(*, metadata: dict[str, object]) -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我创建一个日历事件",
            metadata=metadata,
        ),
        run_id="run-1",
    )


def _confirmed_metadata() -> dict[str, object]:
    return {
        "tool_risk_gate_enabled": True,
        "tool_confirmation": {
            "tool_name": "calendar.create_event",
            "confirmed": True,
            "confirmed_by": "user",
        },
    }


def _write_calendar_write_module(root: Path) -> None:
    (root / "calendar_write_tools.py").write_text(
        '''
from pydantic import BaseModel, Field

from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    DataPolicy,
    ExecutionPolicy,
    RealtimeToolPolicy,
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
    VisibilityPolicy,
)
from assistant_agent.tools.decorators import tool


CALLS = []


class CalendarCreateInput(BaseModel):
    title: str = Field(min_length=1)
    start_time: str = Field(min_length=1)
    idempotency_key: str | None = None


@tool(
    name="calendar.create_event",
    description="Create a calendar event after explicit user confirmation.",
    input_schema=CalendarCreateInput,
    execution=ToolExecutionPolicy(
        dependency_mode="terminal",
        resource_reads=["calendar.events"],
        resource_writes=["calendar.events"],
        realtime_safety="needs_confirmation",
    ),
    policy=ToolPolicyMetadata(
        risk="external_write",
        realtime=RealtimeToolPolicy(mode="confirm_then_execute", interruptible=False, commit_boundary="external_commit"),
        approval=ApprovalPolicy(mode="always", confirmation_kind="verbal"),
        execution=ExecutionPolicy(timeout_s=8, idempotency="required", retry_count=0),
        data=DataPolicy(
            reads_private_data=True,
            writes_private_data=True,
            sends_data_external=True,
            redact_in_trace=True,
        ),
        visibility=VisibilityPolicy(toolset="personal.calendar", enabled_by_default=False, skill_only=True),
    ),
)
def calendar_create_event(input, context):
    CALLS.append(input.title)
    key = input.idempotency_key or "missing"
    return ToolResult(
        tool_name="calendar.create_event",
        success=True,
        data={
            "summary": f"Created calendar event: {input.title}",
            "side_effect_level": "committed",
            "idempotency": {"key": key, "present": True, "required": True},
        },
        voice_summary=f"已创建日历事件：{input.title}",
        trace_summary={"summary": "Calendar event created.", "idempotency_key_present": True},
        audit_payload={"confirmed": True, "redacted": True},
        output_ref=f"calendar://events/{key}",
    )


__assistant_tools__ = [calendar_create_event]
'''.lstrip(),
        encoding="utf-8",
    )
