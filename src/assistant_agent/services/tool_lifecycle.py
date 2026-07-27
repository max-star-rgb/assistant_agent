"""Prompt-safe lifecycle summaries for realtime-aware tool execution."""

from __future__ import annotations

from typing import Any, Literal

from assistant_agent.schemas.tools import ToolResult


ToolLifecycleStatus = Literal[
    "succeeded",
    "failed",
    "cancelled_before_commit",
    "committed",
    "interrupted_after_commit",
    "deferred",
]


def build_tool_lifecycle_summary(
    *,
    result: ToolResult,
    side_effect: dict[str, Any],
    status: str,
    cancel_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a compact lifecycle summary for trace/audit/realtime consumers."""

    data = result.data if isinstance(result.data, dict) else {}
    effect_level = str(side_effect.get("level") or "none")
    committed_effect = effect_level in {"committed", "compensatable"}

    if data.get("status") == "deferred":
        return _summary("deferred", committed=False, cancellable=False, next_action="resume_later")
    if cancel_metadata:
        if result.success and committed_effect:
            return _summary(
                "interrupted_after_commit",
                committed=True,
                cancellable=False,
                next_action="report_committed",
            )
        return _summary(
            "cancelled_before_commit",
            committed=False,
            cancellable=False,
            next_action="restart_or_skip",
        )
    if not result.success:
        return _summary("failed", committed=False, cancellable=False, next_action="retry_or_explain")
    if committed_effect:
        return _summary("committed", committed=True, cancellable=False, next_action="report_result")
    return _summary("succeeded", committed=False, cancellable=False, next_action="report_result")


def _summary(
    status: ToolLifecycleStatus,
    *,
    committed: bool,
    cancellable: bool,
    next_action: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "committed": committed,
        "cancellable": cancellable,
        "next_action": next_action,
    }
