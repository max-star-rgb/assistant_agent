"""Focused offline checks for stable tool-governance behavior."""

from typing import ClassVar

import pytest
from pydantic import BaseModel, Field, model_validator

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.legacy_tool_mapping import (
    canonical_action_for_capability,
    canonical_capability_for_action,
    canonical_capability_for_tool,
    canonical_tool_for_capability,
)
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.mcp.adapter import MCPToolAdapter, MCPToolDefinition
from assistant_agent.mcp.config import MCPToolAdapterConfig
from assistant_agent.mcp.server import OfflineMCPServer
from assistant_agent.runtime.decision_models import AssistantDecision
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import (
    RunToolCatalog,
    ToolResult,
)
from assistant_agent.tools.ids import (
    IMAGE_GENERATION_TOOL_NAME,
    IMAGE_UNDERSTANDING_CAPABILITY,
    IMAGE_UNDERSTANDING_TOOL_NAME,
    PYTHON_INTERPRETER_TOOL_NAME,
    SHOPPING_SEARCH_TOOL_NAME,
    VIDEO_UNDERSTANDING_CAPABILITY,
    WEATHER_TOOL_NAME,
    WEB_FETCH_CAPABILITY,
)
from assistant_agent.tools.spec_adapters import (
    tool_spec_to_mcp_tool,
    tool_spec_to_openai_tool,
)
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.tools.base import ToolBase, ToolContext, ToolInputValidationError
from assistant_agent.tools.input_binding import ToolInputBinding
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry


def _contains_mapping_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(
            _contains_mapping_key(item, key) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_mapping_key(item, key) for item in value)
    return False


class _DeclaredValidationInput(BaseModel):
    value: str = Field(min_length=1)


class _DeclaredValidationTool(ToolBase):
    name = "declared_validation_tool"
    description = "Test-only tool with declarative media and tool-owned validation."
    input_schema = _DeclaredValidationInput
    output_schema = _DeclaredValidationInput
    category = "read"
    requires_media = ["image"]

    def validate_call(self, input: _DeclaredValidationInput) -> None:
        if input.value == "blocked":
            raise ToolInputValidationError(
                "tool_owned_validation_failed",
                "The tool-owned validator rejected this input.",
            )

    def _run(
        self, input: _DeclaredValidationInput, context: ToolContext
    ) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data=input.model_dump())


class _ExecutionBoundaryInput(BaseModel):
    value: str


class _SingleValidationInput(BaseModel):
    value: str
    validation_count: ClassVar[int] = 0

    @model_validator(mode="after")
    def count_validation(self) -> "_SingleValidationInput":
        type(self).validation_count += 1
        return self


class _ExecutionBoundaryTool(ToolBase):
    name = "execution_boundary_tool"
    description = "Test-only tool for staged execution."
    input_schema = _ExecutionBoundaryInput
    output_schema = _ExecutionBoundaryInput
    category = "read"

    def __init__(self) -> None:
        self.run_count = 0

    def _run(self, input: _ExecutionBoundaryInput, context: ToolContext) -> ToolResult:
        self.run_count += 1
        return ToolResult(tool_name=self.name, success=True, data=input.model_dump())


class _ReportedLatencyTool(_ExecutionBoundaryTool):
    name = "reported_latency_tool"

    def _run(self, input: _ExecutionBoundaryInput, context: ToolContext) -> ToolResult:
        self.run_count += 1
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=input.model_dump(),
            latency_ms=1,
        )


class _WriteBoundaryTool(_ExecutionBoundaryTool):
    name = "write_boundary_tool"
    category = "write"


class _RuntimeBindingInput(BaseModel):
    query: str
    user_id: str | None = None
    session_id: str | None = None
    image_ids: list[str] = Field(default_factory=list)
    limit: int = 0
    prior_summary: str | None = None
    runtime_note: str | None = None


