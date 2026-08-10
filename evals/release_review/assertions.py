from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .contracts import ReleaseScenario, StateAssertion, ToolArgumentAssertion
from .evidence import ReleaseRunEvidence, ReleaseToolCallEvidence


_MISSING = object()


class ConformanceAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    passed: bool
    reason: str


class ConformanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool | None
    assertions: tuple[ConformanceAssertion, ...] = ()
    failure_owner: Literal["agent", "infrastructure"] | None = None


def evaluate_task_conformance(
    scenario: ReleaseScenario,
    evidence: ReleaseRunEvidence,
) -> ConformanceResult:
    if evidence.infrastructure_error is not None:
        return ConformanceResult(
            passed=None,
            failure_owner="infrastructure",
        )

    assertions: list[ConformanceAssertion] = []
    calls_by_tool: dict[str, list[ReleaseToolCallEvidence]] = {}
    for call in evidence.calls:
        calls_by_tool.setdefault(call.tool_name, []).append(call)

    for tool_name in scenario.tool_contract.required:
        passed = bool(calls_by_tool.get(tool_name))
        assertions.append(
            _result(
                f"required:{tool_name}",
                f"Required tool {tool_name}",
                passed,
                "tool was called" if passed else "tool was not called",
            )
        )

    for tool_name in scenario.tool_contract.forbidden:
        passed = not calls_by_tool.get(tool_name)
        assertions.append(
            _result(
                f"forbidden:{tool_name}",
                f"Forbidden tool {tool_name}",
                passed,
                "tool was not called" if passed else "tool was called",
            )
        )

    permitted = set(scenario.tool_contract.required) | set(
        scenario.tool_contract.allowed
    )
    for call in evidence.calls:
        passed = call.tool_name in permitted
        assertions.append(
            _result(
                f"allowed:{call.tool_name}",
                f"Tool {call.tool_name} is allowed",
                passed,
                "tool is in the scenario catalog"
                if passed
                else "tool is outside the scenario catalog",
            )
        )

    for index, contract in enumerate(scenario.tool_contract.arguments):
        candidates = calls_by_tool.get(contract.tool, [])
        passed = any(_matches(contract, call.input) for call in candidates)
        assertions.append(
            _result(
                f"arguments:{index}:{contract.tool}:{contract.path}",
                f"Arguments for {contract.tool} at {contract.path}",
                passed,
                "a matching call was observed"
                if passed
                else "no matching call was observed",
            )
        )

    for before, after in scenario.tool_contract.sequence.before:
        before_indices = [call.call_index for call in calls_by_tool.get(before, [])]
        after_indices = [call.call_index for call in calls_by_tool.get(after, [])]
        passed = bool(before_indices and after_indices) and min(before_indices) < min(
            after_indices
        )
        assertions.append(
            _result(
                f"sequence:before:{before}:{after}",
                f"{before} occurs before {after}",
                passed,
                "call order matched" if passed else "call order did not match",
            )
        )

    for tool_name in scenario.tool_contract.sequence.before_final_response:
        calls = calls_by_tool.get(tool_name, [])
        passed = bool(calls) and all(call.before_final_response for call in calls)
        assertions.append(
            _result(
                f"sequence:before_final_response:{tool_name}",
                f"{tool_name} finishes before final response",
                passed,
                "tool finished before final response"
                if passed
                else "tool did not finish before final response",
            )
        )

    for index, contract in enumerate(scenario.state_assertions):
        passed = _matches(contract, evidence.final_state)
        assertions.append(
            _result(
                f"state:{index}:{contract.path}",
                f"Final state at {contract.path}",
                passed,
                "state assertion matched" if passed else "state assertion did not match",
            )
        )

    passed = all(assertion.passed for assertion in assertions)
    return ConformanceResult(
        passed=passed,
        assertions=tuple(assertions),
        failure_owner=None if passed else "agent",
    )


def _result(key: str, label: str, passed: bool, reason: str) -> ConformanceAssertion:
    return ConformanceAssertion(key=key, label=label, passed=passed, reason=reason)


def _matches(
    assertion: ToolArgumentAssertion | StateAssertion,
    payload: Any,
) -> bool:
    value = _resolve_path(payload, assertion.path)
    if assertion.exists is not None:
        return (value is not _MISSING) is assertion.exists
    if value is _MISSING:
        return False
    if assertion.equals is not None:
        return value == assertion.equals
    if assertion.contains is not None:
        try:
            return assertion.contains in value
        except TypeError:
            return False
    if assertion.gte is not None:
        return isinstance(value, (int, float)) and value >= assertion.gte
    if assertion.length is not None:
        try:
            return len(value) == assertion.length
        except TypeError:
            return False
    return False


def _resolve_path(payload: Any, path: str) -> Any:
    current = payload
    for component in path.split("."):
        if isinstance(current, Mapping):
            if component not in current:
                return _MISSING
            current = current[component]
            continue
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            try:
                current = current[int(component)]
            except (ValueError, IndexError):
                return _MISSING
            continue
        return _MISSING
    return current

