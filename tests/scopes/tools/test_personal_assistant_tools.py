from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.services.tool_risk_gate import InMemoryToolIdempotencyLedger
from assistant_agent.tools.personal_assistant_tools import (
    CalendarCreateTool,
    ReminderCreateTool,
)
from assistant_agent.tools.registry import ToolRegistry, create_default_registry
from assistant_agent.services.personal_assistant_adapters import (
    MockCalendarAdapter,
    MockReminderAdapter,
)


def test_default_registry_registers_personal_assistant_tools_with_governance() -> None:
    registry = create_default_registry()
    names = registry.list()

    assert "weather" in names
    assert "calendar_search" in names
    assert "calendar_create" in names
    assert "reminder_create" in names
    assert "contacts_search" in names

    views = {
        spec.name: ToolPolicyInterpreter().view_for_spec(spec)
        for spec in registry.list_specs()
        if spec.name
        in {
            "weather",
            "calendar_search",
            "calendar_create",
            "reminder_create",
            "contacts_search",
        }
    }

    assert views["weather"].side_effect_level == "external_read"
    assert views["weather"].auto_executable is True
    assert views["weather"].dependency_mode == "independent"
    assert views["calendar_search"].reads_private_data is True
    assert views["contacts_search"].reads_private_data is True
    assert views["calendar_create"].risk_gate_level == "hard_gate"
    assert views["calendar_create"].requires_confirmation is True
    assert views["calendar_create"].idempotency_required is True
    assert views["reminder_create"].risk_gate_level == "hard_gate"
    assert views["reminder_create"].requires_confirmation is True
    assert views["reminder_create"].idempotency_required is True


def test_weather_default_tool_returns_prompt_safe_mock_observation() -> None:
    state = _state("上海天气怎么样")

    result = ToolExecutor(registry=create_default_registry()).run_tool(
        state,
        "step-1",
        "weather",
        {"location": "Shanghai", "days": 1},
    )
    observation = observation_from_tool_result(result)

    assert result.success is True
    assert result.data["provider"] == "mock"
    assert observation.summary == "Weather for Shanghai: clear, 26 C."
    assert observation.structured_output["forecast"][0]["temperature_c"] == 26
    assert "raw" not in str(observation).lower()


def test_contacts_search_default_tool_returns_redacted_candidates() -> None:
    state = _state("找一下 Alex 的联系方式")

    result = ToolExecutor(registry=create_default_registry()).run_tool(
        state,
        "step-1",
        "contacts_search",
        {"query": "Alex"},
    )
    observation = observation_from_tool_result(result)

    assert result.success is True
    assert observation.structured_output["contacts"][0]["display_name"] == "Alex Chen"
    assert "raw-private-contact-token" not in str(observation)
    assert result.raw_data_ref == "mock://contacts/alex"


def test_calendar_create_requires_runtime_confirmation_not_model_flag() -> None:
    adapter = MockCalendarAdapter()
    registry = ToolRegistry()
    registry.register(CalendarCreateTool(adapter=adapter))

    result = ToolExecutor(registry=registry).run_tool(
        _state("帮我创建一个明天上午十点的团队同步", metadata={}),
        "step-1",
        "calendar_create",
        {
            "title": "Team sync",
            "start_time": "2026-07-21T10:00:00+08:00",
            "confirmed": True,
            "idempotency_key": "calendar-event-1",
        },
    )

    assert result.success is True
    assert result.data["requires_confirmation"] is True
    assert result.data["risk_gate"]["reason"] == "confirmation_required"
    assert result.output_ref == "local://tool-confirmations/calendar_create"
    assert adapter.created_event_titles == []


def test_calendar_create_confirmed_write_is_idempotent() -> None:
    adapter = MockCalendarAdapter()
    registry = ToolRegistry()
    registry.register(CalendarCreateTool(adapter=adapter))
    executor = ToolExecutor(
        registry=registry,
        idempotency_ledger=InMemoryToolIdempotencyLedger(),
    )
    tool_input = {
        "title": "Team sync",
        "start_time": "2026-07-21T10:00:00+08:00",
        "idempotency_key": "calendar-event-1",
    }

    first = executor.run_tool(
        _state("创建团队同步", metadata=_confirmed_metadata("calendar_create")),
        "step-1",
        "calendar_create",
        tool_input,
    )
    second = executor.run_tool(
        _state("创建团队同步", metadata=_confirmed_metadata("calendar_create")),
        "step-1",
        "calendar_create",
        tool_input,
    )

    assert first.success is True
    assert first.output_ref == "mock://calendar/events/calendar-event-1"
    assert first.data["side_effect_level"] == "committed"
    assert second.data["status"] == "duplicate_suppressed"
    assert second.output_ref == first.output_ref
    assert adapter.created_event_titles == ["Team sync"]


def test_calendar_create_confirmation_still_requires_idempotency_key() -> None:
    adapter = MockCalendarAdapter()
    registry = ToolRegistry()
    registry.register(CalendarCreateTool(adapter=adapter))

    result = ToolExecutor(registry=registry).run_tool(
        _state("创建团队同步", metadata=_confirmed_metadata("calendar_create")),
        "step-1",
        "calendar_create",
        {
            "title": "Team sync",
            "start_time": "2026-07-21T10:00:00+08:00",
        },
    )

    assert result.success is True
    assert result.data["requires_confirmation"] is True
    assert result.data["risk_gate"]["reason"] == "idempotency_key_required_after_confirmation"
    assert adapter.created_event_titles == []


def test_reminder_create_requires_confirmation_before_mutating_todo_state() -> None:
    adapter = MockReminderAdapter()
    registry = ToolRegistry()
    registry.register(ReminderCreateTool(adapter=adapter))

    result = ToolExecutor(registry=registry).run_tool(
        _state("提醒我下午给客户回电话"),
        "step-1",
        "reminder_create",
        {"title": "Call customer", "idempotency_key": "reminder-1"},
    )

    assert result.success is True
    assert result.data["requires_confirmation"] is True
    assert adapter.created_titles == []


def _state(text: str, *, metadata: dict[str, object] | None = None) -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text=text,
            metadata=metadata or {},
        ),
        run_id="run-1",
    )


def _confirmed_metadata(tool_name: str) -> dict[str, object]:
    return {
        "tool_risk_gate_enabled": True,
        "tool_confirmation": {
            "tool_name": tool_name,
            "confirmed": True,
            "confirmed_by": "user",
        },
    }