class _RuntimeBindingTool(ToolBase):
    name = "runtime_binding_tool"
    description = "Test generic runtime-owned input binding."
    input_schema = _RuntimeBindingInput
    output_schema = _RuntimeBindingInput
    category = "read"
    input_bindings = (
        ToolInputBinding(field="user_id", source="runtime_identity", key="user_id"),
        ToolInputBinding(field="session_id", source="runtime_identity", key="session_id"),
        ToolInputBinding(field="image_ids", source="request", key="image_ids"),
        ToolInputBinding(field="limit", source="constant", value=5),
        ToolInputBinding(
            field="prior_summary",
            source="latest_tool_result",
            result_path="summary",
        ),
        ToolInputBinding(field="runtime_note", source="constant", value="default"),
    )

    def _run(self, input: _RuntimeBindingInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data=input.model_dump())


def test_provider_tools_hide_runtime_fields_and_pydantic_titles() -> None:
    registry = create_default_registry()
    execution_only_schema_keys = {
        "additionalProperties",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "uniqueItems",
    }

    for tool_name in (IMAGE_GENERATION_TOOL_NAME, SHOPPING_SEARCH_TOOL_NAME):
        spec = registry.get_spec(tool_name)
        properties = spec.input_schema["properties"]
        assert "user_id" not in properties
        assert "session_id" not in properties
        assert "memory_context" not in properties
        provider_tool = tool_spec_to_openai_tool(spec)
        assert not _contains_mapping_key(
            provider_tool["function"]["parameters"], "title"
        )

    for spec in registry.list_specs():
        parameters = tool_spec_to_openai_tool(spec)["function"]["parameters"]
        for key in execution_only_schema_keys:
            assert not _contains_mapping_key(parameters, key)
        for field_name, field_schema in parameters.get("properties", {}).items():
            assert field_schema.get("description"), f"{spec.name}.{field_name}"

    assert "memory_search" not in registry.list()
    assert "memory_get" not in registry.list()
    assert "memory_save" not in registry.list()

    shopping = tool_spec_to_openai_tool(registry.get_spec(SHOPPING_SEARCH_TOOL_NAME))["function"]
    assert set(shopping["parameters"]["properties"]) == {
        "query",
        "budget_min",
        "budget_max",
        "platforms",
    }
    assert shopping["parameters"]["required"] == ["query"]
    assert "description" not in shopping["parameters"]
    assert not _contains_mapping_key(shopping["parameters"], "anyOf")
    assert not _contains_mapping_key(shopping["parameters"], "default")

    vision = tool_spec_to_openai_tool(
        registry.get_spec(IMAGE_UNDERSTANDING_TOOL_NAME)
    )["function"]
    assert set(vision["parameters"]["properties"]) == {"question"}

    web_fetch = tool_spec_to_openai_tool(registry.get_spec("web_fetch"))["function"]
    assert set(web_fetch["parameters"]["properties"]) == {"url"}
    assert web_fetch["parameters"]["required"] == ["url"]

    calendar_create = tool_spec_to_openai_tool(
        registry.get_spec("calendar_create")
    )["function"]
    assert set(calendar_create["parameters"]["properties"]) == {
        "title",
        "start_time",
        "end_time",
        "timezone",
        "location",
        "attendees",
        "notes",
    }
    assert calendar_create["parameters"]["required"] == ["title", "start_time"]

    calendar_create = tool_spec_to_openai_tool(
        registry.get_spec("calendar_create")
    )["function"]
    assert "idempotency_key" not in calendar_create["parameters"]["properties"]


def test_builtin_tools_declare_plugin_ownership_without_duplicate_grouping() -> None:
    registry = create_default_registry()

    for record in registry.list_registration_records():
        if record.source_type != "builtin":
            continue
        spec = registry.get_spec(record.tool_name)
        assert record.plugin_id != "manual", record.tool_name
        assert "toolset" not in spec.model_dump(mode="json"), record.tool_name


