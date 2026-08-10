"""Stable independent Agent eval scores and deterministic oracle matching."""

from __future__ import annotations

from collections.abc import Mapping

from evals.agent.contracts import (
    AssertionResult,
    DimensionResult,
    EnvironmentValidation,
    GraderDimensions,
    GraderResult,
    JudgeVerdict,
    LLMJudge,
    RunEvidence,
    TaskJudgeResult,
    TaskSpec,
    ToolOutcomeExpectation,
)


DIMENSION_NAMES = (
    "tool_execution",
    "tool_semantics",
    "grounding",
    "response_quality",
)


def rule_assertion(
    passed: bool,
    reason: str,
    *,
    label: str,
) -> AssertionResult:
    return AssertionResult(
        passed=passed,
        label=label,
        reason=reason,
        evaluation_method="rule",
    )


def judge_assertion(
    verdict: JudgeVerdict,
    *,
    criterion_id: str,
    label: str,
) -> AssertionResult:
    return AssertionResult(
        passed=verdict.passed,
        label=label,
        reason=verdict.reason,
        evaluation_method="judge",
        criterion_id=criterion_id,
    )


def dimension(
    assertions: Mapping[str, AssertionResult],
) -> DimensionResult:
    resolved = dict(assertions)
    if not resolved:
        raise ValueError("Agent eval dimension requires at least one assertion.")
    failed = [name for name, result in resolved.items() if not result.passed]
    return DimensionResult(
        passed=not failed,
        reason=(
            _passed_assertion_comment(resolved)
            if not failed
            else _failed_assertion_comment(resolved, failed)
        ),
        assertions=resolved,
    )


def task_judge_result(
    *,
    tool_semantics: DimensionResult,
    grounding: DimensionResult,
    response_quality: DimensionResult,
) -> TaskJudgeResult:
    return TaskJudgeResult(
        tool_semantics=tool_semantics,
        grounding=grounding,
        response_quality=response_quality,
    )


def grader_result(
    *,
    tool_execution: DimensionResult,
    tool_semantics: DimensionResult,
    grounding: DimensionResult,
    response_quality: DimensionResult,
) -> GraderResult:
    return GraderResult(
        dimensions=GraderDimensions(
            tool_execution=tool_execution,
            tool_semantics=tool_semantics,
            grounding=grounding,
            response_quality=response_quality,
        ),
    )


def environment_validation(
    checks: Mapping[str, AssertionResult],
) -> EnvironmentValidation:
    resolved = dict(checks)
    if not resolved:
        raise ValueError("Environment validation requires at least one check.")
    failed = [name for name, result in resolved.items() if not result.passed]
    return EnvironmentValidation(
        passed=not failed,
        reason=(
            "Environment 配置与受控依赖有效。"
            if not failed
            else _failed_assertion_comment(resolved, failed)
        ),
        checks=resolved,
    )


def grade_task(
    *,
    task: TaskSpec,
    evidence: RunEvidence,
    judge: LLMJudge,
) -> GraderResult:
    from evals.agent.loader import load_case_source, load_entrypoint

    environment = load_entrypoint(task.environment)()
    environment.validate().require_valid()
    expectations = environment.tool_outcome_expectations(evidence.available_tools)
    if task.grader is None:
        raise RuntimeError(
            f"Legacy task-local grading is not configured for {task.id!r}."
        )
    grader = load_entrypoint(task.grader)
    task_result: TaskJudgeResult = grader(evidence, judge)
    source = load_case_source(task.id)
    objective_assertions = None
    if source.level == "mission":
        objective_method = getattr(environment, "objective_state_assertions", None)
        if not callable(objective_method):
            raise RuntimeError(
                f"Mission {task.id!r} must define objective_state_assertions()."
            )
        objective_assertions = objective_method(evidence)
    return enforce_tool_outcome_expectations(
        task_result,
        evidence=evidence,
        expectations=expectations,
        objective_assertions=objective_assertions,
    )


def grade_task_conformance(
    *,
    task: TaskSpec,
    evidence: RunEvidence,
) -> DimensionResult:
    """Evaluate only Git-owned deterministic Environment and Mission rules."""

    from evals.agent.loader import load_case_source, load_entrypoint

    environment = load_entrypoint(task.environment)()
    environment.validate().require_valid()
    expectations = environment.tool_outcome_expectations(evidence.available_tools)
    objective_assertions = None
    if load_case_source(task.id).level == "mission":
        objective_method = getattr(environment, "objective_state_assertions", None)
        if not callable(objective_method):
            raise RuntimeError(
                f"Mission {task.id!r} must define objective_state_assertions()."
            )
        objective_assertions = objective_method(evidence)
    return _tool_conformance_dimension(
        evidence=evidence,
        expectations=expectations,
        objective_assertions=objective_assertions,
    )


