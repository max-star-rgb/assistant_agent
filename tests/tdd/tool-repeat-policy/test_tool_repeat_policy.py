from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

from assistant_agent.context.service import AssistantDecisionContext
from assistant_agent.mcp.adapter import MCPToolAdapter, MCPToolDefinition
from assistant_agent.mcp.config import MCPToolAdapterConfig
from assistant_agent.runtime.assistant_loop_nodes import (
    AssistantLoopState,
    _apply_decision_guards,
    _execute_single_requested_tool_node,
    execute_requested_tool_node,
)
from assistant_agent.runtime.loop_guard import LoopGuard
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.decorators import tool
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    CalendarCreateTool,
    CalendarSearchTool,
    ContactsSearchTool,
)
from assistant_agent.tools.plugins.builtin.email_access.tools import (
    EmailReadTool,
    EmailSearchTool,
)
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    ImageGenerationTool,
)
from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool
from assistant_agent.tools.plugins.builtin.local_file_access.tool import LocalFileReadTool
from assistant_agent.tools.plugins.builtin.lodging.tool import LodgingSearchTool
from assistant_agent.tools.plugins.builtin.lodging.watch_tool import (
    HotelPriceWatchCreateTool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
    MediaInspectTool,
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.video_branch import (
    VideoUnderstandingBranch,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    VisualMemorySearchTool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_reminder_tool import (
    VisualReminderManageTool,
)
from assistant_agent.tools.plugins.builtin.python_execution.tool import (
    PythonInterpreterTool,
)
from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool
from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    LoadSkillReferenceTool,
    LoadSkillTool,
)
from assistant_agent.tools.plugins.builtin.visual_image_search.tool import (
    VisualImageSearchTool,
)
from assistant_agent.tools.plugins.builtin.web_access.fetch_tool import WebFetchTool
from assistant_agent.tools.plugins.builtin.web_access.search_tool import WebSearchTool
from assistant_agent.tools.plugins.builtin.website_guidance.tools import (
    WebPageExploreTool,
    WebPageInspectTool,
)
from assistant_agent.tools.registry import ToolRegistry


class _ProbeInput(BaseModel):
    query: str


class _ProbeOutput(BaseModel):
    value: str


class _DefaultProbeTool(ToolBase):
    name = "default_probe"
    description = "Default repeat policy probe."
    input_schema = _ProbeInput
    output_schema = _ProbeOutput
    category = "read"

    def _run(self, input: _ProbeInput, context: object) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.query},
        )


class _RepeatableProbeTool(_DefaultProbeTool):
    name = "repeatable_probe"
    repeat_policy = "distinct_inputs"


class _RepeatableWriteProbeTool(_RepeatableProbeTool):
    name = "repeatable_write_probe"
    category = "write"

    def __init__(self) -> None:
        self.executed_queries: list[str] = []

    def _run(self, input: _ProbeInput, context: object) -> ToolResult:
        self.executed_queries.append(input.query)
        return super()._run(input, context)


class _ImageGenerationProbeTool(_DefaultProbeTool):
    name = "image_generation"
    category = "generate"
    repeat_policy = "once_per_run"


def test_mcp_read_and_write_definitions_project_matching_repeat_policy() -> None:
    """Catches MCP read/write classification being dropped from direct specs."""

    config = MCPToolAdapterConfig(
        server_name="server-sentinel",
        allowed_tools=["lookup", "mutate"],
        read_only_tools=["lookup"],
    )
    adapter = MCPToolAdapter(config)

    read_spec = adapter.tool_spec_for_definition(MCPToolDefinition(name="lookup"))
    write_spec = adapter.tool_spec_for_definition(MCPToolDefinition(name="mutate"))

    assert read_spec is not None
    assert write_spec is not None
    assert read_spec.repeat_policy == "distinct_inputs"
    assert write_spec.repeat_policy == "once_per_run"


def test_mcp_proxy_registry_uses_the_same_repeat_policy_mapping() -> None:
    """Catches MCP proxy registration disagreeing with direct spec projection."""

    config = MCPToolAdapterConfig(
        server_name="server-sentinel",
        allowed_tools=["lookup", "mutate"],
        read_only_tools=["lookup"],
    )
    adapter = MCPToolAdapter(config, runner=cast(Any, object()))
    registry = ToolRegistry()
    registry.register(
        adapter.proxy_tool_for_definition(MCPToolDefinition(name="lookup"))
    )
    registry.register(
        adapter.proxy_tool_for_definition(MCPToolDefinition(name="mutate"))
    )

    assert (
        registry.get_spec("mcp.server-sentinel.lookup").repeat_policy
        == "distinct_inputs"
    )
    assert (
        registry.get_spec("mcp.server-sentinel.mutate").repeat_policy
        == "once_per_run"
    )


def test_decorated_tool_projects_an_explicit_repeat_policy() -> None:
    """Catches the local decorator preventing tools from choosing a policy."""

    @tool(
        name="decorated_repeatable_probe",
        input_schema=_ProbeInput,
        category="read",
        repeat_policy="distinct_inputs",
    )
    def decorated_probe(input: _ProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name="decorated_repeatable_probe",
            success=True,
            data={"value": input.query},
        )

    registry = ToolRegistry()
    registry.register(decorated_probe)

    assert (
        registry.get_spec("decorated_repeatable_probe").repeat_policy
        == "distinct_inputs"
    )


