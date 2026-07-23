"""Langfuse-native Dataset and Experiment adapter for agent evaluations."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol

from langfuse import Evaluation
from pydantic import BaseModel, Field

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.eval.contracts import (
    AgentEvalEvidence,
    AgentEvalScore,
    evidence_from_runtime_state,
)
from assistant_agent.eval.evaluators.calendar_closed_loop import (
    CalendarClosedLoopCase,
    CalendarEventExpectation,
    evaluate_calendar_closed_loop,
)
from assistant_agent.eval.evaluators.capability_closed_loop import (
    CalendarReadClosedLoopCase,
    NoToolClosedLoopCase,
    evaluate_calendar_read_closed_loop,
    evaluate_no_tool_closed_loop,
)
from assistant_agent.eval.fixtures.calendar import (
    CalendarEvalCreateTool,
    CalendarEvalEnvironment,
    EvalCalendarEvent,
)
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.trace_context import RuntimeTraceContext
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.otel_exporter import (
    OtlpHttpTextExporterConfig,
    TextOtelTraceObserver,
    create_otlp_http_text_span_exporter,
)
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.tools.plugins.personal_assistant.tools import CalendarSearchTool
from assistant_agent.tools.registry import ToolRegistry


DEFAULT_DATASET_SOURCE = Path("evals/langfuse/calendar_closed_loop_v1.json")
ITEM_SCORE_NAMES = (
    "agent.strict_pass",
    "agent.goal_completion",
    "agent.tool_correctness",
    "agent.policy_compliance",
    "agent.state_integrity",
    "agent.response_grounding",
    "agent.tool_call_count",
    "agent.total_latency_ms",
)


class LangfuseDatasetSourceItem(BaseModel):
    """One version-controlled item synchronized into Langfuse."""

    id: str = Field(min_length=1)
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class LangfuseDatasetSource(BaseModel):
    """Version-controlled source for a Langfuse Dataset."""

    dataset_name: str = Field(min_length=1)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    items: list[LangfuseDatasetSourceItem] = Field(min_length=1)

    def content_hash(self) -> str:
        """Return the stable hash recorded on the Dataset and Experiment."""

        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class DatasetSyncResult(BaseModel):
    """Result of synchronizing the source file into Langfuse."""

    dataset_name: str
    dataset_hash: str
    item_ids: list[str]


class LangfuseExperimentClient(Protocol):
    """SDK surface needed by the project-owned experiment task."""

    def get_current_trace_id(self) -> str | None: ...

    def get_current_observation_id(self) -> str | None: ...


class RuntimeTraceObserver(Protocol):
    """Fail-fast Experiment sink for canonical Runtime TraceEvents."""

    def on_trace_event(self, event: Any) -> None: ...

    def close(self, *, timeout: float) -> bool: ...


class EvalStateEnvironment(Protocol):
    """State probe consumed by the framework-neutral experiment task."""

    def snapshot(self) -> dict[str, Any]: ...

    def diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]: ...


class _ScriptedCalendarChatAdapter:
    provider = "scripted"
    model = "scripted-calendar-eval"

    def __init__(self, event: CalendarEventExpectation, response_facts: list[str]) -> None:
        tool_input = {
            key: value
            for key, value in event.model_dump(mode="json").items()
            if value not in (None, [])
        }
        facts = "，".join(response_facts)
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="calendar-closed-loop-eval-call",
                            name="calendar_create",
                            arguments=tool_input,
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=f"已创建{event.title}，{facts}。",
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class _ScriptedDirectChatAdapter:
    provider = "scripted"
    model = "scripted-no-tool-eval"

    def __init__(self, response_facts: list[str]) -> None:
        self._result = ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="，".join(response_facts),
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return self._result


class _ScriptedCalendarReadChatAdapter:
    provider = "scripted"
    model = "scripted-calendar-read-eval"

    def __init__(self, case: CalendarReadClosedLoopCase) -> None:
        facts = "，".join(case.response_facts)
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="calendar-read-closed-loop-eval-call",
                            name="calendar_search",
                            arguments={"query": case.query},
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=facts,
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


@dataclass(frozen=True)
class CalendarRuntimeBundle:
    """Per-item isolated Runtime and state probe.

    The historical name remains as a compatibility alias for the first Calendar
    experiment, while the bundle now supports every stateful eval fixture.
    """

    runtime: AgentGraphRuntime
    environment: EvalStateEnvironment


EvalCase = CalendarClosedLoopCase | CalendarReadClosedLoopCase | NoToolClosedLoopCase
CalendarRuntimeFactory = Callable[
    [UserRequest, EvalCase],
    CalendarRuntimeBundle,
]


class AgentExperimentTask:
    """Langfuse task function that executes the real project Runtime."""

    def __init__(
        self,
        *,
        client: LangfuseExperimentClient,
        runtime_factory: CalendarRuntimeFactory | None = None,
        trace_observer: RuntimeTraceObserver | None = None,
    ) -> None:
        self.client = client
        self.runtime_factory = runtime_factory or build_scripted_agent_runtime
        self.trace_observer = trace_observer

    def __call__(self, *, item: Any, **_: Any) -> AgentEvalEvidence:
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
        case = eval_case_from_dataset_fields(
            expected_output=expected_output,
            metadata=metadata,
            case_id=_case_id(item, metadata),
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
        finally:
            bundle.runtime.close()
        final_state = bundle.environment.snapshot()
        evidence = evidence_from_runtime_state(
            case_id=case.id,
            state=state,
            trace_events=bundle.runtime.trace_store.list_by_run(state.run_id),
            initial_state=initial_state,
            final_state=final_state,
            state_diff=bundle.environment.diff(initial_state, final_state),
            runtime_metadata={
                "fixture": metadata.get("fixture"),
                "evaluator_version": metadata.get("evaluator_version"),
                "capability": metadata.get("capability"),
            },
        )
        if self.trace_observer is not None:
            for event in evidence.trace_events:
                self.trace_observer.on_trace_event(event)
        return evidence


def load_langfuse_dataset_source(
    path: Path | str = DEFAULT_DATASET_SOURCE,
) -> LangfuseDatasetSource:
    """Load and validate the version-controlled Dataset source."""

    return LangfuseDatasetSource.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def sync_langfuse_dataset(
    client: Any,
    source: LangfuseDatasetSource,
) -> DatasetSyncResult:
    """Idempotently upsert the Dataset and its stable item IDs."""

    dataset_hash = source.content_hash()
    client.create_dataset(
        name=source.dataset_name,
        description=source.description
        or "assistant_agent Calendar closed-loop evaluation dataset.",
        metadata={
            **source.metadata,
            "dataset_hash": dataset_hash,
        },
    )
    item_ids: list[str] = []
    for item in source.items:
        client.create_dataset_item(
            dataset_name=source.dataset_name,
            id=item.id,
            input=item.input,
            expected_output=item.expected_output,
            metadata={
                **item.metadata,
                "case_id": item.id,
                "dataset_hash": dataset_hash,
            },
        )
        item_ids.append(item.id)
    return DatasetSyncResult(
        dataset_name=source.dataset_name,
        dataset_hash=dataset_hash,
        item_ids=item_ids,
    )


def run_langfuse_agent_experiment(
    client: Any,
    source: LangfuseDatasetSource,
    *,
    experiment_name: str,
    run_name: str | None = None,
    max_concurrency: int = 1,
    metadata: dict[str, Any] | None = None,
    runtime_factory: CalendarRuntimeFactory | None = None,
    trace_observer: RuntimeTraceObserver | None = None,
) -> Any:
    """Run the synchronized Langfuse Dataset and return its ExperimentResult."""

    dataset = client.get_dataset(source.dataset_name)
    observer = trace_observer or create_required_eval_trace_observer()
    owns_observer = trace_observer is None
    try:
        return dataset.run_experiment(
            name=experiment_name,
            run_name=run_name,
            description="assistant_agent trace-and-state capability evaluation.",
            task=AgentExperimentTask(
                client=client,
                runtime_factory=runtime_factory,
                trace_observer=observer,
            ),
            evaluators=agent_item_evaluators(),
            run_evaluators=agent_run_evaluators(),
            max_concurrency=max_concurrency,
            metadata={
                "dataset_hash": source.content_hash(),
                "provider_mode": "mock",
                "chat_provider": "scripted",
                "evaluator_versions": sorted(
                    {
                        str(item.metadata.get("evaluator_version"))
                        for item in source.items
                    }
                ),
                **(metadata or {}),
            },
        )
    finally:
        if owns_observer and not observer.close(timeout=10.0):
            raise RuntimeError("Langfuse Experiment Runtime trace export did not close cleanly.")


def create_required_eval_trace_observer(
    env: Mapping[str, str] | None = None,
) -> TextOtelTraceObserver:
    """Create a synchronous, fail-fast OTLP observer for an explicit Experiment."""

    base_config = OtlpHttpTextExporterConfig.from_env(
        os.environ if env is None else env
    )
    config = replace(
        base_config,
        enabled=True,
        include_content=True,
    )
    setup = create_otlp_http_text_span_exporter(config)
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


def build_scripted_agent_runtime(
    _request: UserRequest,
    case: EvalCase,
) -> CalendarRuntimeBundle:
    """Build one deterministic rollout from the item's explicit evaluator version."""

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
    if isinstance(case, CalendarClosedLoopCase):
        registry.register(CalendarEvalCreateTool(environment))
        chat_adapter: Any = _ScriptedCalendarChatAdapter(
            case.required_event,
            case.response_facts,
        )
    elif isinstance(case, CalendarReadClosedLoopCase):
        registry.register(CalendarSearchTool(environment))
        chat_adapter = _ScriptedCalendarReadChatAdapter(case)
    else:
        registry.register(CalendarSearchTool(environment))
        registry.register(CalendarEvalCreateTool(environment))
        chat_adapter = _ScriptedDirectChatAdapter(case.response_facts)
    registry.seal()
    runtime = AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=chat_adapter,
        trace_store=InMemoryTraceStore(),
        session_store=InMemorySessionStore(),
    )
    return CalendarRuntimeBundle(runtime=runtime, environment=environment)