def validate_mission_objective_assertions(
    assertions: Mapping[str, AssertionResult],
) -> dict[str, AssertionResult]:
    if not isinstance(assertions, Mapping):
        raise RuntimeError(
            "Mission objective_state_assertions() must return a mapping."
        )
    resolved = dict(assertions)
    if not resolved:
        raise RuntimeError(
            "Mission objective_state_assertions() must return at least one Rule."
        )
    invalid = [
        key
        for key, assertion in resolved.items()
        if assertion.evaluation_method != "rule"
        or assertion.criterion_id is not None
    ]
    if invalid:
        raise RuntimeError(
            "Mission objective assertions must use Rule evaluation: "
            + ", ".join(sorted(invalid))
        )
    return resolved


def enforce_tool_outcome_expectations(
    result: TaskJudgeResult,
    *,
    evidence: RunEvidence,
    expectations: list[ToolOutcomeExpectation],
    objective_assertions: Mapping[str, AssertionResult] | None = None,
) -> GraderResult:
    tool_execution = _tool_conformance_dimension(
        evidence=evidence,
        expectations=expectations,
        objective_assertions=objective_assertions,
    )
    return grader_result(
        tool_execution=tool_execution,
        tool_semantics=result.tool_semantics,
        grounding=result.grounding,
        response_quality=result.response_quality,
    )


def _tool_conformance_dimension(
    *,
    evidence: RunEvidence,
    expectations: list[ToolOutcomeExpectation],
    objective_assertions: Mapping[str, AssertionResult] | None = None,
) -> DimensionResult:
    _require_expectation_coverage(evidence, expectations)
    tool_execution_assertions = {
        "outcome_matches_environment": _tool_outcomes_match(
            evidence,
            expectations,
        )
    }
    if objective_assertions is not None:
        for key, assertion in validate_mission_objective_assertions(
            objective_assertions
        ).items():
            tool_execution_assertions[f"mission_state.{key}"] = assertion
    return dimension(tool_execution_assertions)


def _require_expectation_coverage(
    evidence: RunEvidence,
    expectations: list[ToolOutcomeExpectation],
) -> None:
    expected_names = [expectation.tool_name for expectation in expectations]
    if len(expected_names) != len(set(expected_names)):
        raise RuntimeError(
            "Agent eval Environment declares duplicate tool outcome expectations."
        )
    expected_name_set = set(expected_names)
    available_name_set = set(evidence.available_tools)
    missing_expectations = available_name_set - expected_name_set
    unexpected_optional = {
        expectation.tool_name
        for expectation in expectations
        if expectation.tool_name not in available_name_set
        and not expectation.required
    }
    if missing_expectations or unexpected_optional:
        raise RuntimeError(
            "Agent eval Environment outcome expectations do not cover the "
            f"available tools: expected={sorted(expected_name_set)}, "
            f"available={sorted(available_name_set)}."
        )


def _tool_outcomes_match(
    evidence: RunEvidence,
    expectations: list[ToolOutcomeExpectation],
) -> AssertionResult:
    executions_by_name = {
        expectation.tool_name: [
            execution
            for execution in evidence.tool_executions
            if execution.name == expectation.tool_name
        ]
        for expectation in expectations
    }
    unexpected = sorted(
        {
            execution.name
            for execution in evidence.tool_executions
            if execution.name is not None and execution.name not in executions_by_name
        }
    )
    mismatches: list[str] = []
    if unexpected:
        mismatches.append("unexpected_tools=" + ",".join(unexpected))
    for expectation in expectations:
        executions = executions_by_name[expectation.tool_name]
        if expectation.required and not executions:
            mismatches.append(f"{expectation.tool_name}:required_but_not_called")
            continue
        for execution in executions:
            if expectation.expected_result == "success":
                if (
                    execution.terminal_event != "tool.finished"
                    or execution.error_code is not None
                ):
                    mismatches.append(
                        f"{expectation.tool_name}:expected_success,"
                        f"actual={execution.terminal_event},"
                        f"error_code={execution.error_code}"
                    )
            elif (
                execution.terminal_event != "tool.failed"
                or execution.error_code != expectation.error_code
            ):
                mismatches.append(
                    f"{expectation.tool_name}:"
                    f"expected_failure={expectation.error_code},"
                    f"actual={execution.terminal_event},"
                    f"error_code={execution.error_code}"
                )
    return rule_assertion(
        not mismatches,
        (
            "工具业务结果符合 Environment 声明。"
            if not mismatches
            else "；".join(mismatches)
        ),
        label="工具结果符合受控环境预期",
    )


def _failed_assertion_comment(
    assertions: Mapping[str, AssertionResult],
    failed_names: list[str],
) -> str:
    lines = [
        f"- {assertions[name].label}：{assertions[name].reason}"
        for name in failed_names
    ]
    return (
        f"未通过 {len(failed_names)}/{len(assertions)} 项检查：\n"
        + "\n".join(lines)
    )


def _passed_assertion_comment(
    assertions: Mapping[str, AssertionResult],
) -> str:
    labels = [assertion.label for assertion in assertions.values()]
    return (
        f"全部检查通过（{len(labels)}/{len(labels)}）：\n"
        + "\n".join(f"- {label}" for label in labels)
    )
