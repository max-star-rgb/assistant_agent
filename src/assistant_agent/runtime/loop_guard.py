"""Loop guard counters for assistant ReAct execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal

LoopGuardDisposition = Literal["block_action", "finalize", "terminate"]


@dataclass(frozen=True)
class LoopGuardDecision:
    """Decision returned when a guard limit is checked."""

    triggered: bool
    code: str
    message: str
    disposition: LoopGuardDisposition = "block_action"


class LoopGuard:
    """Small counter-based guard against invalid or repetitive actions."""

    unknown_tool_limit = 1
    invalid_tool_input_limit = 2
    empty_decision_limit = 1

    def __init__(self, metadata: dict[str, Any]) -> None:
        state = metadata.setdefault("assistant_loop_guard", {})
        if not isinstance(state, dict):
            state = {}
            metadata["assistant_loop_guard"] = state
        self.state = state

    def record_empty_decision(self) -> LoopGuardDecision:
        return self._increment(
            "empty_decision_count",
            self.empty_decision_limit,
            "empty_decision_limit",
            disposition="terminate",
        )

    def record_validation_rejection(self, code: str, tool_name: str | None) -> LoopGuardDecision:
        if code == "unknown_tool":
            return self._increment(
                "unknown_tool_count",
                self.unknown_tool_limit,
                "unknown_tool_limit",
                disposition="finalize",
            )
        if code in {"invalid_tool_input", "missing_required_input"}:
            return self._increment(
                "invalid_tool_input_count",
                self.invalid_tool_input_limit,
                "invalid_tool_input_limit",
                disposition="finalize",
            )
        return LoopGuardDecision(
            True,
            "tool_validation_rejected",
            f"{code} is not recoverable within the assistant loop.",
            disposition="finalize",
        )

    def failed_call_already_seen(self, *, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Return whether the same normalized invocation already failed."""

        signatures = self.state.get("failed_tool_call_signatures", [])
        return (
            isinstance(signatures, list)
            and self._tool_call_signature(tool_name, tool_input) in signatures
        )

    def complete_call_already_seen(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """Return whether the same invocation already succeeded."""

        signatures = self.state.get("complete_tool_call_signatures", [])
        return (
            isinstance(signatures, list)
            and self._tool_call_signature(tool_name, tool_input) in signatures
        )

    def record_complete_tool_success(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        """Remember a successful invocation for this run."""

        signatures = self.state.get("complete_tool_call_signatures", [])
        if not isinstance(signatures, list):
            signatures = []
        signature = self._tool_call_signature(tool_name, tool_input)
        if signature not in signatures:
            signatures.append(signature)
        self.state["complete_tool_call_signatures"] = signatures

    def nonrecoverable_failure_already_seen(self, tool_name: str) -> bool:
        """Return whether this tool already reported a non-recoverable failure."""

        tool_names = self.state.get("nonrecoverable_failed_tools", [])
        return isinstance(tool_names, list) and tool_name in tool_names

    def record_tool_result(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        success: bool,
        nonrecoverable: bool = False,
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
        if nonrecoverable:
            tool_names = self.state.get("nonrecoverable_failed_tools", [])
            if not isinstance(tool_names, list):
                tool_names = []
            if tool_name not in tool_names:
                tool_names.append(tool_name)
            self.state["nonrecoverable_failed_tools"] = tool_names
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

    def _increment(
        self,
        key: str,
        limit: int,
        code: str,
        *,
        disposition: LoopGuardDisposition,
    ) -> LoopGuardDecision:
        value = int(self.state.get(key, 0)) + 1
        self.state[key] = value
        if value >= limit:
            return LoopGuardDecision(
                True,
                code,
                f"{code} reached; stopping further tool calls.",
                disposition=disposition,
            )
        return LoopGuardDecision(False, "ok", "Guard not triggered.")
