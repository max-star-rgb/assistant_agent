"""Offline contract tests for the thin Langfuse Runtime task."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.cases.langfuse.experiment import (
    AgentExperimentTask,
    BEHAVIOR_DATASET_SEED,
    DEFAULT_DATASET_SEED,
    run_langfuse_agent_experiment,
)
from evals.cases.langfuse.contracts import (
    RealAgentCase,
    RuntimeBundle,
    StatelessEvalEnvironment,
)
from evals.cases.langfuse.dataset_sync import (
    failed_dataset_item_ids,
    load_dataset_seed,
    partition_available_dataset_item_ids,
    sync_langfuse_dataset,
)
from evals.cases.langfuse.evidence import (
    available_tools as _available_tools,
    tool_executions as _tool_executions,
)
from evals.cases.langfuse.runtime_profiles import (
    build_real_readonly_runtime,
)
from evals.cases.langfuse.weather_failure_fixture import (
    SimulatedWeatherFailureAdapter,
)
from evals.cases.langfuse.manifest import (
    load_eval_manifest,
    select_eval_item_ids,
)
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.decision_models import NativeToolCall
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.observability.trace_store import InMemoryTraceStore, TraceEvent
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    WeatherTool,
)
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from scripts.run_langfuse_agent_evals import _optional_run_name


TRACE_ID = "0123456789abcdef0123456789abcdef"
PARENT_SPAN_ID = "0123456789abcdef"


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.datasets: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.dataset = _FakeDataset()
        self.deleted_item_ids: list[str] = []
        self.api = SimpleNamespace(
            dataset_items=SimpleNamespace(delete=self.deleted_item_ids.append)
        )

    def create_dataset(self, **kwargs: Any) -> object:
        self.datasets.append(kwargs)
        return object()

    def create_dataset_item(self, **kwargs: Any) -> object:
        self.items.append(kwargs)
        return object()

    def get_dataset(self, name: str) -> "_FakeDataset":
        self.dataset.name = name
        return self.dataset

    def get_current_trace_id(self) -> str:
        return TRACE_ID

    def get_current_observation_id(self) -> str:
        return PARENT_SPAN_ID


class _FakeDataset:
    def __init__(self) -> None:
        self.name = ""
        self.items: list[Any] = []
        self.run_kwargs: dict[str, Any] = {}
        self.run_item_ids: list[str] = []
        self.run_records: list[dict[str, Any]] = []

    def run_experiment(self, **kwargs: Any) -> object:
        self.run_kwargs = kwargs
        self.run_item_ids = [str(item.id) for item in self.items]
        self.run_records.append(
            {
                "item_ids": self.run_item_ids,
                "kwargs": kwargs,
            }
        )
        return object()


class _FakeTraceObserver:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_trace_event(self, event: Any) -> None:
        self.events.append(event)

    def close(self, *, timeout: float) -> bool:
        return timeout > 0


class _TruncatedChat:
    provider = "scripted"
    model = "truncated-sentinel"

    def chat(self, _request: object) -> ChatResult:
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="length",
            response_text="partial-sentinel",
        )


class _WeatherFailureRecoveryChat:
    provider = "scripted"
    model = "weather-failure-recovery-sentinel"

    def __init__(self) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="weather-timeout-eval-call",
                            name="weather",
                            arguments={"location": "上海"},
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=(
                        "天气服务暂时超时，我无法确认明早的温度、降水或风力。"
                        "请出发前查看可靠天气来源；若仍无法确认，缩短路线并携带轻便雨具。"
                    ),
                ),
            ]
        )

    def chat(self, _request: object) -> ChatResult:
        return next(self._results)


def _truncated_runtime_factory(_request: object, _case: object) -> RuntimeBundle:
    registry = ToolRegistry()
    registry.seal()
    return RuntimeBundle(
        runtime=AgentGraphRuntime(
            registry=registry,
            config=ProviderConfig(langgraph_checkpointer_backend="none"),
            chat_adapter=_TruncatedChat(),
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        ),
        environment=StatelessEvalEnvironment(),
    )


def _weather_failure_runtime_factory(
    _request: object,
    case: object,
) -> RuntimeBundle:
    assert isinstance(case, RealAgentCase)
    assert case.weather_failure is not None
    registry = ToolRegistry()
    registry.register(
        WeatherTool(
            adapter=SimulatedWeatherFailureAdapter(case.weather_failure)
        )
    )
    registry.seal()
    return RuntimeBundle(
        runtime=AgentGraphRuntime(
            registry=registry,
            config=ProviderConfig(langgraph_checkpointer_backend="none"),
            chat_adapter=_WeatherFailureRecoveryChat(),
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        ),
        environment=StatelessEvalEnvironment(),
    )


class _ExplodingRuntime:
    def run_state(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("peer closed incomplete response")

    def close(self) -> None:
        return None


def _exploding_runtime_factory(_request: object, _case: object) -> RuntimeBundle:
    return RuntimeBundle(
        runtime=_ExplodingRuntime(),  # type: ignore[arg-type]
        environment=StatelessEvalEnvironment(),
    )


def _seed_item_from(seed_path: Path, case_id: str) -> dict[str, Any]:
    seed = load_dataset_seed(seed_path)
    item = next(item for item in seed.items if item.id == case_id)
    return {
        "id": item.id,
        "input": item.input,
        "expected_output": item.expected_output,
        "metadata": {**item.metadata, "case_id": item.id},
    }


def _seed_item(case_id: str) -> dict[str, Any]:
    return _seed_item_from(
        DEFAULT_DATASET_SEED,
        case_id,
    )


def test_explicit_seed_uses_stable_native_dataset_ids() -> None:
    seed = load_dataset_seed(DEFAULT_DATASET_SEED)
    client = _FakeLangfuseClient()

    result = sync_langfuse_dataset(client, seed)

    assert result.dataset_name == "assistant-agent-infrastructure-v1"
    assert result.seed_hash.startswith("sha256:")
    assert result.item_ids == [
        "agent_v1_daily_simple_015_create_dentist_event",
        "agent_v1_daily_simple_017_polite_message_no_tool",
        "agent_v1_daily_simple_002_calendar_read_team_sync",
    ]
    assert client.datasets[0]["metadata"]["seed_hash"] == result.seed_hash
    assert client.items[0]["metadata"]["case_id"] == result.item_ids[0]


def test_explicit_seed_removes_obsolete_seed_managed_items() -> None:
    seed = load_dataset_seed(DEFAULT_DATASET_SEED)
    client = _FakeLangfuseClient()
    client.dataset.items = [
        SimpleNamespace(
            id="obsolete-confirmation-case",
            metadata={
                "case_id": "obsolete-confirmation-case",
                "seed_hash": "sha256:old",
                "capability": "real_confirmation_guard",
            },
        ),
        SimpleNamespace(
            id="ui-authored-case",
            metadata={"case_id": "ui-authored-case"},
        ),
    ]

    result = sync_langfuse_dataset(client, seed)

    assert result.removed_item_ids == ["obsolete-confirmation-case"]
    assert client.deleted_item_ids == ["obsolete-confirmation-case"]


def test_behavior_seed_includes_controlled_weather_failure_recovery() -> None:
    seed = load_dataset_seed(BEHAVIOR_DATASET_SEED)

    assert seed.dataset_name == "assistant-agent-behavior-v2"
    assert len(seed.items) == 18
    failure_item = next(
        item
        for item in seed.items
        if item.metadata["capability"] == "tool_failure_recovery"
    )
    assert failure_item.metadata["dependency_mode"] == "simulated"
    assert failure_item.metadata["expected_tool_terminal"] == "tool.failed"
    assert failure_item.metadata["compatible_profiles"] == [
        "real_readonly",
        "real_system",
    ]
    assert failure_item.expected_output["weather_failure"]["error_code"] == (
        "provider_timeout"
    )
    assert "不得编造" in failure_item.input["evaluation_criteria"]


def test_weather_timeout_case_produces_scorable_degraded_runtime_evidence() -> None:
    output = AgentExperimentTask(
        client=_FakeLangfuseClient(),
        runtime_factory=_weather_failure_runtime_factory,
    )(
        item=_seed_item_from(
            BEHAVIOR_DATASET_SEED,
            "agent_real_v1_weather_timeout_running_recovery",
        )
    )

    assert output.terminal_status == "completed"
    assert output.available_tools == ["weather"]
    assert len(output.tool_executions) == 1
    execution = output.tool_executions[0]
    assert execution["name"] == "weather"
    assert execution["status"] == "failed"
    assert execution["terminal_event"] == "tool.failed"
    assert execution["error_code"] == "provider_timeout"
    assert execution["retry_count"] >= 0
    assert output.response is not None
    assert output.response["data"]["degraded"] is True
    assert output.response["data"]["handled_tool_failures"] == 1
    assert "无法确认" in output.response["message"]


def test_behavior_seed_covers_production_like_capabilities() -> None:
    seed = load_dataset_seed(BEHAVIOR_DATASET_SEED)

    assert seed.dataset_name == "assistant-agent-behavior-v2"
    assert len(seed.items) == 18
    assert {
        item.metadata["capability"] for item in seed.items
    } == {
        "direct_response",
        "clarification",
        "weather_advice",
        "tool_failure_recovery",
        "calendar_read",
        "file_read",
        "shopping_search",
        "shopping_list_search",
        "web_search",
        "web_fetch",
        "media_understanding",
        "image_generation",
        "multi_tool_planning",
        "calendar_write",
    }
    assert all(
        "real_system" in item.metadata["compatible_profiles"]
        for item in seed.items
    )
    required_tools = {
        tool
        for item in seed.items
        for tool in item.metadata.get("required_tools", [])
    }
    assert required_tools == {
        "calendar_create",
        "calendar_search",
        "file_read",
        "image_generation",
        "shopping_search",
        "shopping_list_search",
        "media_inspect",
        "weather",
        "web_fetch",
        "web_search",
    }
    write_case = next(
        item
        for item in seed.items
        if item.metadata["capability"] == "calendar_write"
    )
    assert write_case.input["user_request"]["metadata"] == {
        "tool_visibility": {"enabled_tools": ["calendar_create"]}
    }
    assert all(item.input.get("evaluation_criteria") for item in seed.items)
    checklist_case = next(
        item
        for item in seed.items
        if item.id == "agent_system_v1_no_tool_trip_checklist"
    )
    assert "不得假设用户未提供的地点、日期、天气" in (
        checklist_case.input["evaluation_criteria"]
    )


def test_eval_manifest_indexes_profiles_suites_and_seed_capabilities() -> None:
    manifest = load_eval_manifest()

    assert set(manifest.profiles) == {
        "scripted_mock",
        "real_readonly",
        "real_system",
    }
    assert (
        manifest.suites["failure_recovery"].default_profile
        == "real_readonly"
    )
    assert manifest.suites["failure_recovery"].capabilities == [
        "tool_failure_recovery"
    ]
    for dataset in manifest.datasets.values():
        seed = load_dataset_seed(dataset.seed_source)
        assert seed.dataset_name == dataset.dataset_name
        assert {
            item.metadata["capability"] for item in seed.items
        } <= set(manifest.capabilities)
    behavior_ids = {
        item.id for item in load_dataset_seed(BEHAVIOR_DATASET_SEED).items
    }
    assert set(manifest.case_id_aliases.values()) <= behavior_ids
    assert not set(manifest.case_id_aliases) & behavior_ids


def test_eval_manifest_selects_suite_case_and_capability_by_intersection() -> None:
    manifest = load_eval_manifest()
    seed = load_dataset_seed(BEHAVIOR_DATASET_SEED)
    failure_case_id = "agent_real_v1_weather_timeout_running_recovery"

    assert select_eval_item_ids(
        seed.items,
        manifest=manifest,
        suite_name="failure_recovery",
        profile_name="real_readonly",
    ) == [failure_case_id]
    assert select_eval_item_ids(
        seed.items,
        manifest=manifest,
        suite_name="readonly_smoke",
        profile_name="real_readonly",
        case_ids=["agent_real_v1_daily_simple_001_commute_weather"],
    ) == ["agent_system_v1_weather_commute"]
    assert select_eval_item_ids(
        [
            SimpleNamespace(
                id="legacy-failure-case",
                metadata={"capability": "real_tool_failure_recovery"},
            )
        ],
        manifest=manifest,
        suite_name="failure_recovery",
        profile_name="real_readonly",
    ) == ["legacy-failure-case"]
    assert select_eval_item_ids(
        seed.items,
        manifest=manifest,
        suite_name="readonly_smoke",
        profile_name="real_readonly",
        case_ids=[failure_case_id],
        capabilities=["tool_failure_recovery"],
    ) == [failure_case_id]
    with pytest.raises(ValueError, match="did not match"):
        select_eval_item_ids(
            seed.items,
            manifest=manifest,
            suite_name="readonly_smoke",
            profile_name="real_readonly",
            case_ids=[failure_case_id],
            capabilities=["weather_advice"],
        )
    with pytest.raises(ValueError, match="incompatible"):
        select_eval_item_ids(
            seed.items,
            manifest=manifest,
            suite_name="system_full",
            profile_name="real_readonly",
        )


def test_real_readonly_runtime_fails_closed_in_mock_mode() -> None:
    seed = load_dataset_seed(BEHAVIOR_DATASET_SEED)
    item = seed.items[0]

    try:
        AgentExperimentTask(
            client=_FakeLangfuseClient(),
            runtime_factory=lambda request, case: build_real_readonly_runtime(
                request,
                case,
                config=ProviderConfig(),
            ),
        )(
            item={
                "id": item.id,
                "input": item.input,
                "expected_output": item.expected_output,
                "metadata": {**item.metadata, "case_id": item.id},
            }
        )
    except RuntimeError as exc:
        assert "MULTIMODAL_AGENT_PROVIDER_MODE=real" in str(exc)
    else:
        raise AssertionError("mock mode must not run a real-readonly eval")


def test_runtime_task_exposes_truncated_provider_result_for_native_score() -> None:
    seed = load_dataset_seed(BEHAVIOR_DATASET_SEED)
    item = seed.items[0]

    output = AgentExperimentTask(
        client=_FakeLangfuseClient(),
        runtime_factory=_truncated_runtime_factory,
    )(
        item={
            "id": item.id,
            "input": item.input,
            "expected_output": item.expected_output,
            "metadata": {**item.metadata, "case_id": item.id},
        }
    )

    assert output.terminal_status == "completed"
    assert output.provider_result_kinds == ["truncated"]


def test_real_runtime_exception_becomes_scorable_failed_output() -> None:
    seed = load_dataset_seed(BEHAVIOR_DATASET_SEED)
    item = seed.items[0]

    output = AgentExperimentTask(
        client=_FakeLangfuseClient(),
        runtime_factory=_exploding_runtime_factory,
        contain_runtime_errors=True,
    )(
        item={
            "id": item.id,
            "input": item.input,
            "expected_output": item.expected_output,
            "metadata": {**item.metadata, "case_id": item.id},
        }
    )

    assert output.terminal_status == "failed"
    assert output.provider_result_kinds == ["error"]
    assert output.execution_error == {
        "code": "eval_runtime_exception",
        "message": "RuntimeError: peer closed incomplete response",
    }


def test_runtime_task_returns_compact_code_evaluator_evidence() -> None:
    observer = _FakeTraceObserver()

    output = AgentExperimentTask(
        client=_FakeLangfuseClient(),
        trace_observer=observer,
    )(item=_seed_item("agent_v1_daily_simple_015_create_dentist_event"))

    assert output.schema_version == "agent_experiment_output_v1"
    assert output.trace_id == TRACE_ID
    assert output.terminal_status == "completed"
    assert output.state_diff["added"][0]["title"] == "洗牙"
    assert output.tool_executions[0]["name"] == "calendar_create"
    assert output.tool_executions[0]["status"] == "succeeded"
    assert output.tool_executions[0]["terminal_event"] == "tool.finished"
    assert output.validation_results[0]["status"] == "accepted"
    assert output.response is not None
    assert "静安牙科诊所" in output.response["message"]
    assert "trace.content" in output.trace_event_names

    root_span = build_text_otel_span_specs(observer.events)[0]
    assert root_span.trace_id == TRACE_ID
    assert root_span.parent_span_id == PARENT_SPAN_ID


def test_code_evaluator_keeps_semantic_expectations_out_of_mechanical_score() -> None:
    source = Path(
        "evals/cases/langfuse/evaluators/agent_strict_pass.ts"
    ).read_text(
        encoding="utf-8"
    )
    tool_checks = source.split("const toolChecks = {", maxsplit=1)[1].split(
        "\n  };", maxsplit=1
    )[0]

    assert "executed_tools_exposed:" in tool_checks
    assert "validation_chain_accepted:" in tool_checks
    assert "executions_reached_terminal:" in tool_checks
    assert "tool_trace_complete:" in tool_checks
    assert "required_tools" not in tool_checks
    assert "forbidden_tools" not in tool_checks
    assert "no_tool_called" not in tool_checks


def test_evaluator_manifest_covers_both_active_datasets_and_all_scores() -> None:
    manifest_path = Path(
        "evals/cases/langfuse/evaluators/evaluator_manifest_v1.json"
    )
    evaluator_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert evaluator_manifest["dataset_rules"] == [
        "assistant-agent-infrastructure-v1",
        "assistant-agent-behavior-v2",
    ]
    scores = {
        score
        for evaluator in evaluator_manifest["evaluators"]
        for score in evaluator["scores"]
    }
    assert scores == {
        "agent.runtime_trace_pass",
        "agent.tool_mechanical_pass",
        "agent.tool_semantic_pass",
        "agent.answer_semantic_pass",
    }
    code_evaluator = evaluator_manifest["evaluators"][0]
    assert Path(code_evaluator["source"]).is_file()
    semantic_evaluators = [
        evaluator
        for evaluator in evaluator_manifest["evaluators"]
        if evaluator["kind"] == "llm_as_a_judge"
    ]
    assert len(semantic_evaluators) == 2
    assert all(
        evaluator["model_requirements"] == {
            "structured_output": True,
            "model_params": {
                "providerOptions": {
                    "anthropic": {
                        "thinking": {"type": "disabled"},
                    }
                }
            },
        }
        for evaluator in semantic_evaluators
    )


def test_code_evaluator_keeps_failed_tool_outcome_out_of_mechanical_score() -> None:
    source = Path(
        "evals/cases/langfuse/evaluators/agent_strict_pass.ts"
    ).read_text(
        encoding="utf-8"
    )
    tool_checks = source.split("const toolChecks = {", maxsplit=1)[1].split(
        "\n  };", maxsplit=1
    )[0]

    assert '"tool.finished", "tool.failed"' in tool_checks
    assert 'execution.status === "succeeded"' not in tool_checks
    assert "execution.outcome" not in tool_checks


def test_eval_available_tools_falls_back_to_context_report() -> None:
    event = TraceEvent(
        trace_id=TRACE_ID,
        run_id="run-context-catalog",
        node_name="assistant",
        event_type="observability",
        canonical_event="context.build.finished",
        output_summary={
            "context_report_v1": {
                "selected_tool_names": ["web_search", "web_fetch"],
            }
        },
    )

    assert _available_tools(
        SimpleNamespace(
            run_tool_catalog=SimpleNamespace(available_tool_names=[]),
        ),
        [event],
    ) == ["web_search", "web_fetch"]


def test_eval_tool_exposure_uses_the_context_for_each_execution() -> None:
    events = [
        TraceEvent(
            trace_id=TRACE_ID,
            run_id="run-per-call-catalog",
            node_name="assistant",
            event_type="observability",
            canonical_event="context.build.finished",
            output_summary={
                "context_report_v1": {
                    "selected_tool_names": ["weather", "shopping_search"],
                }
            },
        ),
        TraceEvent(
            trace_id=TRACE_ID,
            run_id="run-per-call-catalog",
            node_name="execute_tool",
            event_type="observability",
            canonical_event="tool.started",
            tool_name="weather",
            status="started",
            attributes={"tool_call_id": "call-weather"},
        ),
        TraceEvent(
            trace_id=TRACE_ID,
            run_id="run-per-call-catalog",
            node_name="execute_tool",
            event_type="observability",
            canonical_event="tool.finished",
            tool_name="weather",
            status="succeeded",
            attributes={"tool_call_id": "call-weather"},
        ),
        TraceEvent(
            trace_id=TRACE_ID,
            run_id="run-per-call-catalog",
            node_name="assistant",
            event_type="observability",
            canonical_event="context.build.finished",
            output_summary={
                "context_report_v1": {
                    "selected_tool_names": [],
                }
            },
        ),
    ]

    executions = _tool_executions(events)

    assert executions[0]["exposed"] is True
    assert executions[0]["exposed_tools"] == ["weather", "shopping_search"]
    assert _available_tools(
        SimpleNamespace(
            run_tool_catalog=SimpleNamespace(available_tool_names=[]),
        ),
        events,
    ) == ["weather", "shopping_search"]


def test_runtime_task_covers_no_tool_and_read_only_capabilities() -> None:
    cases = {
        "agent_v1_daily_simple_017_polite_message_no_tool": [],
        "agent_v1_daily_simple_002_calendar_read_team_sync": ["calendar_search"],
    }

    for case_id, expected_tools in cases.items():
        output = AgentExperimentTask(client=_FakeLangfuseClient())(
            item=_seed_item(case_id)
        )

        assert output.terminal_status == "completed"
        assert [
            execution["name"] for execution in output.tool_executions
        ] == expected_tools
        assert output.initial_state == output.final_state


def test_experiment_wires_task_without_project_evaluators() -> None:
    client = _FakeLangfuseClient()

    result = run_langfuse_agent_experiment(
        client,
        dataset_name="dataset-sentinel",
        experiment_name="experiment-sentinel",
        run_name="run-sentinel",
        trace_observer=_FakeTraceObserver(),
    )

    assert result is not None
    assert client.dataset.name == "dataset-sentinel"
    assert client.dataset.run_kwargs["name"] == "experiment-sentinel"
    assert client.dataset.run_kwargs["run_name"] == "run-sentinel"
    assert isinstance(client.dataset.run_kwargs["task"], AgentExperimentTask)
    assert "evaluators" not in client.dataset.run_kwargs
    assert "run_evaluators" not in client.dataset.run_kwargs
    assert (
        client.dataset.run_kwargs["metadata"]["evaluation_owner"]
        == "langfuse_native_evaluators"
    )
    assert client.dataset.run_kwargs["metadata"]["evaluation_methods"] == [
        "code",
        "llm_as_a_judge",
    ]
    assert client.dataset.run_kwargs["metadata"]["deterministic_score_names"] == [
        "agent.runtime_trace_pass",
        "agent.tool_mechanical_pass",
    ]
    assert client.dataset.run_kwargs["metadata"]["semantic_score_names"] == [
        "agent.tool_semantic_pass",
        "agent.answer_semantic_pass",
    ]


def test_experiment_runs_only_selected_dataset_items() -> None:
    client = _FakeLangfuseClient()
    client.dataset.items = [
        SimpleNamespace(id="case-pass"),
        SimpleNamespace(id="case-fail"),
    ]

    run_langfuse_agent_experiment(
        client,
        dataset_name="dataset-sentinel",
        experiment_name="experiment-sentinel",
        trace_observer=_FakeTraceObserver(),
        dataset_item_ids=["case-fail"],
    )

    assert client.dataset.run_records[0]["item_ids"] == ["case-fail"]
    metadata = client.dataset.run_records[0]["kwargs"]["metadata"]
    assert metadata["dataset_item_count"] == 1
    assert metadata["dataset_selection_mode"] == "explicit_item_ids"
    assert "dataset_item_ids" not in metadata
    # 子集运行不修改 client 缓存的完整 Dataset。
    assert [item.id for item in client.dataset.items] == [
        "case-pass",
        "case-fail",
    ]


def test_full_experiment_metadata_uses_propagation_safe_dataset_summary() -> None:
    client = _FakeLangfuseClient()
    client.dataset.items = [
        SimpleNamespace(id=f"case-{index}-with-a-long-identifier")
        for index in range(20)
    ]

    run_langfuse_agent_experiment(
        client,
        dataset_name="dataset-sentinel",
        experiment_name="experiment-sentinel",
        trace_observer=_FakeTraceObserver(),
    )

    metadata = client.dataset.run_records[0]["kwargs"]["metadata"]
    assert metadata["dataset_item_count"] == 20
    assert metadata["dataset_selection_mode"] == "full"
    assert "dataset_item_ids" not in metadata


def test_experiment_rejects_unavailable_selected_dataset_item() -> None:
    client = _FakeLangfuseClient()
    client.dataset.items = [SimpleNamespace(id="case-available")]

    with pytest.raises(ValueError, match="case-unavailable"):
        run_langfuse_agent_experiment(
            client,
            dataset_name="dataset-sentinel",
            experiment_name="experiment-sentinel",
            trace_observer=_FakeTraceObserver(),
            dataset_item_ids=["case-unavailable"],
        )

    assert client.dataset.run_records == []


def test_rerun_failed_from_none_disables_dataset_filter() -> None:
    assert _optional_run_name("none") is None
    assert _optional_run_name(" NONE ") is None
    assert _optional_run_name("run-sentinel") == "run-sentinel"


def test_historical_failed_items_missing_from_current_dataset_are_skipped() -> None:
    dataset = SimpleNamespace(
        items=[
            SimpleNamespace(id="case-current-fail"),
            SimpleNamespace(id="case-current-pass"),
        ]
    )

    selected, unavailable = partition_available_dataset_item_ids(
        dataset,
        ["case-obsolete-fail", "case-current-fail"],
    )

    assert selected == ["case-current-fail"]
    assert unavailable == ["case-obsolete-fail"]


def test_failed_dataset_items_use_latest_explicit_boolean_scores() -> None:
    now = datetime.now(UTC)
    run_items = [
        SimpleNamespace(dataset_item_id="case-fixed", trace_id="trace-fixed"),
        SimpleNamespace(dataset_item_id="case-fail", trace_id="trace-fail"),
        SimpleNamespace(dataset_item_id="case-missing", trace_id="trace-missing"),
    ]
    scores = [
        SimpleNamespace(
            trace_id="trace-fixed",
            name="agent.runtime_trace_pass",
            data_type="BOOLEAN",
            value=0,
            timestamp=now,
        ),
        SimpleNamespace(
            trace_id="trace-fixed",
            name="agent.runtime_trace_pass",
            data_type="BOOLEAN",
            value=1,
            timestamp=now + timedelta(seconds=1),
        ),
        SimpleNamespace(
            trace_id="trace-fail",
            name="agent.answer_semantic_pass",
            data_type="BOOLEAN",
            value=0,
            timestamp=now,
        ),
        SimpleNamespace(
            trace_id="trace-missing",
            name="unrelated-score",
            data_type="BOOLEAN",
            value=0,
            timestamp=now,
        ),
    ]
    score_queries: list[dict[str, Any]] = []

    def get_scores(**kwargs: Any) -> SimpleNamespace:
        score_queries.append(kwargs)
        return SimpleNamespace(
            data=[
                score
                for score in scores
                if score.trace_id == kwargs["trace_id"]
            ],
            meta=SimpleNamespace(total_pages=1),
        )

    client = SimpleNamespace(
        get_dataset_run=lambda **_: SimpleNamespace(
            id="dataset-run-sentinel",
            dataset_run_items=run_items,
        ),
        api=SimpleNamespace(
            scores=SimpleNamespace(
                get_many=get_scores
            )
        ),
    )

    result = failed_dataset_item_ids(
        client,
        dataset_name="dataset-sentinel",
        run_name="run-sentinel",
        score_names={
            "agent.runtime_trace_pass",
            "agent.answer_semantic_pass",
        },
    )

    assert result == ["case-fail"]
    assert [query["trace_id"] for query in score_queries] == [
        "trace-fixed",
        "trace-fail",
        "trace-missing",
    ]
    assert all("dataset_run_id" not in query for query in score_queries)


def test_real_experiment_metadata_is_explicit_without_running_provider() -> None:
    client = _FakeLangfuseClient()

    run_langfuse_agent_experiment(
        client,
        dataset_name="real-dataset-sentinel",
        experiment_name="real-experiment-sentinel",
        runtime_factory=lambda request, case: build_real_readonly_runtime(
            request,
            case,
            config=ProviderConfig(),
        ),
        trace_observer=_FakeTraceObserver(),
        execution_profile="real_readonly",
        metadata={"chat_provider": "provider-sentinel"},
    )

    assert client.dataset.run_kwargs["metadata"]["provider_mode"] == "real"
    assert (
        client.dataset.run_kwargs["metadata"]["execution_profile"]
        == "real_readonly"
    )
    assert (
        client.dataset.run_kwargs["metadata"]["chat_provider"]
        == "provider-sentinel"
    )
    assert client.dataset.run_kwargs["metadata"]["evaluation_methods"] == [
        "code",
        "llm_as_a_judge",
    ]
    assert client.dataset.run_kwargs["metadata"]["semantic_score_names"] == [
        "agent.tool_semantic_pass",
        "agent.answer_semantic_pass",
    ]
    assert "真实 Chat Provider" in client.dataset.run_kwargs["description"]
    assert (
        "真实 Chat Provider"
        in client.dataset.run_kwargs["metadata"]["evaluation_objective"]
    )


def test_real_system_experiment_metadata_is_explicit_without_running_provider() -> None:
    client = _FakeLangfuseClient()

    run_langfuse_agent_experiment(
        client,
        dataset_name="real-system-dataset-sentinel",
        experiment_name="real-system-experiment-sentinel",
        runtime_factory=lambda request, case: build_real_readonly_runtime(
            request,
            case,
            config=ProviderConfig(),
        ),
        trace_observer=_FakeTraceObserver(),
        execution_profile="real_system",
        metadata={"chat_provider": "provider-sentinel"},
    )

    assert client.dataset.run_kwargs["metadata"]["provider_mode"] == "real"
    assert (
        client.dataset.run_kwargs["metadata"]["execution_profile"]
        == "real_system"
    )
    assert client.dataset.run_kwargs["metadata"]["evaluation_methods"] == [
        "code",
        "llm_as_a_judge",
    ]
    assert client.dataset.run_kwargs["metadata"]["semantic_score_names"] == [
        "agent.tool_semantic_pass",
        "agent.answer_semantic_pass",
    ]
    assert "多工具自主执行" in client.dataset.run_kwargs["description"]
