from assistant_agent.agent import tool_scheduler
from assistant_agent.agent.action_validator import ActionValidationResult
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolExecutionPolicy, ToolSideEffectPolicy, ToolSpec
from assistant_agent.services import tool_call_boundary
from assistant_agent.services.context import tool_catalog
from assistant_agent.services.tool_policy import ToolPolicyInterpreter, ToolPolicyView
from assistant_agent.tools.registry import create_default_registry


def _accepted_validation() -> ActionValidationResult:
    return ActionValidationResult(
        accepted=True,
        code="accepted",
        message="Action accepted.",
    )


def _scheduled_call(
    tool_name: str,
    *,
    side_effect_level: str = "external_read",
    requires_confirmation: bool = False,
    dependency_mode: str = "independent",
    realtime_safety: str = "safe",
    concurrency_group: str | None = None,
    resource_reads: list[str] | None = None,
    resource_writes: list[str] | None = None,
) -> tool_scheduler.ScheduledToolCall:
    return tool_scheduler.ScheduledToolCall(
        call_index=0,
        decision=AssistantDecision(
            type="tool_call",
            tool_name=tool_name,
            tool_input={"query": "headphones"},
        ),
        validation=_accepted_validation(),
        side_effect_level=side_effect_level,
        requires_confirmation=requires_confirmation,
        dependency_mode=dependency_mode,
        realtime_safety=realtime_safety,
        concurrency_group=concurrency_group,
        resource_reads=tuple(resource_reads or ()),
        resource_writes=tuple(resource_writes or ()),
    )


def test_scheduler_builds_call_metadata_from_policy_view(monkeypatch) -> None:
    class FakePolicyInterpreter:
        def view_for_tool_name(self, tool_name: str) -> ToolPolicyView:
            assert tool_name == "product_search"
            return ToolPolicyView(
                tool_name=tool_name,
                side_effect_level="compensatable",
                risk_gate_level="soft_gate",
                requires_confirmation=True,
                dependency_mode="terminal",
                realtime_safety="needs_progress",
                concurrency_group="artifact",
                resource_reads=["prompt"],
                resource_writes=["artifact:image"],
            )

    monkeypatch.setattr(
        tool_scheduler,
        "ToolPolicyInterpreter",
        FakePolicyInterpreter,
        raising=False,
    )

    scheduled = tool_scheduler.build_scheduled_tool_call(
        call_index=0,
        decision=AssistantDecision(
            type="tool_call",
            tool_name="product_search",
            tool_input={"query": "headphones"},
        ),
        validation=_accepted_validation(),
        native_call_id="call-1",
    )

    assert scheduled.side_effect_level == "compensatable"
    assert scheduled.requires_confirmation is True
    assert scheduled.dependency_mode == "terminal"
    assert scheduled.realtime_safety == "needs_progress"
    assert scheduled.concurrency_group == "artifact"
    assert scheduled.resource_reads == ("prompt",)
    assert scheduled.resource_writes == ("artifact:image",)


def test_scheduler_policy_metadata_matches_default_interpreter_views() -> None:
    interpreter = ToolPolicyInterpreter()
    specs_by_name = {spec.name: spec for spec in create_default_registry().list_specs()}

    for tool_name in ("product_search", "web_search", "image_generation", "memory_save"):
        scheduled = tool_scheduler.build_scheduled_tool_call(
            call_index=0,
            decision=AssistantDecision(
                type="tool_call",
                tool_name=tool_name,
                tool_input={},
            ),
            validation=_accepted_validation(),
        )
        view = interpreter.view_for_spec(specs_by_name[tool_name])

        assert scheduled.side_effect_level == view.side_effect_level
        assert scheduled.requires_confirmation is view.requires_confirmation
        assert scheduled.dependency_mode == view.dependency_mode
        assert scheduled.realtime_safety == view.realtime_safety


def test_scheduler_parallelizes_only_independent_safe_read_only_calls() -> None:
    calls = [
        _scheduled_call("product_search"),
        _scheduled_call("web_search"),
    ]

    schedule = tool_scheduler.plan_tool_schedule(calls, remaining_tool_budget=5)

    assert schedule.reason == "read_only_independent"
    assert schedule.groups[0].mode == "parallel"
    metadata = schedule.to_metadata()
    assert metadata["dependency_modes"] == ["independent", "independent"]
    assert metadata["realtime_safety"] == ["safe", "safe"]
    assert metadata["concurrency_groups"] == [None, None]


def test_scheduler_serializes_requires_prior_observation_calls() -> None:
    calls = [
        _scheduled_call("product_search"),
        _scheduled_call("price_compare", dependency_mode="requires_prior_observation"),
    ]

    schedule = tool_scheduler.plan_tool_schedule(calls, remaining_tool_budget=5)

    assert schedule.reason == "requires_prior_observation"
    assert schedule.groups[0].mode == "serial"
    assert schedule.groups[0].to_metadata()["dependency_modes"] == [
        "independent",
        "requires_prior_observation",
    ]


