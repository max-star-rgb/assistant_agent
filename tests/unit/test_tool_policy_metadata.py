from pydantic import BaseModel

from assistant_agent.schemas.tool_spec_adapters import (
    tool_spec_to_mcp_tool,
    tool_spec_to_openai_tool,
)
from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    DataPolicy,
    ExecutionPolicy,
    RealtimeToolPolicy,
    ToolPolicyMetadata,
    ToolResult,
    ToolSideEffectPolicy,
    ToolSpec,
    VisibilityPolicy,
)
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


class MetadataInput(BaseModel):
    query: str


class MetadataTool(MockTool):
    name = "calendar.search_events"
    description = "Search calendar events."
    input_schema = MetadataInput
    output_schema = MetadataInput
    policy = ToolPolicyMetadata(
        risk="external_read",
        realtime=RealtimeToolPolicy(mode="blocking", interruptible=True),
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=4, max_result_chars=800),
        data=DataPolicy(reads_private_data=True, redact_in_trace=True),
        visibility=VisibilityPolicy(
            toolset="personal.calendar",
            tags=["calendar", "search"],
            enabled_by_default=False,
        ),
    )

    def _run(self, input: MetadataInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"events": []})


def test_tool_spec_accepts_policy_metadata_without_replacing_side_effect() -> None:
    spec = ToolSpec(
        name="calendar.create_event",
        side_effect=ToolSideEffectPolicy(
            level="external_read",
            requires_confirmation=False,
        ),
        policy=ToolPolicyMetadata(
            risk="external_write",
            realtime=RealtimeToolPolicy(mode="confirm_then_execute"),
            approval=ApprovalPolicy(mode="always", confirmation_kind="calendar_write"),
            execution=ExecutionPolicy(timeout_s=8, idempotency="required"),
            data=DataPolicy(
                reads_private_data=True,
                writes_private_data=True,
                sends_data_external=True,
                redact_in_trace=True,
            ),
            visibility=VisibilityPolicy(toolset="personal.calendar", skill_only=True),
        ),
    )

    view = ToolPolicyInterpreter().view_for_spec(spec)

    assert spec.side_effect.level == "external_read"
    assert view.side_effect_level == "committed"
    assert view.risk_gate_level == "hard_gate"
    assert view.requires_confirmation is True
    assert view.confirmation_kind == "calendar_write"
    assert view.realtime_mode == "confirm_then_execute"
    assert view.idempotency_required is True
    assert view.reads_private_data is True
    assert view.writes_private_data is True
    assert view.sends_data_external is True
    assert view.redact_in_trace is True
    assert view.toolset == "personal.calendar"
    assert view.skill_only is True


def test_registry_copies_tool_policy_metadata_to_specs() -> None:
    registry = ToolRegistry()
    registry.register(MetadataTool())

    spec = registry.list_specs()[0]
    view = ToolPolicyInterpreter().view_for_spec(spec)

    assert spec.policy is not None
    assert view.side_effect_level == "external_read"
    assert view.realtime_mode == "blocking"
    assert view.timeout_s == 4
    assert view.max_result_chars == 800
    assert view.reads_private_data is True
    assert view.toolset == "personal.calendar"
    assert view.enabled_by_default is False
    assert view.tags == ["calendar", "search"]


def test_default_registry_policy_views_still_fall_back_to_side_effect() -> None:
    for spec in create_default_registry().list_specs():
        view = ToolPolicyInterpreter().view_for_spec(spec)

        assert view.side_effect_level == spec.side_effect.level
        assert view.requires_confirmation is spec.side_effect.requires_confirmation


def test_provider_and_mcp_adapters_do_not_expose_internal_policy_metadata() -> None:
    spec = ToolSpec(
        name="calendar.search_events",
        description="Search calendar events.",
        input_schema={"fields": {"query": {"type": "string", "required": True}}},
        policy=MetadataTool.policy,
    )

    payload = {
        "openai": tool_spec_to_openai_tool(spec),
        "mcp": tool_spec_to_mcp_tool(spec),
    }
    text = str(payload)

    assert "reads_private_data" not in text
    assert "writes_private_data" not in text
    assert "sends_data_external" not in text
    assert "redact_in_trace" not in text
    assert "skill_only" not in text