@pytest.mark.parametrize(
    ("tool_type", "expected"),
    [
        (CalendarSearchTool, "distinct_inputs"),
        (CalendarCreateTool, "distinct_inputs"),
        (ContactsSearchTool, "distinct_inputs"),
        (EmailSearchTool, "distinct_inputs"),
        (EmailReadTool, "distinct_inputs"),
        (LocalFileReadTool, "distinct_inputs"),
        (WebSearchTool, "distinct_inputs"),
        (WebFetchTool, "distinct_inputs"),
        (LodgingSearchTool, "distinct_inputs"),
        (HotelPriceWatchCreateTool, "distinct_inputs"),
        (ShoppingSearchTool, "distinct_inputs"),
        (VisualImageSearchTool, "distinct_inputs"),
        (MediaInspectTool, "distinct_inputs"),
        (LiveViewInspectTool, "distinct_inputs"),
        (RealtimeVideoObserveTool, "distinct_inputs"),
        (VideoUnderstandingBranch, "distinct_inputs"),
        (VisualMemorySearchTool, "distinct_inputs"),
        (VisualReminderManageTool, "distinct_inputs"),
        (LoadSkillTool, "distinct_inputs"),
        (LoadSkillReferenceTool, "distinct_inputs"),
        (WebPageInspectTool, "distinct_inputs"),
        (PythonInterpreterTool, "distinct_inputs"),
        (WebPageExploreTool, "distinct_inputs"),
        (ImageGenerationTool, "once_per_run"),
        (ImageTo3DTool, "once_per_run"),
    ],
)
def test_builtin_registry_projects_the_approved_repeat_policy(
    tool_type: type[ToolBase],
    expected: str,
) -> None:
    """Catches a builtin being assigned to the wrong repeat behavior."""

    registry = ToolRegistry()
    registry.register(tool_type.__new__(tool_type))

    assert registry.get_spec(tool_type.name).repeat_policy == expected


def test_registry_projects_default_and_explicit_repeat_policy() -> None:
    """Catches Registry dropping a Tool's repeat policy declaration."""

    registry = ToolRegistry()
    registry.register(_DefaultProbeTool())
    registry.register(_RepeatableProbeTool())

    assert registry.get_spec("default_probe").repeat_policy == "once_per_run"
    assert registry.get_spec("repeatable_probe").repeat_policy == "distinct_inputs"


def _successful_state(tool_name: str, tool_input: dict[str, object]) -> AgentState:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="continue",
    )
    state = AgentState.from_request(request)
    record = state.add_tool_call(tool_name, tool_input)
    state.complete_tool_call(
        record.tool_call_id,
        ToolResult(tool_name=tool_name, success=True, data={"value": "ok"}),
    )
    LoopGuard(state.request.metadata).record_complete_tool_success(
        tool_name=tool_name,
        tool_input=tool_input,
    )
    return state


def _guarded_decision(
    state: AgentState,
    registry: ToolRegistry,
    *,
    tool_name: str,
    tool_input: dict[str, object],
) -> AssistantToolCall:
    graph_state = cast(
        AssistantLoopState,
        {
            "request": state.request,
            "state": state,
            "tool_executor": SimpleNamespace(registry=registry),
            "outputs_by_step": {},
            "current_step_index": 0,
        },
    )
    return cast(
        AssistantToolCall,
        _apply_decision_guards(
            graph_state,
            AssistantToolCall(tool_name=tool_name, tool_input=tool_input),
            cast(AssistantDecisionContext, None),
        ),
    )


def test_default_tool_blocks_a_second_successful_call_with_different_input() -> None:
    """Catches the default policy accidentally allowing unbounded distinct calls."""

    registry = ToolRegistry()
    registry.register(_DefaultProbeTool())
    state = _successful_state("default_probe", {"query": "first"})

    decision = _guarded_decision(
        state,
        registry,
        tool_name="default_probe",
        tool_input={"query": "second"},
    )

    assert "tool_repeat_limit_reached" in decision.safety_notes


def test_repeatable_tool_allows_a_second_call_with_different_input() -> None:
    """Catches distinct-input opt-in being ignored by the Runtime guard."""

    registry = ToolRegistry()
    registry.register(_RepeatableProbeTool())
    state = _successful_state("repeatable_probe", {"query": "first"})

    decision = _guarded_decision(
        state,
        registry,
        tool_name="repeatable_probe",
        tool_input={"query": "second"},
    )

    assert "tool_repeat_limit_reached" not in decision.safety_notes
    assert "duplicate_complete_tool_call" not in decision.safety_notes


def test_repeatable_tool_still_reuses_an_identical_complete_call() -> None:
    """Catches repeat opt-in bypassing the existing same-input deduplication guard."""

    registry = ToolRegistry()
    registry.register(_RepeatableProbeTool())
    state = _successful_state("repeatable_probe", {"query": "same"})

    decision = _guarded_decision(
        state,
        registry,
        tool_name="repeatable_probe",
        tool_input={"query": "same"},
    )

    assert "duplicate_complete_tool_call" in decision.safety_notes


