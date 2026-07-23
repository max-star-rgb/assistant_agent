"""Focused offline checks for stable tool-governance behavior."""

import json
from typing import ClassVar

import pytest
from pydantic import BaseModel, Field, model_validator

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.legacy_tool_mapping import (
    canonical_action_for_capability,
    canonical_capability_for_action,
    canonical_capability_for_tool,
    canonical_tool_for_capability,
)
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.mcp.adapter import MCPToolAdapter, MCPToolDefinition
from assistant_agent.mcp.config import MCPToolAdapterConfig
from assistant_agent.mcp.server import OfflineMCPServer
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import (
    RunToolCatalog,
    ToolResult,
    ToolSpec,
)
from assistant_agent.schemas.tool_ids import (
    IMAGE_GENERATION_TOOL_NAME,
    IMAGE_UNDERSTANDING_CAPABILITY,
    IMAGE_UNDERSTANDING_TOOL_NAME,
    PYTHON_INTERPRETER_TOOL_NAME,
    SHOPPING_SEARCH_TOOL_NAME,
    VIDEO_UNDERSTANDING_CAPABILITY,
    WEATHER_TOOL_NAME,
    WEB_FETCH_CAPABILITY,
)
from assistant_agent.schemas.tool_spec_adapters import (
    tool_spec_to_mcp_tool,
    tool_spec_to_openai_tool,
)
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.tools.base import ToolBase, ToolContext, ToolInputValidationError
from assistant_agent.tools.input_binding import ToolInputBinding
from assistant_agent.tools.plugins.durable_task.tool import TaskPlanSubmitTool
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


class _DeclaredValidationInput(BaseModel):
    value: str = Field(min_length=1)


class _DeclaredValidationTool(ToolBase):
    name = "declared_validation_tool"
    description = "Test-only tool with declarative media and tool-owned validation."
    input_schema = _DeclaredValidationInput
    output_schema = _DeclaredValidationInput
    category = "read"
    requires_confirmation = False
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
    requires_confirmation = False

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


class _ConfirmationBoundaryTool(_ExecutionBoundaryTool):
    name = "confirmation_boundary_tool"
    category = "write"
    requires_confirmation = True


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
    requires_confirmation = False
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


def test_provider_description_uses_simple_tool_spec() -> None:
    spec = ToolSpec(
        name="canonical_policy_tool",
        description="Read canonical data.",
        category="read",
        requires_confirmation=False,
    )

    payload = tool_spec_to_openai_tool(spec)

    description = payload["function"]["description"]
    assert description == "Read canonical data."


def test_builtin_tool_spec_descriptions_use_chinese() -> None:
    registry = create_default_registry()
    durable_registry = ToolRegistry()
    durable_registry.register(TaskPlanSubmitTool(object()))
    specs = [*registry.list_specs(), *durable_registry.list_specs()]

    descriptions: list[tuple[str, str]] = []
    for spec in specs:
        _collect_descriptions(
            {
                "description": spec.description,
                "input_schema": spec.input_schema,
            },
            path=spec.name,
            result=descriptions,
        )

    assert descriptions
    assert [
        (path, description)
        for path, description in descriptions
        if not any("\u4e00" <= character <= "\u9fff" for character in description)
    ] == []


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
        assert '"title"' not in json.dumps(
            provider_tool["function"]["parameters"],
            ensure_ascii=False,
        )

    for spec in registry.list_specs():
        parameters = tool_spec_to_openai_tool(spec)["function"]["parameters"]
        encoded = json.dumps(parameters, ensure_ascii=False)
        for key in execution_only_schema_keys:
            assert f'"{key}"' not in encoded
        for field_name, field_schema in parameters.get("properties", {}).items():
            assert field_schema.get("description"), f"{spec.name}.{field_name}"

    search = tool_spec_to_openai_tool(registry.get_spec("memory_search"))["function"]
    assert search["parameters"] == {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "需要从过往对话记忆中检索的主题、事实或问题。",
            }
        },
        "required": ["query"],
    }

    get = tool_spec_to_openai_tool(registry.get_spec("memory_get"))["function"]
    assert get["parameters"] == {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "需要读取的完整每日记忆记录 ID。",
            }
        },
        "required": ["memory_id"],
    }
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
    assert "anyOf" not in json.dumps(shopping["parameters"], ensure_ascii=False)
    assert '"default"' not in json.dumps(shopping["parameters"], ensure_ascii=False)

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


def _collect_descriptions(
    value: object,
    *,
    path: str,
    result: list[tuple[str, str]],
) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _collect_descriptions(
                item,
                path=f"{path}[{index}]",
                result=result,
            )
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        item_path = f"{path}.{key}"
        if key == "description" and isinstance(item, str):
            result.append((item_path, item))
        _collect_descriptions(item, path=item_path, result=result)


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


