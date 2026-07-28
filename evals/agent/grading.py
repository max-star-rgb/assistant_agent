"""Stable Agent eval dimensions and deterministic aggregation."""

from __future__ import annotations

from collections.abc import Mapping

from evals.agent.contracts import (
    AssertionResult,
    DimensionResult,
    EnvironmentValidation,
    GraderDimensions,
    GraderResult,
)


DIMENSION_NAMES = (
    "tool_execution",
    "tool_semantics",
    "state",
    "response",
)


def assertion(passed: bool, reason: str) -> AssertionResult:
    return AssertionResult(passed=passed, reason=reason)


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
    tool_semantics: DimensionResult,
    state: DimensionResult,
    response: DimensionResult,
) -> GraderResult:
    dimensions = GraderDimensions(
        tool_execution=tool_execution,
        tool_semantics=tool_semantics,
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