def calendar_case_from_dataset_fields(
    *,
    expected_output: dict[str, Any],
    metadata: dict[str, Any],
    case_id: str,
) -> CalendarClosedLoopCase:
    """Reconstruct project ground truth from Langfuse evaluator inputs."""

    return CalendarClosedLoopCase(
        id=case_id,
        required_event=CalendarEventExpectation.model_validate(
            expected_output.get("required_event")
        ),
        required_tools=_string_list(metadata.get("required_tools"))
        or ["calendar_create"],
        forbidden_tools=_string_list(metadata.get("forbidden_tools")),
        required_confirmation=_string_list(metadata.get("required_confirmation"))
        or ["calendar_create"],
        response_facts=_string_list(expected_output.get("response_facts")),
    )


def eval_case_from_dataset_fields(
    *,
    expected_output: dict[str, Any],
    metadata: dict[str, Any],
    case_id: str,
) -> EvalCase:
    """Dispatch ground truth only from the versioned Dataset contract."""

    evaluator_version = metadata.get("evaluator_version")
    if evaluator_version == "calendar_closed_loop_v1":
        return calendar_case_from_dataset_fields(
            expected_output=expected_output,
            metadata=metadata,
            case_id=case_id,
        )
    if evaluator_version == "no_tool_closed_loop_v1":
        return NoToolClosedLoopCase(
            id=case_id,
            forbidden_tools=_string_list(metadata.get("forbidden_tools")),
            response_facts=_string_list(expected_output.get("response_facts")),
        )
    if evaluator_version == "calendar_read_closed_loop_v1":
        raw_events = expected_output.get("expected_events")
        if not isinstance(raw_events, list):
            raise ValueError("Calendar read case requires expected_events.")
        return CalendarReadClosedLoopCase(
            id=case_id,
            query=str(expected_output.get("query") or ""),
            expected_events=[
                CalendarEventExpectation.model_validate(event)
                for event in raw_events
            ],
            required_tools=_string_list(metadata.get("required_tools"))
            or ["calendar_search"],
            forbidden_tools=_string_list(metadata.get("forbidden_tools"))
            or ["calendar_create"],
            response_facts=_string_list(expected_output.get("response_facts")),
        )
    raise ValueError(f"Unsupported evaluator_version: {evaluator_version!r}.")