def test_shopping_search_opts_into_distinct_input_repeats() -> None:
    """Catches reintroducing the shopping-specific once-per-run hard code."""

    registry = ToolRegistry()
    registry.register(
        ShoppingSearchTool(
            search_adapter=cast(Any, object()),
            compare_adapter=cast(Any, object()),
        )
    )
    state = _successful_state(
        "shopping_search",
        {"needs": [{"keyword": "蓝牙耳机"}]},
    )

    decision = _guarded_decision(
        state,
        registry,
        tool_name="shopping_search",
        tool_input={"needs": [{"keyword": "降噪蓝牙耳机"}]},
    )

    assert "tool_repeat_limit_reached" not in decision.safety_notes
    assert "run_tool_call_limit_reached" not in decision.safety_notes


def test_execution_boundary_blocks_unguarded_second_default_call() -> None:
    """Catches a later native batch call bypassing the decision-level repeat guard."""

    registry = ToolRegistry()
    registry.register(_DefaultProbeTool())
    state = _successful_state("default_probe", {"query": "first"})
    decision = AssistantToolCall(
        tool_name="default_probe",
        tool_input={"query": "second"},
    )

    executed = _execute_single_requested_tool_node(
        cast(
            AssistantLoopState,
            {
                "request": state.request,
                "state": state,
                "tool_executor": ToolExecutor(registry=registry),
                "outputs_by_step": {},
                "current_step_index": 0,
                "assistant_output": decision,
                "tool_observations": [],
            },
        )
    )

    assert len(state.tool_calls) == 1
    assert executed["tool_observations"][-1]["error"]["code"] == (
        "tool_repeat_limit_reached"
    )
    assert executed["run_phase"].value == "finalize"


def _execute_write_probe_batch(
    tool: _RepeatableWriteProbeTool,
    queries: list[str],
) -> AssistantLoopState:
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="run write probes",
        )
    )
    return execute_requested_tool_node(
        cast(
            AssistantLoopState,
            {
                "request": state.request,
                "state": state,
                "tool_executor": ToolExecutor(registry=registry),
                "outputs_by_step": {},
                "current_step_index": 0,
                "pending_tool_calls": [
                    AssistantToolCall(
                        tool_name=tool.name,
                        tool_input={"query": query},
                    )
                    for query in queries
                ],
                "tool_observations": [],
                "max_tool_iterations": len(queries),
            },
        )
    )


def test_distinct_input_write_tool_blocks_identical_calls_at_execution_boundary() -> None:
    """Catches native batches re-running a successful identical write call."""

    tool = _RepeatableWriteProbeTool()

    executed = _execute_write_probe_batch(tool, ["same", "same"])

    assert tool.executed_queries == ["same"]
    assert (
        executed["tool_observations"][-1]["error"]["code"]
        == "duplicate_complete_tool_call"
    )


def test_distinct_input_write_tool_allows_different_calls_in_one_batch() -> None:
    """Catches distinct-input write tools being narrowed to once per run."""

    tool = _RepeatableWriteProbeTool()

    _execute_write_probe_batch(tool, ["first", "second"])

    assert tool.executed_queries == ["first", "second"]


def test_image_generation_uses_the_generic_once_per_run_guard() -> None:
    """Catches the image-generation name bypassing ToolSpec repeat policy."""

    registry = ToolRegistry()
    registry.register(_ImageGenerationProbeTool())
    state = _successful_state("image_generation", {"query": "first"})
    state.request.metadata["assistant_loop_guard"]["succeeded_terminal_tools"] = [
        "image_generation"
    ]

    decision = _guarded_decision(
        state,
        registry,
        tool_name="image_generation",
        tool_input={"query": "second"},
    )

    assert isinstance(decision, AssistantToolCall)
    assert "tool_repeat_limit_reached" in decision.safety_notes
    assert "duplicate_terminal_tool" not in decision.safety_notes


def test_repeatable_tool_still_obeys_the_global_tool_call_budget() -> None:
    """Catches distinct-input opt-in bypassing max_tool_iterations."""

    registry = ToolRegistry()
    registry.register(_RepeatableProbeTool())
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="search twice",
        )
    )

    executed = execute_requested_tool_node(
        cast(
            AssistantLoopState,
            {
                "request": state.request,
                "state": state,
                "tool_executor": ToolExecutor(registry=registry),
                "outputs_by_step": {},
                "current_step_index": 0,
                "pending_tool_calls": [
                    AssistantToolCall(
                        tool_name="repeatable_probe",
                        tool_input={"query": "first"},
                    ),
                    AssistantToolCall(
                        tool_name="repeatable_probe",
                        tool_input={"query": "second"},
                    ),
                ],
                "tool_observations": [],
                "max_tool_iterations": 1,
            },
        )
    )

    assert len(state.tool_calls) == 1
    assert executed["tool_calls_used"] == 1
    assert state.request.metadata["tool_calls_skipped_for_budget"] == 1
    assert executed["run_phase"].value == "finalize"
