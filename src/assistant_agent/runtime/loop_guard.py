"""Loop guard counters for assistant ReAct execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from assistant_agent.tools.ids import IMAGE_GENERATION_TOOL_NAME


@dataclass(frozen=True)
class LoopGuardDecision:
    """Decision returned when a guard limit is checked."""

    triggered: bool
    code: str
    message: str


class LoopGuard:
    """Small counter-based guard against invalid or repetitive actions."""

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

    def failed_call_already_seen(self, *, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Return whether the same normalized invocation already failed."""

        signatures = self.state.get("failed_tool_call_signatures", [])
        return (
            isinstance(signatures, list)
            and self._tool_call_signature(tool_name, tool_input) in signatures
        )

    def record_tool_result(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        success: bool,
    ) -> LoopGuardDecision:
        signature = self._tool_call_signature(tool_name, tool_input)
        signatures = self.state.get("failed_tool_call_signatures", [])
        if not isinstance(signatures, list):
            signatures = []
        if success:
            self.state["failed_tool_call_signatures"] = [
                item for item in signatures if item != signature
            ]
            return LoopGuardDecision(False, "ok", "Guard not triggered.")
        if signature not in signatures:
            signatures.append(signature)
            self.state["failed_tool_call_signatures"] = signatures
        return LoopGuardDecision(False, "ok", "Guard not triggered.")

    @staticmethod
    def _tool_call_signature(tool_name: str, tool_input: dict[str, Any]) -> str:
        payload = json.dumps(
            {"tool_name": tool_name, "tool_input": tool_input},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _increment(self, key: str, limit: int, code: str) -> LoopGuardDecision:
        value = int(self.state.get(key, 0)) + 1
        self.state[key] = value
        if value >= limit:
            return LoopGuardDecision(True, code, f"{code} reached; stopping further tool calls.")
        return LoopGuardDecision(False, "ok", "Guard not triggered.")