def test_scheduler_serializes_terminal_confirmation_unsafe_and_resource_write_batches() -> None:
    cases = [
        (
            [
                _scheduled_call("product_search"),
                _scheduled_call("image_generation", dependency_mode="terminal"),
            ],
            "terminal_tool",
        ),
        (
            [
                _scheduled_call("product_search"),
                _scheduled_call("memory_save", realtime_safety="needs_confirmation"),
            ],
            "realtime_confirmation_required",
        ),
        (
            [
                _scheduled_call("product_search"),
                _scheduled_call("external_mutation", realtime_safety="unsafe"),
            ],
            "realtime_unsafe",
        ),
        (
            [
                _scheduled_call("product_search"),
                _scheduled_call("cache_refresh", resource_writes=["catalog"]),
            ],
            "resource_write_conflict",
        ),
        (
            [
                _scheduled_call("catalog_lookup", concurrency_group="catalog"),
                _scheduled_call("catalog_status", concurrency_group="catalog"),
            ],
            "concurrency_group_conflict",
        ),
    ]

    for calls, expected_reason in cases:
        schedule = tool_scheduler.plan_tool_schedule(calls, remaining_tool_budget=5)

        assert schedule.reason == expected_reason
        assert schedule.groups[0].mode == "serial"


def test_pre_tool_call_boundary_summary_uses_policy_view(monkeypatch) -> None:
    class FakePolicyInterpreter:
        def view_for_tool_name(self, tool_name: str) -> ToolPolicyView:
            assert tool_name == "product_search"
            return ToolPolicyView(
                tool_name=tool_name,
                side_effect_level="compensatable",
                risk_gate_level="soft_gate",
                requires_confirmation=True,
                confirmation_kind="verbal_confirmation",
                description="Fake policy view for boundary wiring.",
                compensation_hint="Report that the generated artifact already exists.",
            )

    monkeypatch.setattr(
        tool_call_boundary,
        "ToolPolicyInterpreter",
        FakePolicyInterpreter,
        raising=False,
    )
    request = UserRequest(user_id="u1", session_id="s1", text="hello")
    state = AgentState.from_request(request, run_id="run-1")

    summary = tool_call_boundary.build_pre_tool_call_summary(
        tool_name="product_search",
        tool_input={"query": "headphones"},
        registry=create_default_registry(),
        request=request,
        state=state,
    )

    assert summary["side_effect"]["level"] == "compensatable"
    assert summary["side_effect"]["requires_confirmation"] is True
    assert summary["side_effect"]["confirmation_kind"] == "verbal_confirmation"
    assert summary["confirmation"] == {
        "required": True,
        "kind": "verbal_confirmation",
    }


def test_prompt_tool_spec_payload_compacts_side_effect_from_policy_view(monkeypatch) -> None:
    class FakePolicyInterpreter:
        def view_for_spec(self, spec: ToolSpec) -> ToolPolicyView:
            assert spec.name == "calendar.create_event"
            return ToolPolicyView(
                tool_name=spec.name,
                side_effect_level="committed",
                risk_gate_level="hard_gate",
                requires_confirmation=True,
                confirmation_kind="calendar_write",
                dependency_mode="terminal",
                realtime_safety="needs_confirmation",
                resource_writes=["calendar.events"],
            )

    monkeypatch.setattr(
        tool_catalog,
        "ToolPolicyInterpreter",
        FakePolicyInterpreter,
        raising=False,
    )
    spec = ToolSpec(
        name="calendar.create_event",
        side_effect=ToolSideEffectPolicy(
            level="external_read",
            requires_confirmation=False,
            description="Original spec should not drive this compact payload.",
        ),
    )

    payload = tool_catalog.prompt_tool_spec_payload(spec)

    assert payload["side_effect"] == {
        "level": "committed",
        "requires_confirmation": True,
        "confirmation_kind": "calendar_write",
    }
    assert payload["execution"] == {
        "dependency_mode": "terminal",
        "realtime_safety": "needs_confirmation",
        "resource_writes": ["calendar.events"],
    }


def test_prompt_tool_spec_payload_omits_execution_for_read_only_independent_tools() -> None:
    spec = ToolSpec(
        name="web_search",
        side_effect=ToolSideEffectPolicy(
            level="external_read",
            requires_confirmation=False,
            description="Reads web results.",
        ),
        execution=ToolExecutionPolicy(
            dependency_mode="independent",
            resource_reads=["web"],
            realtime_safety="safe",
        ),
    )

    payload = tool_catalog.prompt_tool_spec_payload(spec)

    assert "side_effect" not in payload
    assert "execution" not in payload
