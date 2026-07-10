from assistant_agent.agent import tool_scheduler
from assistant_agent.agent.action_validator import ActionValidationResult
from assistant_agent.schemas.assistant_decision import AssistantDecision
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
