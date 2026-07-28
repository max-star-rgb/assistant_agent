"""Stable Agent eval dimensions and deterministic aggregation."""

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
    TaskSpec,
    ToolOutcomeExpectation,
)


DIMENSION_NAMES = (
    "tool_execution",
    "tool_use",
    "state",
    "response",
)


def rule_assertion(passed: bool, reason: str) -> AssertionResult:
    return AssertionResult(
        passed=passed,
        reason=reason,
        evaluation_method="rule",
    )


def judge_assertion(
    verdict: JudgeVerdict,
    *,
    criterion_id: str,
) -> AssertionResult:
    return AssertionResult(
        passed=verdict.passed,
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
        reason=("全部断言通过。" if not failed else "未通过：" + "、".join(failed)),
        assertions=resolved,
    )


def grader_result(
    *,
    tool_execution: DimensionResult,
    tool_use: DimensionResult,
    state: DimensionResult,
    response: DimensionResult,
) -> GraderResult:
    dimensions = GraderDimensions(
        tool_execution=tool_execution,
        tool_use=tool_use,
        state=state,
        response=response,
    )
    failed = [name for name in DIMENSION_NAMES if not getattr(dimensions, name).passed]
    passed = not failed
    return GraderResult(
        passed=passed,
        reward=1.0 if passed else 0.0,
        reason=(
            "全部必要评分维度通过。" if passed else "未通过维度：" + "、".join(failed)
        ),
        dimensions=dimensions,
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
            else "未通过：" + "、".join(failed)
        ),
        checks=resolved,
    )


def grade_task(
    *,
    task: TaskSpec,
    evidence: RunEvidence,
    judge: LLMJudge,
) -> GraderResult:
    from evals.agent.loader import load_entrypoint

    environment = load_entrypoint(task.environment)()
    environment.validate().require_valid()
    expectations = environment.tool_outcome_expectations()
    grader = load_entrypoint(task.grader)
    task_result: GraderResult = grader(evidence, judge)
    return enforce_tool_outcome_expectations(
        task_result,
        evidence=evidence,
        expectations=expectations,
    )


def enforce_tool_outcome_expectations(
    result: GraderResult,
    *,
    evidence: RunEvidence,
    expectations: list[ToolOutcomeExpectation],
) -> GraderResult:
    _require_expectation_coverage(evidence, expectations)
    outcome_assertion = _tool_outcomes_match(evidence, expectations)
    tool_use = dimension(
        {
            "outcome_matches_environment": outcome_assertion,
            **result.dimensions.tool_use.assertions,
        }
    )
    return grader_result(
        tool_execution=result.dimensions.tool_execution,
        tool_use=tool_use,
        state=result.dimensions.state,
        response=result.dimensions.response,
    )


def _require_expectation_coverage(
    evidence: RunEvidence,
    expectations: list[ToolOutcomeExpectation],
) -> None:
    expected_names = [expectation.tool_name for expectation in expectations]
    if len(expected_names) != len(set(expected_names)):
        raise RuntimeError(
            "Agent eval Environment declares duplicate tool outcome expectations."
        )
    if set(expected_names) != set(evidence.available_tools):
        raise RuntimeError(
            "Agent eval Environment outcome expectations do not cover the "
            f"available tools: expected={sorted(expected_names)}, "
            f"available={sorted(evidence.available_tools)}."
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
    )
