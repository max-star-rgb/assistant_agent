"""Shared, behavior-neutral support for controlled Agent eval Tasks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.trace_context import RuntimeTraceContext
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import (
    AssertionResult,
    RunEvidence,
    TaskExecution,
    TaskSpec,
    ToolOutcomeExpectation,
)
from evals.agent.evidence import (
    available_tools,
    provider_result_kinds,
    tool_executions,
    validation_results,
)
from evals.agent.grading import rule_assertion
from evals.agent.provider_gate import validate_real_chat_config


StateReader = Callable[[AgentGraphRuntime, Any], dict[str, Any]]
BeforeRun = Callable[[AgentGraphRuntime], None]


def build_controlled_registry(
    *,
    replacements: Mapping[str, Tool] | None = None,
    config: ProviderConfig | None = None,
) -> ToolRegistry:
    """Build the complete offline-safe catalog and replace selected dependencies."""

    source = create_default_registry(
        config or ProviderConfig(provider_mode="mock"),
        plugin_modules=[],
    )
    replacement_by_name = dict(replacements or {})
    unknown = set(replacement_by_name) - set(source.list())
    if unknown:
        raise ValueError(
            f"Controlled replacements are not registered: {sorted(unknown)}"
        )
    registry = ToolRegistry()
    for name in source.list():
        registry.register(
            replacement_by_name.get(name, source.get(name)),
            source.registration_record(name),
        )
    registry.seal(assembly_report=source.assembly_report)
    return registry


def outcome_expectations(
    registry: ToolRegistry,
    *,
    required_successes: tuple[str, ...] = (),
    required_failures: Mapping[str, str] | None = None,
) -> list[ToolOutcomeExpectation]:
    """Declare an outcome for every registered tool without exposing the oracle."""

    successes = set(required_successes)
    failures = dict(required_failures or {})
    unknown = (successes | set(failures)) - set(registry.list())
    if unknown:
        raise ValueError(f"Outcome expectations reference unknown tools: {sorted(unknown)}")
    return [
        (
            ToolOutcomeExpectation.must_fail_with(name, error_code=failures[name])
            if name in failures
            else ToolOutcomeExpectation(
                tool_name=name,
                required=name in successes,
                expected_result="success",
            )
        )
        for name in registry.list()
    ]


def execute_isolated_runtime(
    *,
    task: TaskSpec,
    request: UserRequest,
    trace_id: str,
    parent_span_id: str,
    registry: ToolRegistry,
    config: ProviderConfig | None = None,
    chat_adapter: ChatAdapter | None = None,
    initial_state: dict[str, Any] | None = None,
    before_run: BeforeRun | None = None,
    final_state_reader: StateReader | None = None,
    runtime_overrides: Mapping[str, Any] | None = None,
) -> TaskExecution:
    """Run the active Runtime with per-run stores and project stable Evidence."""

    resolved_config = config or ProviderConfig.from_env()
    if chat_adapter is None:
        validate_real_chat_config(resolved_config)
    isolated = replace(
        resolved_config,
        mem0_base_url=None,
        conversation_history_backend="memory",
        langgraph_checkpointer_backend="none",
        durable_tasks_enabled=False,
        durable_task_worker_enabled=False,
    )
    runtime = AgentGraphRuntime(
        config=isolated,
        registry=registry,
        chat_adapter=chat_adapter,
        trace_store=InMemoryTraceStore(),
        session_store=InMemorySessionStore(),
        **dict(runtime_overrides or {}),
    )
    try:
        if before_run is not None:
            before_run(runtime)
        state = runtime.run_state(
            UserRequest.model_validate(request),
            trace_context=RuntimeTraceContext(
                trace_id=trace_id,
                parent_span_id=parent_span_id,
            ),
        )
        events = runtime.trace_store.list_by_run(state.run_id)
        final_state = (
            final_state_reader(runtime, state)
            if final_state_reader is not None
            else dict(initial_state or {})
        )
    finally:
        runtime.close()
    starting = dict(initial_state or {})
    evidence = RunEvidence(
        task_id=task.id,
        run_id=state.run_id,
        trace_id=state.trace_id,
        terminal_status=state.status,
        response=(
            state.response.model_dump(mode="json")
            if state.response is not None
            else None
        ),
        available_tools=available_tools(state, events),
        tool_executions=tool_executions(events),
        validation_results=validation_results(events),
        initial_state=starting,
        final_state=final_state,
        state_diff=_state_diff(starting, final_state),
        trace_event_names=[
            event.canonical_event
            for event in events
            if event.canonical_event is not None
        ],
        provider_result_kinds=provider_result_kinds(events),
    )
    return TaskExecution(evidence=evidence, trace_events=events)


def runtime_completed(evidence: RunEvidence) -> AssertionResult:
    return rule_assertion(
        evidence.terminal_status == "completed",
        f"terminal_status={evidence.terminal_status}",
        label="Runtime 正常完成",
    )


def expected_tools_exposed(
    evidence: RunEvidence,
    *tool_names: str,
) -> AssertionResult:
    missing = [name for name in tool_names if name not in evidence.available_tools]
    return rule_assertion(
        not missing and len(evidence.available_tools) > len(tool_names),
        f"missing={missing}, available_tools={evidence.available_tools}",
        label="完整目录包含目标工具",
    )


def validations_accepted(evidence: RunEvidence) -> AssertionResult:
    statuses = [item.status for item in evidence.validation_results]
    passed = len(statuses) == len(evidence.tool_executions) and all(
        status == "accepted" for status in statuses
    )
    return rule_assertion(
        passed,
        (
            f"validation_statuses={statuses}, "
            f"tool_execution_count={len(evidence.tool_executions)}"
        ),
        label="工具调用通过 Action Validator",
    )


def successful_tool_lifecycle(evidence: RunEvidence) -> AssertionResult:
    executions = evidence.tool_executions
    passed = bool(executions) and all(
        item.exposed
        and item.terminal_event == "tool.finished"
        and item.error_code is None
        for item in executions
    )
    return rule_assertion(
        passed,
        (
            f"terminal_events={[item.terminal_event for item in executions]}, "
            f"error_codes={[item.error_code for item in executions]}"
        ),
        label="工具调用成功闭合",
    )


def optional_successful_tool_lifecycle(evidence: RunEvidence) -> AssertionResult:
    executions = evidence.tool_executions
    passed = (
        all(
            item.exposed
            and item.terminal_event == "tool.finished"
            and item.error_code is None
            for item in executions
        )
        and len(evidence.validation_results) == len(executions)
    )
    return rule_assertion(
        passed,
        (
            f"tool_calls={[item.name for item in executions]}, "
            f"terminal_events={[item.terminal_event for item in executions]}, "
            f"error_codes={[item.error_code for item in executions]}"
        ),
        label="可选辅助工具调用成功闭合",
    )


def no_tool_execution(evidence: RunEvidence) -> AssertionResult:
    return rule_assertion(
        not evidence.tool_executions and not evidence.validation_results,
        (
            f"tool_calls={[item.name for item in evidence.tool_executions]}, "
            f"validation_count={len(evidence.validation_results)}"
        ),
        label="未在信息不足时调用工具",
    )


def response_generated(evidence: RunEvidence) -> AssertionResult:
    message = (
        str(evidence.response.get("message") or "").strip()
        if evidence.response is not None
        else ""
    )
    return rule_assertion(
        bool(message),
        f"response_present={bool(message)}",
        label="已生成面向用户的回答",
    )


def state_unchanged(evidence: RunEvidence) -> AssertionResult:
    changed = any(
        evidence.state_diff.get(key) for key in ("added", "modified", "deleted")
    )
    return rule_assertion(
        not changed,
        f"state_diff={evidence.state_diff}",
        label="只读任务未产生状态变更",
    )


def tool_sequence(
    evidence: RunEvidence,
    expected: list[str],
    *,
    label: str = "工具调用顺序正确",
) -> AssertionResult:
    actual = [item.name for item in evidence.tool_executions]
    return rule_assertion(
        actual == expected,
        f"tool_calls={actual}, expected={expected}",
        label=label,
    )


def _state_diff(
    initial: dict[str, Any],
    final: dict[str, Any],
) -> dict[str, list[str]]:
    initial_keys = set(initial)
    final_keys = set(final)
    return {
        "added": sorted(final_keys - initial_keys),
        "modified": sorted(
            key
            for key in initial_keys & final_keys
            if initial[key] != final[key]
        ),
        "deleted": sorted(initial_keys - final_keys),
    }