def test_generic_runtime_bindings_shrink_schema_and_bind_each_run_identity() -> None:
    tool = _RuntimeBindingTool()
    registry = ToolRegistry()
    registry.register(tool)
    spec = registry.get_spec(tool.name)
    first_request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="bind",
        image_ids=["image-1"],
    )
    first_state = AgentState.from_request(first_request)
    first_state.tool_results.append(
        ToolResult(
            tool_name="prior_tool",
            success=True,
            data={"summary": "derived from a prior result"},
        )
    )
    decision = AssistantDecision(
        type="tool_call",
        tool_name=tool.name,
        tool_input={"query": "first"},
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=first_request,
        state=first_state,
    )

    first = ToolExecutor(registry=registry).run_tool(
        first_state,
        "step-1",
        tool.name,
        decision.tool_input,
        validated_input=validation.validated_input,
        runtime_input={"runtime_note": "trusted override"},
    )
    second_state = AgentState.from_request(
        UserRequest(
            user_id="user-2",
            session_id="session-2",
            text="bind",
        )
    )
    second = ToolExecutor(registry=registry).run_tool(
        second_state,
        "step-2",
        tool.name,
        {"query": "second"},
    )

    assert set(spec.input_schema["properties"]) == {"query"}
    assert spec.input_schema["required"] == ["query"]
    assert validation.accepted is True
    assert first.data == {
        "query": "first",
        "user_id": "user-1",
        "session_id": "session-1",
        "image_ids": ["image-1"],
        "limit": 5,
        "prior_summary": "derived from a prior result",
        "runtime_note": "trusted override",
    }
    assert second.data["user_id"] == "user-2"
    assert second.data["session_id"] == "session-2"
    assert second.data["image_ids"] == []
    assert second.data["prior_summary"] is None


def test_model_cannot_submit_runtime_owned_tool_fields() -> None:
    tool = _RuntimeBindingTool()
    registry = ToolRegistry()
    registry.register(tool)
    request = UserRequest(user_id="user-1", session_id="session-1", text="bind")

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=tool.name,
            tool_input={"query": "value", "user_id": "spoofed"},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "runtime_owned_tool_input"


def test_memory_is_runtime_lifecycle_not_a_default_tool() -> None:
    registry = create_default_registry()
    assert {"memory_search", "memory_get", "memory_save"}.isdisjoint(registry.list())


def test_mcp_tool_spec_preserves_canonical_json_schema() -> None:
    input_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["open", "closed"],
                "description": "Issue status.",
            }
        },
        "required": ["status"],
    }
    adapter = MCPToolAdapter(
        MCPToolAdapterConfig(
            server_name="issues",
            allowed_tools=["search"],
            read_only_tools=["search"],
        )
    )

    spec = adapter.tool_spec_for_definition(
        MCPToolDefinition(
            name="search",
            description="Search issues.",
            input_schema=input_schema,
        )
    )

    assert spec is not None
    expected_schema = {
        "type": "object",
        "properties": input_schema["properties"],
        "required": ["status"],
    }
    assert spec.input_schema == expected_schema
    assert tool_spec_to_mcp_tool(spec)["inputSchema"] == expected_schema
    assert tool_spec_to_openai_tool(spec)["function"]["parameters"] == expected_schema


def test_offline_mcp_server_lists_standard_json_schemas() -> None:
    definitions = OfflineMCPServer().list_tools()

    assert definitions
    assert all("input_schema" not in definition for definition in definitions)
    assert all(definition["inputSchema"]["type"] == "object" for definition in definitions)
    assert all("fields" not in definition["inputSchema"] for definition in definitions)
    tool_run = next(definition for definition in definitions if definition["name"] == "tool_run")
    assert tool_run["inputSchema"]["required"] == ["tool_name"]


def test_weather_declares_location_and_normalized_target_date() -> None:
    registry = create_default_registry()
    spec = registry.get_spec(WEATHER_TOOL_NAME)
    openai_tool = tool_spec_to_openai_tool(spec)
    mcp_tool = tool_spec_to_mcp_tool(spec)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="查一下天气",
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=WEATHER_TOOL_NAME,
            tool_input={"location": "   "},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    parameters = openai_tool["function"]["parameters"]
    assert "required_inputs" not in spec.model_dump(mode="json")
    assert "fields" not in spec.input_schema
    assert not _contains_mapping_key(parameters, "title")
    assert mcp_tool["inputSchema"] == parameters
    assert parameters["required"] == ["location"]
    assert set(parameters["properties"]) == {"location", "target_date"}
    assert parameters["properties"]["target_date"]["type"] == "string"
    assert "format" not in parameters["properties"]["target_date"]
    assert "additionalProperties" not in parameters
    assert not _contains_mapping_key(parameters, "default")
    assert result.accepted is False
    assert result.code == "invalid_tool_input"

    dated_result = ToolExecutor(registry=registry).run_tool(
        AgentState.from_request(request),
        "step-weather",
        WEATHER_TOOL_NAME,
        {
            "location": " 北京 ",
            "target_date": "2026-07-22/2026-07-23",
        },
    )

    assert dated_result.success is True
    assert [item["date"] for item in dated_result.data["forecast"]] == [
        "2026-07-22",
        "2026-07-23",
    ]


