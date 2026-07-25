"""Thin AgentRuntime task for Langfuse-native Dataset experiments.

The project executes the Agent and returns structured evidence. Langfuse owns
the Dataset, evaluator execution, Scores, and Experiment comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, Field

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.trace_context import RuntimeTraceContext
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.otel_exporter import (
    OtlpHttpTextExporterConfig,
    TextOtelTraceObserver,
    create_otlp_http_text_span_exporter,
)
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceEvent
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.backend import (
    configured_personal_assistant_tools,
)
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.schemas.tool_ids import WEATHER_TOOL_NAME
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.tools import (
    CalendarSearchTool,
)
from assistant_agent.tools.registry import ToolRegistry
from evals.cases.langfuse.calendar_fixture import (
    CalendarEvalCreateTool,
    CalendarEvalEnvironment,
    EvalCalendarEvent,
)


DEFAULT_DATASET_NAME = "assistant-agent-closed-loop-v1"
DEFAULT_DATASET_SEED = Path(
    "evals/cases/langfuse/agent_closed_loop_v1.seed.json"
)
REAL_READONLY_DATASET_NAME = "assistant-agent-real-readonly-v1"
REAL_READONLY_DATASET_SEED = Path(
    "evals/cases/langfuse/agent_real_readonly_v1.seed.json"
)
AGENT_EVALUATION_OBJECTIVE = (
    "验证 Agent 是否在受治理的 Runtime 中正确完成、克制或只读执行任务，并由 Tool Trace、"
    "Policy、环境状态变化和最终回答共同证明结果。当前 scripted mock 实验用于验证闭环评测"
    "基础设施，不代表真实模型的泛化能力。"
)
REAL_READONLY_EVALUATION_OBJECTIVE = (
    "验证真实 Chat Provider 是否在受治理的 Runtime 中正确克制或调用真实只读 Tool，"
    "并由 Tool Trace、Policy、Provider 终态和最终回答共同证明结果。该 profile 不执行"
    "写操作，不接入真实 Memory。"
)


class DatasetSeedItem(BaseModel):
    """One optional bootstrap item for a Langfuse Dataset."""

    id: str = Field(min_length=1)
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetSeed(BaseModel):
    """Optional versioned seed; Langfuse remains the runtime authority."""

    dataset_name: str = Field(min_length=1)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    items: list[DatasetSeedItem] = Field(min_length=1)

    def content_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class DatasetSeedResult(BaseModel):
    """Result of explicitly seeding a Langfuse Dataset."""

    dataset_name: str
    seed_hash: str
    item_ids: list[str]


class CalendarEventExpectation(BaseModel):
    """Calendar facts used to drive the deterministic mock rollout."""

    title: str = Field(min_length=1)
    start_time: str = Field(min_length=1)
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = None


class CreateCalendarCase(BaseModel):
    id: str = Field(min_length=1)
    required_event: CalendarEventExpectation
    response_facts: list[str] = Field(default_factory=list)


class ReadCalendarCase(BaseModel):
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    response_facts: list[str] = Field(default_factory=list)


class NoToolCase(BaseModel):
    id: str = Field(min_length=1)
    response_facts: list[str] = Field(default_factory=list)


class RealReadonlyCase(BaseModel):
    """Dynamic real-provider case scored from runtime evidence, not fixtures."""

    id: str = Field(min_length=1)
    capability: Literal["real_no_tool", "real_read_only_tool"]
    response_facts: list[str] = Field(default_factory=list)


ExperimentCase = (
    CreateCalendarCase | ReadCalendarCase | NoToolCase | RealReadonlyCase
)


class AgentExperimentOutput(BaseModel):
    """Compact evidence contract consumed by a Langfuse Code Evaluator."""

    schema_version: Literal["agent_experiment_output_v1"] = "agent_experiment_output_v1"
    case_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    terminal_status: str = Field(min_length=1)
    response: dict[str, Any] | None = None
    available_tools: list[str] = Field(default_factory=list)
    request_metadata: dict[str, Any] = Field(default_factory=dict)
    tool_executions: list[dict[str, Any]] = Field(default_factory=list)
    validation_results: list[dict[str, Any]] = Field(default_factory=list)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    final_state: dict[str, Any] = Field(default_factory=dict)
    state_diff: dict[str, Any] = Field(default_factory=dict)
    trace_event_names: list[str] = Field(default_factory=list)
    provider_result_kinds: list[str] = Field(default_factory=list)
    total_latency_ms: int = Field(default=0, ge=0)


class LangfuseExperimentClient(Protocol):
    def get_current_trace_id(self) -> str | None: ...

    def get_current_observation_id(self) -> str | None: ...


class RuntimeTraceObserver(Protocol):
    def on_trace_event(self, event: Any) -> None: ...

    def close(self, *, timeout: float) -> bool: ...


class EvalStateEnvironment(Protocol):
    def snapshot(self) -> dict[str, Any]: ...

    def diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RuntimeBundle:
    runtime: AgentGraphRuntime
    environment: EvalStateEnvironment


RuntimeFactory = Callable[[UserRequest, ExperimentCase], RuntimeBundle]


class StatelessEvalEnvironment:
    """No-persistence state boundary for real read-only and no-tool evals."""

    def snapshot(self) -> dict[str, Any]:
        return {}

    def diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "added": [],
            "modified": [],
            "deleted": [],
            "duplicate_groups": [],
        }


def validate_real_readonly_config(config: ProviderConfig) -> None:
    """Fail before an Experiment unless real chat and weather are configured."""

    if config.provider_mode != "real":
        raise RuntimeError(
            "Real Langfuse eval requires "
            "MULTIMODAL_AGENT_PROVIDER_MODE=real."
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
    configured_tools = configured_personal_assistant_tools(
        load_mcp_server_configs_from_env()
    )
    if WEATHER_TOOL_NAME not in configured_tools:
        raise RuntimeError(
            "Real-readonly Dataset requires a configured MCP weather mapping."
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


class AgentExperimentTask:
    """Execute one Dataset item through the real governed Runtime."""

    def __init__(
        self,
        *,
        client: LangfuseExperimentClient,
        runtime_factory: RuntimeFactory | None = None,
        trace_observer: RuntimeTraceObserver | None = None,
    ) -> None:
        self.client = client
        self.runtime_factory = runtime_factory or build_scripted_runtime
        self.trace_observer = trace_observer

    def __call__(self, *, item: Any, **_: Any) -> AgentExperimentOutput:
        trace_id = self.client.get_current_trace_id()
        parent_span_id = self.client.get_current_observation_id()
        if not trace_id or not parent_span_id:
            raise RuntimeError(
                "Langfuse Experiment task is missing its active trace/span context."
            )

        item_input = _item_field(item, "input")
        expected_output = _item_field(item, "expected_output")
        metadata = _item_field(item, "metadata") or {}
        if not isinstance(item_input, dict) or not isinstance(expected_output, dict):
            raise ValueError("Agent Dataset item input/expected_output must be objects.")

        request = UserRequest.model_validate(item_input.get("user_request"))
        case_id = _case_id(item, metadata)
        case = case_from_dataset_fields(
            expected_output=expected_output,
            metadata=metadata,
            case_id=case_id,
        )
        bundle = self.runtime_factory(request, case)
        initial_state = bundle.environment.snapshot()
        try:
            state = bundle.runtime.run_state(
                request,
                trace_context=RuntimeTraceContext(
                    trace_id=trace_id,
                    parent_span_id=parent_span_id,
                ),
            )
            trace_events = bundle.runtime.trace_store.list_by_run(state.run_id)
        finally:
            bundle.runtime.close()
        final_state = bundle.environment.snapshot()

        if self.trace_observer is not None:
            for event in trace_events:
                self.trace_observer.on_trace_event(event)

        return AgentExperimentOutput(
            case_id=case_id,
            run_id=state.run_id,
            trace_id=state.trace_id,
            terminal_status=state.status,
            response=(
                state.response.model_dump(mode="json")
                if state.response is not None
                else None
            ),
            available_tools=(
                list(state.run_tool_catalog.available_tool_names)
                if state.run_tool_catalog is not None
                else []
            ),
            request_metadata=dict(state.request.metadata),
            tool_executions=_tool_executions(trace_events),
            validation_results=_validation_results(trace_events),
            initial_state=initial_state,
            final_state=final_state,
            state_diff=bundle.environment.diff(initial_state, final_state),
            trace_event_names=[
                event.canonical_event
                for event in trace_events
                if event.canonical_event is not None
            ],
            provider_result_kinds=_provider_result_kinds(trace_events),
            total_latency_ms=_total_latency_ms(trace_events),
        )


def load_dataset_seed(path: Path | str = DEFAULT_DATASET_SEED) -> DatasetSeed:
    """Load the optional bootstrap seed without contacting Langfuse."""

    return DatasetSeed.model_validate_json(Path(path).read_text(encoding="utf-8"))


def seed_langfuse_dataset(client: Any, seed: DatasetSeed) -> DatasetSeedResult:
    """Explicitly bootstrap or reset the seeded items in Langfuse."""

    seed_hash = seed.content_hash()
    client.create_dataset(
        name=seed.dataset_name,
        description=seed.description or AGENT_EVALUATION_OBJECTIVE,
        metadata={**seed.metadata, "seed_hash": seed_hash},
    )
    for item in seed.items:
        client.create_dataset_item(
            dataset_name=seed.dataset_name,
            id=item.id,
            input=item.input,
            expected_output=item.expected_output,
            metadata={
                **item.metadata,
                "case_id": item.id,
                "seed_hash": seed_hash,
            },
        )
    return DatasetSeedResult(
        dataset_name=seed.dataset_name,
        seed_hash=seed_hash,
        item_ids=[item.id for item in seed.items],
    )


def run_langfuse_agent_experiment(
    client: Any,
    *,
    dataset_name: str,
    experiment_name: str,
    run_name: str | None = None,
    max_concurrency: int = 1,
    metadata: dict[str, Any] | None = None,
    runtime_factory: RuntimeFactory | None = None,
    trace_observer: RuntimeTraceObserver | None = None,
    execution_profile: Literal["scripted_mock", "real_readonly"] = "scripted_mock",
) -> Any:
    """Run the Agent task; Langfuse-native rules evaluate it asynchronously."""

    dataset = client.get_dataset(dataset_name)
    observer = trace_observer or create_required_eval_trace_observer()
    owns_observer = trace_observer is None
    evaluation_objective = (
        REAL_READONLY_EVALUATION_OBJECTIVE
        if execution_profile == "real_readonly"
        else AGENT_EVALUATION_OBJECTIVE
    )
    try:
        return dataset.run_experiment(
            name=experiment_name,
            run_name=run_name,
            description=evaluation_objective,
            task=AgentExperimentTask(
                client=client,
                runtime_factory=runtime_factory,
                trace_observer=observer,
            ),
            max_concurrency=max_concurrency,
            metadata={
                "provider_mode": (
                    "real" if execution_profile == "real_readonly" else "mock"
                ),
                "chat_provider": (
                    "configured"
                    if execution_profile == "real_readonly"
                    else "scripted"
                ),
                "execution_profile": execution_profile,
                "evaluation_objective": evaluation_objective,
                "evaluation_owner": "langfuse_code_evaluator",
                **(metadata or {}),
            },
        )
    finally:
        if owns_observer and not observer.close(timeout=10.0):
            raise RuntimeError(
                "Langfuse Experiment Runtime trace export did not close cleanly."
            )


def create_required_eval_trace_observer(
    env: Mapping[str, str] | None = None,
) -> TextOtelTraceObserver:
    """Create a synchronous, content-preserving Experiment trace exporter."""

    base_config = OtlpHttpTextExporterConfig.from_env(
        os.environ if env is None else env
    )
    setup = create_otlp_http_text_span_exporter(
        replace(base_config, enabled=True, include_content=True)
    )
    if setup.status != "ready" or setup.exporter is None:
        raise RuntimeError(
            setup.reason or "Langfuse Experiment OTLP trace exporter is unavailable."
        )
    return TextOtelTraceObserver(
        setup.exporter,
        enabled=True,
        continue_on_error=False,
        include_content=True,
    )


def build_scripted_runtime(
    _request: UserRequest,
    case: ExperimentCase,
) -> RuntimeBundle:
    """Build one deterministic rollout for the infrastructure baseline."""

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
    """Build an isolated real-provider Runtime for no-tool/read-only cases."""

    if not isinstance(case, RealReadonlyCase):
        raise ValueError(
            "The real-readonly Runtime only accepts real_no_tool or "
            "real_read_only_tool Dataset items."
        )
    resolved = config or ProviderConfig.from_env()
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
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        ),
        environment=StatelessEvalEnvironment(),
    )


def case_from_dataset_fields(
    *,
    expected_output: dict[str, Any],
    metadata: dict[str, Any],
    case_id: str,
) -> ExperimentCase:
    """Build only the scripted rollout fixture; scoring stays in Langfuse."""

    capability = metadata.get("capability")
    response_facts = _string_list(expected_output.get("response_facts"))
    if capability == "write_with_confirmation":
        return CreateCalendarCase(
            id=case_id,
            required_event=CalendarEventExpectation.model_validate(
                expected_output.get("required_event")
            ),
            response_facts=response_facts,
        )
    if capability == "read_only_tool":
        return ReadCalendarCase(
            id=case_id,
            query=str(expected_output.get("query") or ""),
            response_facts=response_facts,
        )
    if capability == "no_tool":
        return NoToolCase(id=case_id, response_facts=response_facts)
    if capability in {"real_no_tool", "real_read_only_tool"}:
        return RealReadonlyCase(
            id=case_id,
            capability=capability,
            response_facts=response_facts,
        )
    raise ValueError(f"Unsupported Dataset capability: {capability!r}.")


def _tool_executions(events: list[TraceEvent]) -> list[dict[str, Any]]:
    terminals = {
        event.attributes.get("tool_call_id"): event
        for event in events
        if event.canonical_event in {"tool.finished", "tool.failed"}
    }
    executions: list[dict[str, Any]] = []
    for event in events:
        if event.canonical_event != "tool.started":
            continue
        tool_call_id = event.attributes.get("tool_call_id")
        terminal = terminals.get(tool_call_id)
        executions.append(
            {
                "tool_call_id": tool_call_id,
                "name": event.tool_name,
                "input": event.input_summary,
                "status": terminal.status if terminal is not None else "missing_terminal",
                "output": terminal.output_summary if terminal is not None else {},
                "confirmation_pending": (
                    terminal.attributes.get("confirmation_pending")
                    if terminal is not None
                    else None
                ),
            }
        )
    return executions


def _validation_results(events: list[TraceEvent]) -> list[dict[str, Any]]:
    return [
        {
            "tool_name": event.tool_name,
            "status": event.status,
            "tool_call_id": event.attributes.get("tool_call_id"),
        }
        for event in events
        if event.canonical_event == "action.validation.finished"
    ]


def _provider_result_kinds(events: list[TraceEvent]) -> list[str]:
    return [
        result_kind
        for event in events
        if event.canonical_event == "llm.chat.finished"
        and isinstance(
            result_kind := event.attributes.get("result_kind"),
            str,
        )
        and result_kind
    ]


def _total_latency_ms(events: list[TraceEvent]) -> int:
    terminal = next(
        (
            event
            for event in reversed(events)
            if event.canonical_event
            in {"run.completed", "run.failed", "run.cancelled"}
        ),
        None,
    )
    return (
        terminal.latency_ms
        if terminal is not None and terminal.latency_ms is not None
        else 0
    )


def _item_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _case_id(item: Any, metadata: dict[str, Any]) -> str:
    case_id = metadata.get("case_id")
    if isinstance(case_id, str) and case_id:
        return case_id
    item_id = _item_field(item, "id")
    if isinstance(item_id, str) and item_id:
        return item_id
    raise ValueError("Langfuse Dataset item is missing a stable case_id.")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]
