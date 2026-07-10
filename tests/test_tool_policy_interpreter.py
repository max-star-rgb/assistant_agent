from assistant_agent.schemas.tools import ToolSideEffectPolicy, ToolSpec
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.services.tool_risk_gate import risk_gate_level_for_policy
from assistant_agent.tools.registry import create_default_registry


def test_policy_interpreter_matches_existing_default_tool_risk_rules() -> None:
    interpreter = ToolPolicyInterpreter()

    for spec in create_default_registry().list_specs():
        view = interpreter.view_for_spec(spec)

        assert view.tool_name == spec.name
        assert view.side_effect_level == spec.side_effect.level
        assert view.requires_confirmation is spec.side_effect.requires_confirmation
        assert view.confirmation_kind == spec.side_effect.confirmation_kind
        assert view.risk_gate_level == risk_gate_level_for_policy(spec.side_effect)
        assert view.idempotency_required is (view.risk_gate_level == "soft_gate")
        assert view.auto_executable is (view.risk_gate_level == "auto")


def test_policy_interpreter_keeps_unknown_tool_conservative() -> None:
    view = ToolPolicyInterpreter().view_for_tool_name("custom_notification")

    assert view.tool_name == "custom_notification"
    assert view.side_effect_level == "pending_confirmation"
    assert view.risk_gate_level == "hard_gate"
    assert view.requires_confirmation is True
    assert view.confirmation_owner == "runtime"
    assert view.auto_executable is False
    assert view.idempotency_required is False


def test_policy_interpreter_preserves_tool_owned_confirmation_behavior() -> None:
    view = ToolPolicyInterpreter().view_for_tool_name("memory_save")

    assert view.risk_gate_level == "hard_gate"
    assert view.requires_confirmation is True
    assert view.confirmation_kind == "memory_write"
    assert view.confirmation_owner == "tool"
    assert view.tool_owned_confirmation is True


def test_policy_interpreter_accepts_explicit_spec_without_registry_lookup() -> None:
    spec = ToolSpec(
        name="calendar.search_events",
        side_effect=ToolSideEffectPolicy(
            level="external_read",
            requires_confirmation=False,
            description="Reads calendar events without writing.",
        ),
    )

    view = ToolPolicyInterpreter().view_for_spec(spec)

    assert view.tool_name == "calendar.search_events"
    assert view.side_effect_level == "external_read"
    assert view.risk_gate_level == "auto"
    assert view.auto_executable is True
