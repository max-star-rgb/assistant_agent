"""Loop guard counters for assistant ReAct execution."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from assistant_agent.schemas.tool_ids import IMAGE_GENERATION_TOOL_NAME


@dataclass(frozen=True)
class LoopGuardDecision:
    """Decision returned when a guard limit is checked."""

    triggered: bool
    code: str
    message: str


class LoopGuard:
    """Small counter-based guard against invalid or repetitive actions."""

    same_tool_failure_limit = 1
    unknown_tool_limit = 1
    invalid_tool_input_limit = 1
    empty_decision_limit = 1

    # Tools that produce a terminal artifact on success and should not be
    # called again within the same run (the assistant should answer instead).
    terminal_tools = frozenset({IMAGE_GENERATION_TOOL_NAME})

    def __init__(self, metadata: dict[str, Any]) -> None:
        state = metadata.setdefault("assistant_loop_guard", {})
        if not isinstance(state, dict):
            state = {}
            metadata["assistant_loop_guard"] = state
        self.state = state

    def record_empty_decision(self) -> LoopGuardDecision:
        return self._increment("empty_decision_count", self.empty_decision_limit, "empty_decision_limit")

    def record_terminal_tool_success(self, tool_name: str) -> None:
        """Remember that a terminal tool already succeeded in this run."""

        if tool_name not in self.terminal_tools:
            return
        succeeded = self.state.get("succeeded_terminal_tools", [])
        if not isinstance(succeeded, list):
            succeeded = []
        if tool_name not in succeeded:
            succeeded.append(tool_name)
        self.state["succeeded_terminal_tools"] = succeeded

    def terminal_tool_already_succeeded(self, tool_name: str) -> bool:
        """Return true when a terminal tool already produced a result this run."""

        if tool_name not in self.terminal_tools:
            return False
        succeeded = self.state.get("succeeded_terminal_tools", [])
        return isinstance(succeeded, list) and tool_name in succeeded

    def record_validation_rejection(self, code: str, tool_name: str | None) -> LoopGuardDecision:
        if code == "unknown_tool":
            return self._increment("unknown_tool_count", self.unknown_tool_limit, "unknown_tool_limit")
        if code in {"invalid_tool_input", "missing_required_input"}:
            return self._increment("invalid_tool_input_count", self.invalid_tool_input_limit, "invalid_tool_input_limit")
        return LoopGuardDecision(False, "ok", "Guard not triggered.")

    def record_tool_result(self, *, tool_name: str, success: bool) -> LoopGuardDecision:
        if success:
            failures = Counter(self.state.get("same_tool_failures", {}))
            if tool_name in failures:
                failures.pop(tool_name, None)
            self.state["same_tool_failures"] = dict(failures)
            return LoopGuardDecision(False, "ok", "Guard not triggered.")
        failures = Counter(self.state.get("same_tool_failures", {}))
        failures[tool_name] += 1
        self.state["same_tool_failures"] = dict(failures)
        if failures[tool_name] >= self.same_tool_failure_limit:
            return LoopGuardDecision(
                True,
                "same_tool_failure_limit",
                f"{tool_name} failed repeatedly; stopping further tool calls.",
            )
        return LoopGuardDecision(False, "ok", "Guard not triggered.")

    def _increment(self, key: str, limit: int, code: str) -> LoopGuardDecision:
        value = int(self.state.get(key, 0)) + 1
        self.state[key] = value
        if value >= limit:
            return LoopGuardDecision(True, code, f"{code} reached; stopping further tool calls.")
        return LoopGuardDecision(False, "ok", "Guard not triggered.")
