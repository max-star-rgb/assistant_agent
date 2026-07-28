"""Runtime construction for Langfuse execution profiles."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.decision_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.ids import WEATHER_TOOL_NAME
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.backend import (
    configured_calendar_weather_contacts_tools,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.local_calendar import (
    LocalSQLiteCalendarAdapter,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    CalendarSearchTool,
    WeatherTool,
)
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry
from evals.langfuse.calendar_fixture import (
    CalendarEvalCreateTool,
    CalendarEvalEnvironment,
    EvalCalendarEvent,
)
from evals.langfuse.contracts import (
    CalendarEventExpectation,
    CreateCalendarCase,
    ExperimentCase,
    FrozenFileFixture,
    NoToolCase,
    ReadCalendarCase,
    RealAgentCase,
    RuntimeBundle,
    StatelessEvalEnvironment,
)
from evals.langfuse.manifest import load_eval_manifest
from evals.langfuse.weather_failure_fixture import (
    SimulatedWeatherFailureAdapter,
    WeatherFailureFixture,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def validate_real_readonly_config(config: ProviderConfig) -> None:
    validate_real_chat_config(config)
    configured_tools = configured_calendar_weather_contacts_tools(
        load_mcp_server_configs_from_env()
    )
    if WEATHER_TOOL_NAME not in configured_tools:
        raise RuntimeError(
            "Real-readonly profile requires a configured MCP weather mapping."
        )


def validate_real_chat_config(config: ProviderConfig) -> None:
    if config.provider_mode != "real":
        raise RuntimeError(
            "Real Langfuse eval requires MULTIMODAL_AGENT_PROVIDER_MODE=real."
        )
    if config.chat_provider == "mock" or config.chat_adapter_kind == "mock":
        raise RuntimeError(
            "Real Langfuse eval requires an explicit real chat Provider."
        )
    missing = config.resolved_chat_provider().missing_required_env()
    if missing:
        raise RuntimeError(
            "Real Langfuse eval chat Provider is missing: "
            + ", ".join(missing)
            + "."
        )


class _ScriptedCalendarCreateChat:
    provider = "scripted"
    model = "scripted-calendar-create-eval"

    def __init__(self, case: CreateCalendarCase) -> None:
        arguments = {
            key: value
            for key, value in case.required_event.model_dump(mode="json").items()
            if value not in (None, [])
        }
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="calendar-create-eval-call",
                            name="calendar_create",
                            arguments=arguments,
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=(
                        f"已创建{case.required_event.title}，"
                        f"{'，'.join(case.response_facts)}。"
                    ),
                ),
            ]
        )

    def chat(self, _request: ChatRequest) -> ChatResult:
        return next(self._results)


class _ScriptedCalendarReadChat:
    provider = "scripted"
    model = "scripted-calendar-read-eval"

    def __init__(self, case: ReadCalendarCase) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="calendar-read-eval-call",
                            name="calendar_search",
                            arguments={"query": case.query},
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text="，".join(case.response_facts),
                ),
            ]
        )

    def chat(self, _request: ChatRequest) -> ChatResult:
        return next(self._results)


class _ScriptedDirectChat:
    provider = "scripted"
    model = "scripted-no-tool-eval"

    def __init__(self, case: NoToolCase) -> None:
        self._result = ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="，".join(case.response_facts),
        )

    def chat(self, _request: ChatRequest) -> ChatResult:
        return self._result


def build_scripted_runtime(
    _request: UserRequest,
    case: ExperimentCase,
) -> RuntimeBundle:
    environment = CalendarEvalEnvironment(
        [
            EvalCalendarEvent(
                event_id="existing-team-sync",
                title="团队同步",
                start_time="2026-07-25T10:00:00+08:00",
                end_time="2026-07-25T10:30:00+08:00",
                location="线上",
            )
        ]
    )
    registry = ToolRegistry()
    if isinstance(case, CreateCalendarCase):
        registry.register(CalendarEvalCreateTool(environment))
        chat_adapter: Any = _ScriptedCalendarCreateChat(case)
    elif isinstance(case, ReadCalendarCase):
        registry.register(CalendarSearchTool(environment))
        chat_adapter = _ScriptedCalendarReadChat(case)
    else:
        registry.register(CalendarSearchTool(environment))
        registry.register(CalendarEvalCreateTool(environment))
        chat_adapter = _ScriptedDirectChat(case)
    registry.seal()
    return RuntimeBundle(
        runtime=AgentGraphRuntime(
            registry=registry,
            config=ProviderConfig(langgraph_checkpointer_backend="none"),
            chat_adapter=chat_adapter,
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        ),
        environment=environment,
    )


def build_real_readonly_runtime(
    _request: UserRequest,
    case: ExperimentCase,
    *,
    config: ProviderConfig | None = None,
) -> RuntimeBundle:
    if not isinstance(case, RealAgentCase):
        raise ValueError("Real profiles only accept behavior Dataset items.")
    resolved = config or ProviderConfig.from_env()
    registry = None
    if case.capability == "tool_failure_recovery":
        validate_real_chat_config(resolved)
        if case.weather_failure is None:
            raise ValueError(
                "A tool_failure_recovery case requires weather_failure."
            )
        registry = ToolRegistry()
        registry.register(
            WeatherTool(
                adapter=SimulatedWeatherFailureAdapter(case.weather_failure)
            )
        )
        registry.seal()
    elif case.capability in {"direct_response", "clarification"}:
        validate_real_chat_config(resolved)
    else:
        validate_real_readonly_config(resolved)
    isolated = replace(
        resolved,
        mem0_base_url=None,
        conversation_history_backend="memory",
        langgraph_checkpointer_backend="none",
        durable_tasks_enabled=False,
        durable_task_worker_enabled=False,
    )
    return RuntimeBundle(
        runtime=AgentGraphRuntime(
            config=isolated,
            registry=registry,
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        ),
        environment=StatelessEvalEnvironment(),
    )


def build_real_system_runtime(
    request: UserRequest,
    case: ExperimentCase,
    *,
    config: ProviderConfig | None = None,
    calendar_path: str | Path = ".data/evals/langfuse/calendar.sqlite3",
) -> RuntimeBundle:
    if not isinstance(case, RealAgentCase):
        raise ValueError("Real profiles only accept behavior Dataset items.")
    resolved = config or ProviderConfig.from_env()
    if case.capability == "tool_failure_recovery":
        return build_real_readonly_runtime(
            request,
            case,
            config=resolved,
        )
    if WEATHER_TOOL_NAME in case.required_tools:
        validate_real_readonly_config(resolved)
    else:
        validate_real_chat_config(resolved)
    isolated = replace(
        resolved,
        mem0_base_url=None,
        conversation_history_backend="memory",
        langgraph_checkpointer_backend="none",
        durable_tasks_enabled=False,
        durable_task_worker_enabled=False,
    )
    if case.frozen_file is not None:
        isolated = prepare_frozen_file_fixture(
            isolated,
            case.frozen_file,
        )
    calendar = LocalSQLiteCalendarAdapter(
        calendar_path,
        namespace=request.user_id,
    )
    registry = create_default_registry(isolated, calendar_adapter=calendar)
    return RuntimeBundle(
        runtime=AgentGraphRuntime(
            config=isolated,
            registry=registry,
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        ),
        environment=calendar,
    )


def case_from_dataset_fields(
    *,
    expected_output: dict[str, Any],
    metadata: dict[str, Any],
    case_id: str,
) -> ExperimentCase:
    manifest = load_eval_manifest()
    raw_capability = metadata.get("capability")
    if not isinstance(raw_capability, str):
        raise ValueError("Dataset item metadata requires capability.")
    capability = manifest.normalize_capability(raw_capability)
    profile = metadata.get("profile")
    compatible_profiles = metadata.get("compatible_profiles")
    real_case = (
        raw_capability.startswith("real_")
        or isinstance(compatible_profiles, list)
    )
    response_facts = _string_list(expected_output.get("response_facts"))
    if profile == "scripted_mock" or raw_capability in {
        "write_tool",
        "read_only_tool",
        "no_tool",
    }:
        if capability == "calendar_write":
            return CreateCalendarCase(
                id=case_id,
                required_event=CalendarEventExpectation.model_validate(
                    expected_output.get("required_event")
                ),
                response_facts=response_facts,
            )
        if capability == "calendar_read":
            return ReadCalendarCase(
                id=case_id,
                query=str(expected_output.get("query") or ""),
                response_facts=response_facts,
            )
        if capability == "direct_response":
            return NoToolCase(id=case_id, response_facts=response_facts)
    if real_case:
        weather_failure = (
            WeatherFailureFixture.model_validate(
                expected_output.get("weather_failure")
            )
            if capability == "tool_failure_recovery"
            else None
        )
        frozen_file = (
            FrozenFileFixture.model_validate(
                expected_output.get("frozen_file_fixture")
            )
            if expected_output.get("frozen_file_fixture") is not None
            else None
        )
        return RealAgentCase(
            id=case_id,
            capability=capability,
            required_tools=_string_list(metadata.get("required_tools")),
            response_facts=response_facts,
            weather_failure=weather_failure,
            frozen_file=frozen_file,
        )
    raise ValueError(
        f"Unsupported Dataset capability/profile: {capability!r}/{profile!r}."
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def prepare_frozen_file_fixture(
    config: ProviderConfig,
    fixture: FrozenFileFixture,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> ProviderConfig:
    """Verify and stage one tracked fixture inside the governed file root."""

    repository_root = repository_root.resolve()
    source_path = _resolve_relative_path(
        repository_root,
        fixture.source_path,
    )
    content = source_path.read_bytes()
    actual_hash = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if actual_hash != fixture.sha256:
        raise RuntimeError(
            f"Frozen eval fixture hash mismatch: {fixture.source_path}."
        )

    file_root = Path(config.local_file_access_root).expanduser()
    if not file_root.is_absolute():
        file_root = repository_root / file_root
    file_root = file_root.resolve()
    target_path = _resolve_relative_path(file_root, fixture.target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists() or target_path.read_bytes() != content:
        target_path.write_bytes(content)
    return replace(config, local_file_access_root=str(file_root))


def _resolve_relative_path(root: Path, relative_value: str) -> Path:
    relative_path = Path(relative_value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("Frozen eval fixture paths must be relative and contained.")
    resolved = (root / relative_path).resolve()
    resolved.relative_to(root)
    return resolved