def agent_item_evaluators() -> list[Callable[..., Evaluation]]:
    """Return one Langfuse SDK evaluator per stable cross-capability score."""

    return [_agent_item_evaluator(score_name) for score_name in ITEM_SCORE_NAMES]


def agent_run_evaluators() -> list[Callable[..., Evaluation]]:
    """Return Dataset Run aggregate evaluators."""

    return [
        _mean_run_evaluator("strict_pass_rate", "agent.strict_pass"),
        _mean_run_evaluator("goal_completion_mean", "agent.goal_completion"),
        _inverse_mean_run_evaluator(
            "policy_violation_rate",
            "agent.policy_compliance",
        ),
        _inverse_mean_run_evaluator(
            "state_integrity_failure_rate",
            "agent.state_integrity",
        ),
        _mean_run_evaluator(
            "response_grounding_mean",
            "agent.response_grounding",
        ),
        _mean_run_evaluator("tool_call_count_mean", "agent.tool_call_count"),
        _percentile_run_evaluator("latency_p50", "agent.total_latency_ms", 0.50),
        _percentile_run_evaluator("latency_p95", "agent.total_latency_ms", 0.95),
    ]


def _agent_item_evaluator(score_name: str) -> Callable[..., Evaluation]:
    def evaluator(
        *,
        output: Any,
        expected_output: Any,
        metadata: Any,
        **_: Any,
    ) -> Evaluation:
        evidence = AgentEvalEvidence.model_validate(output)
        expected = expected_output if isinstance(expected_output, dict) else {}
        item_metadata = metadata if isinstance(metadata, dict) else {}
        case = eval_case_from_dataset_fields(
            expected_output=expected,
            metadata=item_metadata,
            case_id=str(item_metadata.get("case_id") or evidence.case_id),
        )
        if isinstance(case, CalendarClosedLoopCase):
            report = evaluate_calendar_closed_loop(case, evidence)
        elif isinstance(case, CalendarReadClosedLoopCase):
            report = evaluate_calendar_read_closed_loop(case, evidence)
        else:
            report = evaluate_no_tool_closed_loop(case, evidence)
        score = report.score(score_name)
        return langfuse_evaluation_from_score(score)

    evaluator.__name__ = score_name.replace(".", "_")
    return evaluator