def test_memory_tools_expose_read_only_contracts() -> None:
    registry = create_default_registry()
    request = UserRequest(user_id="user-1", session_id="session-1", text="查一下昨天的记录")
    state = AgentState.from_request(request)
    state.run_tool_catalog = RunToolCatalog(available_tool_names=["memory_search"])
    tool_input = {"query": "昨天喝了什么"}
    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="memory_search",
            tool_input=tool_input,
        ),
        registry=registry,
        request=request,
        state=state,
    )

    assert validation.accepted is True
    assert registry.get_spec("memory_search").category == "read"
    assert registry.get_spec("memory_get").category == "read"
    assert "memory_save" not in registry.list()


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
    assert '"title"' not in json.dumps(parameters, ensure_ascii=False)
    assert mcp_tool["inputSchema"] == parameters
    assert parameters["required"] == ["location"]
    assert set(parameters["properties"]) == {"location", "target_date", "days"}
    assert parameters["properties"]["target_date"]["type"] == "string"
    assert "format" not in parameters["properties"]["target_date"]
    assert "additionalProperties" not in parameters
    assert '"default"' not in json.dumps(parameters, ensure_ascii=False)
    assert result.accepted is False
    assert result.code == "invalid_tool_input"

    dated_result = ToolExecutor(registry=registry).run_tool(
        AgentState.from_request(request),
        "step-weather",
        WEATHER_TOOL_NAME,
        {
            "location": " 北京 ",
            "target_date": "2026-07-22",
            "days": 2,
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
    assert spec.requires_confirmation is False
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


def test_tool_trace_content_requires_explicit_local_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    registry.register(_ExecutionBoundaryTool())

    monkeypatch.delenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", raising=False)
    safe_trace = InMemoryTraceStore()
    safe_state = AgentState.from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="execute")
    )
    ToolExecutor(registry=registry).run_tool(
        safe_state,
        "safe-step",
        _ExecutionBoundaryTool.name,
        {"value": "private-value"},
        trace_store=safe_trace,
        trace_id=safe_state.trace_id,
    )

    safe_events = safe_trace.list_by_run(safe_state.run_id)
    assert safe_events[0].input_summary["redacted"] is True
    assert safe_events[0].attributes["tool_call_id"] == safe_state.tool_calls[0].call_id
    assert "private-value" not in str(safe_events)

    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    local_trace = InMemoryTraceStore()
    local_state = AgentState.from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="execute")
    )
    ToolExecutor(registry=registry).run_tool(
        local_state,
        "local-step",
        _ExecutionBoundaryTool.name,
        {"value": "visible-value"},
        trace_store=local_trace,
        trace_id=local_state.trace_id,
    )

    local_events = local_trace.list_by_run(local_state.run_id)
    assert local_events[0].input_summary == {"value": "visible-value"}
    assert local_events[1].output_summary["data"] == {"value": "visible-value"}


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


def test_tool_executor_stages_do_not_commit_during_invocation() -> None:
    tool = _ExecutionBoundaryTool()
    registry = ToolRegistry()
    registry.register(tool)
    events = ListEventSink()
    executor = ToolExecutor(registry=registry, event_sink=events)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="execute",
    )
    state = AgentState.from_request(request)

    prepared = executor.prepare_tool_call(
        state,
        "step-1",
        tool.name,
        {"value": "ok"},
    )

    assert tool.run_count == 0
    assert len(state.tool_calls) == 1
    assert state.tool_results == []
    assert [event.type for event in events.events] == ["tool_started"]

    invocation = executor.invoke_tool(prepared)

    assert tool.run_count == 1
    assert state.tool_results == []
    assert [event.type for event in events.events] == ["tool_started"]

    result = executor.commit_tool_result(state, prepared, invocation)

    assert result.success is True
    assert [item.data for item in state.tool_results] == [{"value": "ok"}]
    assert [event.type for event in events.events] == ["tool_started", "tool_finished"]


def test_tool_trace_uses_executor_wall_latency_instead_of_tool_reported_latency(monkeypatch) -> None:
    tool = _ReportedLatencyTool()
    registry = ToolRegistry()
    registry.register(tool)
    trace_store = InMemoryTraceStore()
    clock_values = iter((100.0, 107.45, 107.46))
    monkeypatch.setattr(
        "assistant_agent.agent.tool_executor.perf_counter",
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


def test_staged_executor_preserves_confirmation_without_invoking_tool() -> None:
    tool = _ConfirmationBoundaryTool()
    registry = ToolRegistry()
    registry.register(tool)
    events = ListEventSink()
    executor = ToolExecutor(registry=registry, event_sink=events)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="write externally",
    )
    state = AgentState.from_request(request)

    result = executor.run_tool(state, "step-1", tool.name, {"value": "write"})

    assert result.success is True
    assert result.data["status"] == "confirmation_required"
    assert tool.run_count == 0
    assert [event.type for event in events.events] == ["tool_started", "tool_finished"]


def test_confirmation_is_bound_to_the_declared_tool_name() -> None:
    tool = _ConfirmationBoundaryTool()
    registry = ToolRegistry()
    registry.register(tool)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="write externally",
        metadata={
            "tool_confirmation": {
                "confirmed": True,
                "tool_name": tool.name,
            }
        },
    )

    result = ToolExecutor(registry=registry).run_tool(
        AgentState.from_request(request),
        "step-1",
        tool.name,
        {"value": "write"},
    )

    assert result.success is True
    assert tool.run_count == 1


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