def test_registry_exposes_one_simple_tool_contract() -> None:
    registry = ToolRegistry()
    registry.register(_DeclaredValidationTool())

    spec = registry.get_spec(_DeclaredValidationTool.name)

    assert spec.category == "read"
    assert not hasattr(spec, "requires_confirmation")
    assert spec.requires_media == ["image"]
    assert not hasattr(spec, "redact_trace")
    assert not hasattr(spec, "policy")
    assert not hasattr(spec, "execution")
    assert not hasattr(spec, "when_to_use")
    assert not hasattr(spec, "runtime_constraints")
    assert not hasattr(spec, "requires_env")
    assert not hasattr(spec, "skill_only")
    assert not hasattr(spec, "progress_message")
    assert spec.enabled_by_default is True


def test_legacy_tool_mapping_remains_compatible_without_a_tool_manifest() -> None:
    assert canonical_tool_for_capability(IMAGE_UNDERSTANDING_CAPABILITY) == (
        IMAGE_UNDERSTANDING_TOOL_NAME
    )
    assert canonical_tool_for_capability(VIDEO_UNDERSTANDING_CAPABILITY) == (
        IMAGE_UNDERSTANDING_TOOL_NAME
    )
    assert canonical_capability_for_tool(IMAGE_UNDERSTANDING_TOOL_NAME) == (
        IMAGE_UNDERSTANDING_CAPABILITY
    )
    assert canonical_action_for_capability(VIDEO_UNDERSTANDING_CAPABILITY) == (
        "understand_video"
    )
    assert canonical_capability_for_action("read_url") == WEB_FETCH_CAPABILITY


def test_validated_tool_input_is_reused_by_executor() -> None:
    class SingleValidationTool(_ExecutionBoundaryTool):
        name = "single_validation_tool"
        input_schema = _SingleValidationInput

    _SingleValidationInput.validation_count = 0
    tool = SingleValidationTool()
    registry = ToolRegistry()
    registry.register(tool)
    request = UserRequest(user_id="user-1", session_id="session-1", text="execute")
    state = AgentState.from_request(request)
    decision = AssistantDecision(
        type="tool_call",
        tool_name=tool.name,
        tool_input={"value": "ok"},
    )

    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        tool.name,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert validation.accepted is True
    assert result.success is True
    assert _SingleValidationInput.validation_count == 1


def test_tool_trace_content_is_enabled_by_default_with_explicit_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    registry.register(_ExecutionBoundaryTool())

    monkeypatch.delenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", raising=False)
    content_trace = InMemoryTraceStore()
    content_state = AgentState.from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="execute")
    )
    ToolExecutor(registry=registry).run_tool(
        content_state,
        "content-step",
        _ExecutionBoundaryTool.name,
        {"value": "private-value"},
        trace_store=content_trace,
        trace_id=content_state.trace_id,
    )

    content_events = content_trace.list_by_run(content_state.run_id)
    assert content_events[0].input_summary == {"value": "private-value"}
    assert (
        content_events[0].attributes["tool_call_id"]
        == content_state.tool_calls[0].tool_call_id
    )
    assert content_events[1].output_summary["data"] == {"value": "private-value"}

    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "0")
    reduced_trace = InMemoryTraceStore()
    reduced_state = AgentState.from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="execute")
    )
    ToolExecutor(registry=registry).run_tool(
        reduced_state,
        "reduced-step",
        _ExecutionBoundaryTool.name,
        {"value": "visible-value"},
        trace_store=reduced_trace,
        trace_id=reduced_state.trace_id,
    )

    reduced_events = reduced_trace.list_by_run(reduced_state.run_id)
    assert reduced_events[0].input_summary["redacted"] is True
    assert "visible-value" not in str(reduced_events)


