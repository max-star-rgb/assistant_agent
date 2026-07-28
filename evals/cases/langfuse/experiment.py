"""Thin AgentRuntime task for Langfuse-native Dataset experiments.

The project executes the Agent and returns structured evidence. Langfuse owns
the Dataset, evaluator execution, Scores, and Experiment comparison.
"""

from __future__ import annotations

import os
from copy import copy
from dataclasses import replace
from typing import Any, Collection, Literal, Mapping

from assistant_agent.runtime.requests import UserRequest
from assistant_agent.observability.trace_context import RuntimeTraceContext
from assistant_agent.identifiers import new_run_id
from assistant_agent.observability.otel_exporter import (
    OtlpHttpTextExporterConfig,
    TextOtelTraceObserver,
    create_otlp_http_text_span_exporter,
)
from assistant_agent.providers.provider_errors import sanitize_error_message
from evals.cases.langfuse.manifest import load_eval_manifest
from evals.cases.langfuse.contracts import (
    AgentExperimentOutput,
    LangfuseExperimentClient,
    RuntimeFactory,
    RuntimeTraceObserver,
)
from evals.cases.langfuse.evidence import (
    available_tools as _available_tools,
    provider_result_kinds as _provider_result_kinds,
    tool_executions as _tool_executions,
    total_latency_ms as _total_latency_ms,
    validation_results as _validation_results,
)
from evals.cases.langfuse.runtime_profiles import (
    build_scripted_runtime,
    case_from_dataset_fields,
)


EVAL_MANIFEST = load_eval_manifest()
INFRASTRUCTURE_DATASET = EVAL_MANIFEST.datasets["infrastructure"]
BEHAVIOR_DATASET = EVAL_MANIFEST.datasets["behavior"]
DEFAULT_DATASET_NAME = INFRASTRUCTURE_DATASET.dataset_name
DEFAULT_DATASET_SEED = INFRASTRUCTURE_DATASET.seed_source
BEHAVIOR_DATASET_NAME = BEHAVIOR_DATASET.dataset_name
BEHAVIOR_DATASET_SEED = BEHAVIOR_DATASET.seed_source
DETERMINISTIC_SCORE_NAMES = tuple(
    EVAL_MANIFEST.score_names.deterministic
)
REAL_AGENT_SEMANTIC_SCORE_NAMES = tuple(
    EVAL_MANIFEST.score_names.semantic
)
REAL_READONLY_SEMANTIC_SCORE_NAMES = REAL_AGENT_SEMANTIC_SCORE_NAMES
REAL_SYSTEM_SEMANTIC_SCORE_NAMES = REAL_AGENT_SEMANTIC_SCORE_NAMES
AGENT_EVALUATION_OBJECTIVE = (
    "验证 Agent 是否在受治理的 Runtime 中正确完成、克制或只读执行任务，并由 Tool Trace、"
    "Policy、环境状态变化和最终回答共同证明结果。当前 scripted mock 实验用于验证闭环评测"
    "基础设施，不代表真实模型的泛化能力。"
)
REAL_READONLY_EVALUATION_OBJECTIVE = (
    "验证真实 Chat Provider 是否在受治理的 Runtime 中正确克制、调用真实只读 Tool，"
    "或在受控只读依赖失败后诚实降级，并由 Tool Trace、Policy、Provider 终态和最终回答"
    "共同证明结果。该 profile 不执行写操作，不接入真实 Memory。"
)
REAL_SYSTEM_EVALUATION_OBJECTIVE = (
    "使用真实 Chat Provider 和当前已配置的真实能力，系统评估 Agent 的无工具克制、"
    "必要澄清、单工具与多工具自主执行、媒体理解、内容生成和本地写操作。"
    "动态结果以 Tool Trace、Provider 终态、最终回答和 Langfuse 原生评分共同证明；"
    "日历写入本地 SQLite，不调用 Google Calendar MCP，也不写入真实 Memory。"
)


class AgentExperimentTask:
    """Execute one Dataset item through the real governed Runtime."""

    def __init__(
        self,
        *,
        client: LangfuseExperimentClient,
        runtime_factory: RuntimeFactory | None = None,
        trace_observer: RuntimeTraceObserver | None = None,
        contain_runtime_errors: bool = False,
    ) -> None:
        self.client = client
        self.runtime_factory = runtime_factory or build_scripted_runtime
        self.trace_observer = trace_observer
        self.contain_runtime_errors = contain_runtime_errors

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
        except Exception as exc:
            if not self.contain_runtime_errors:
                raise
            message = sanitize_error_message(exc)
            final_state = bundle.environment.snapshot()
            return AgentExperimentOutput(
                case_id=case_id,
                run_id=new_run_id(),
                trace_id=trace_id,
                terminal_status="failed",
                response={
                    "message": f"评测执行失败：{message}",
                    "data": {"error_code": "eval_runtime_exception"},
                },
                request_metadata=dict(request.metadata),
                initial_state=initial_state,
                final_state=final_state,
                state_diff=bundle.environment.diff(initial_state, final_state),
                provider_result_kinds=["error"],
                execution_error={
                    "code": "eval_runtime_exception",
                    "message": message,
                },
            )
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
            available_tools=_available_tools(state, trace_events),
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
    execution_profile: Literal[
        "scripted_mock",
        "real_readonly",
        "real_system",
    ] = "scripted_mock",
    dataset_item_ids: Collection[str] | None = None,
) -> Any:
    """Run the Agent task; Langfuse-native rules evaluate it asynchronously."""

    dataset = client.get_dataset(dataset_name)
    selected_item_ids = set(dataset_item_ids or ())
    if dataset_item_ids is not None:
        available_item_ids = {str(item.id) for item in dataset.items}
        missing_item_ids = selected_item_ids - available_item_ids
        if missing_item_ids:
            raise ValueError(
                "Dataset items are unavailable: "
                + ", ".join(sorted(missing_item_ids))
            )
        if not selected_item_ids:
            raise ValueError("At least one Dataset item must be selected.")
        dataset = copy(dataset)
        dataset.items = [
            item for item in dataset.items if str(item.id) in selected_item_ids
        ]
    observer = trace_observer or create_required_eval_trace_observer()
    owns_observer = trace_observer is None
    evaluation_objective = {
        "scripted_mock": AGENT_EVALUATION_OBJECTIVE,
        "real_readonly": REAL_READONLY_EVALUATION_OBJECTIVE,
        "real_system": REAL_SYSTEM_EVALUATION_OBJECTIVE,
    }[execution_profile]
    evaluation_methods = ["code", "llm_as_a_judge"]
    semantic_score_names = list(REAL_AGENT_SEMANTIC_SCORE_NAMES)
    dataset_selection_mode = (
        "explicit_item_ids" if dataset_item_ids is not None else "full"
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
                contain_runtime_errors=execution_profile
                in {"real_readonly", "real_system"},
            ),
            max_concurrency=max_concurrency,
            metadata={
                "provider_mode": (
                    "real"
                    if execution_profile in {"real_readonly", "real_system"}
                    else "mock"
                ),
                "chat_provider": (
                    "configured"
                    if execution_profile in {"real_readonly", "real_system"}
                    else "scripted"
                ),
                "execution_profile": execution_profile,
                "evaluation_objective": evaluation_objective,
                "evaluation_owner": "langfuse_native_evaluators",
                "evaluation_methods": evaluation_methods,
                "deterministic_score_names": list(DETERMINISTIC_SCORE_NAMES),
                "semantic_score_names": semantic_score_names,
                "dataset_item_count": len(dataset.items),
                "dataset_selection_mode": dataset_selection_mode,
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