def langfuse_evaluation_from_score(score: AgentEvalScore) -> Evaluation:
    """Convert the project score into the native Langfuse Evaluation."""

    value: float | bool = (
        bool(score.value)
        if score.data_type == "BOOLEAN"
        else score.value
    )
    return Evaluation(
        name=score.name,
        value=value,
        comment=score.comment,
        metadata=score.evidence,
        data_type=score.data_type,
    )


def _mean_run_evaluator(
    result_name: str,
    item_score_name: str,
) -> Callable[..., Evaluation]:
    def evaluator(*, item_results: list[Any], **_: Any) -> Evaluation:
        values = _item_score_values(item_results, item_score_name)
        value = sum(values) / len(values) if values else 0.0
        return Evaluation(
            name=result_name,
            value=value,
            comment=f"{item_score_name} mean over {len(values)} item(s).",
        )

    evaluator.__name__ = result_name
    return evaluator


def _inverse_mean_run_evaluator(
    result_name: str,
    item_score_name: str,
) -> Callable[..., Evaluation]:
    mean_evaluator = _mean_run_evaluator(result_name, item_score_name)

    def evaluator(*, item_results: list[Any], **kwargs: Any) -> Evaluation:
        result = mean_evaluator(item_results=item_results, **kwargs)
        result.value = 1.0 - float(result.value)
        result.comment = f"1 - mean({item_score_name}) over {len(item_results)} item(s)."
        return result

    evaluator.__name__ = result_name
    return evaluator


def _percentile_run_evaluator(
    result_name: str,
    item_score_name: str,
    quantile: float,
) -> Callable[..., Evaluation]:
    def evaluator(*, item_results: list[Any], **_: Any) -> Evaluation:
        values = sorted(_item_score_values(item_results, item_score_name))
        if not values:
            value = 0.0
        else:
            index = max(0, min(len(values) - 1, int((len(values) - 1) * quantile + 0.5)))
            value = values[index]
        return Evaluation(
            name=result_name,
            value=value,
            comment=f"{item_score_name} p{int(quantile * 100)} over {len(values)} item(s).",
        )

    evaluator.__name__ = result_name
    return evaluator


def _item_score_values(item_results: list[Any], score_name: str) -> list[float]:
    values: list[float] = []
    for item_result in item_results:
        evaluations = getattr(item_result, "evaluations", None)
        if evaluations is None and isinstance(item_result, dict):
            evaluations = item_result.get("evaluations")
        for evaluation in evaluations or []:
            name = getattr(evaluation, "name", None)
            value = getattr(evaluation, "value", None)
            if isinstance(evaluation, dict):
                name = evaluation.get("name")
                value = evaluation.get("value")
            if name == score_name and isinstance(value, (int, float, bool)):
                values.append(float(value))
    return values


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


# Backward-compatible names retained for the Phase 1 Calendar-only callers.
CalendarExperimentTask = AgentExperimentTask
build_scripted_calendar_runtime = build_scripted_agent_runtime
calendar_item_evaluators = agent_item_evaluators
calendar_run_evaluators = agent_run_evaluators
run_langfuse_calendar_experiment = run_langfuse_agent_experiment