def test_available_catalog_is_the_execution_boundary() -> None:
    catalog = RunToolCatalog(available_tool_names=["weather"])

    assert catalog.allows("weather") is True
    assert catalog.allows("calendar_create") is False


def test_action_validator_uses_available_catalog_as_its_only_run_allowlist() -> None:
    registry = ToolRegistry()
    registry.register(_ExecutionBoundaryTool())
    request = UserRequest(user_id="user-1", session_id="session-1", text="execute")
    state = AgentState.from_request(request)
    state.run_tool_catalog = RunToolCatalog(available_tool_names=[])

    rejected = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=_ExecutionBoundaryTool.name,
            tool_input={"value": "ok"},
        ),
        registry=registry,
        request=request,
        state=state,
    )
    state.run_tool_catalog = RunToolCatalog(
        available_tool_names=[_ExecutionBoundaryTool.name]
    )
    accepted = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=_ExecutionBoundaryTool.name,
            tool_input={"value": "ok"},
        ),
        registry=registry,
        request=request,
        state=state,
    )

    assert rejected.code == "tool_not_allowed_for_run"
    assert accepted.code == "accepted"


def test_tool_trace_uses_executor_wall_latency_instead_of_tool_reported_latency(monkeypatch) -> None:
    tool = _ReportedLatencyTool()
    registry = ToolRegistry()
    registry.register(tool)
    trace_store = InMemoryTraceStore()
    clock_values = iter((100.0, 107.45, 107.46))
    monkeypatch.setattr(
        "assistant_agent.runtime.tool_executor.perf_counter",
        lambda: next(clock_values),
    )
    state = AgentState.from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="execute")
    )

    ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        tool.name,
        {"value": "ok"},
        trace_store=trace_store,
        trace_id=state.trace_id,
    )

    terminal = next(
        event
        for event in trace_store.list_by_trace(state.trace_id)
        if event.canonical_event == "tool.finished"
    )
    assert terminal.latency_ms >= 7450
    assert terminal.attributes["tool_reported_latency_ms"] == 1


def test_tool_executor_runs_write_tool_without_a_confirmation_state() -> None:
    tool = _WriteBoundaryTool()
    registry = ToolRegistry()
    registry.register(tool)
    events = ListEventSink()
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="write externally",
    )

    result = ToolExecutor(registry=registry, event_sink=events).run_tool(
        AgentState.from_request(request),
        "step-1",
        tool.name,
        {"value": "write"},
    )

    assert result.success is True
    assert tool.run_count == 1
    assert [event.type for event in events.events] == [
        "tool_started",
        "tool_finished",
    ]


def test_llm_selected_tool_is_not_rejected_by_natural_language_intent_rules() -> None:
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="处理这个输入",
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=SHOPPING_SEARCH_TOOL_NAME,
            tool_input={"query": "牛奶"},
        ),
        registry=create_default_registry(),
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is True
    assert result.code == "accepted"


def test_declared_media_requirement_is_enforced_without_tool_name_branch() -> None:
    registry = ToolRegistry()
    registry.register(_DeclaredValidationTool())
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="处理这个输入",
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=_DeclaredValidationTool.name,
            tool_input={"value": "ok"},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "missing_required_input"


def test_tool_owned_validator_runs_without_action_validator_branch() -> None:
    registry = ToolRegistry()
    registry.register(_DeclaredValidationTool())
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="处理这个输入",
        image_ids=["image-1"],
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=_DeclaredValidationTool.name,
            tool_input={"value": "blocked"},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "tool_owned_validation_failed"


def test_python_safety_validation_is_owned_by_python_tool() -> None:
    registry = create_default_registry()
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="分析代码",
    )

    result = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=PYTHON_INTERPRETER_TOOL_NAME,
            tool_input={"code": 'open("secret.txt")'},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "unsafe_tool_input"


def test_python_enablement_remains_owned_by_python_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MULTIMODAL_AGENT_PYTHON_INTERPRETER_ENABLED", raising=False)

    result = create_default_registry().run(
        PYTHON_INTERPRETER_TOOL_NAME,
        {"code": "1 + 1"},
    )

    assert result.success is False
    assert result.data["errors"][0]["code"] == "python_interpreter_disabled"
