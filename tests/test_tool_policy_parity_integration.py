from assistant_agent.agent import tool_scheduler
from assistant_agent.agent.action_validator import ActionValidationResult
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSideEffectPolicy, ToolSpec
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


def test_scheduler_builds_call_metadata_from_policy_view(monkeypatch) -> None:
    class FakePolicyInterpreter:
        def view_for_tool_name(self, tool_name: str) -> ToolPolicyView:
            assert tool_name == "product_search"
            return ToolPolicyView(
                tool_name=tool_name,
                side_effect_level="compensatable",
                risk_gate_level="soft_gate",
                requires_confirmation=True,
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
