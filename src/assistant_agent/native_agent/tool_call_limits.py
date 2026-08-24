"""Generic metadata-driven per-Tool call limits for the native agent loop."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.channels.untracked_value import UntrackedValue

from assistant_agent.tools.native_boundary import RUN_CALL_LIMIT_METADATA_KEY


class PerToolCallLimitState(AgentState):
    """Private per-run counters used by ``PerToolCallLimitMiddleware``."""

    per_tool_run_call_count: NotRequired[
        Annotated[dict[str, int], UntrackedValue, PrivateStateAttr]
    ]


class PerToolCallLimitMiddleware(AgentMiddleware):
    """Enforce positive per-run Tool limits declared in trusted metadata."""

    state_schema = PerToolCallLimitState

    def __init__(self, run_limits: Mapping[str, int]) -> None:
        super().__init__()
        if not run_limits:
            raise ValueError("at least one per-Tool run call limit is required")
        self.run_limits = dict(sorted(run_limits.items()))

    @classmethod
    def from_tools(
        cls,
        tools: Sequence[BaseTool],
    ) -> PerToolCallLimitMiddleware | None:
        """Build one limiter from all Tool metadata, or return ``None``."""

        run_limits: dict[str, int] = {}
        for tool in tools:
            raw_limit = (tool.metadata or {}).get(RUN_CALL_LIMIT_METADATA_KEY)
            if raw_limit is None:
                continue
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1:
                raise ValueError(
                    f"Tool {tool.name!r} metadata {RUN_CALL_LIMIT_METADATA_KEY!r} "
                    "must be a positive integer"
                )
            run_limits[tool.name] = raw_limit
        return cls(run_limits) if run_limits else None

    def after_model(self, state, runtime) -> dict[str, Any] | None:
        """Count configured calls and satisfy calls exceeding their Tool limit."""

        del runtime
        last_ai_message = next(
            (
                message
                for message in reversed(state.get("messages", ()))
                if isinstance(message, AIMessage)
            ),
            None,
        )
        if last_ai_message is None or not last_ai_message.tool_calls:
            return None

        counts = dict(state.get("per_tool_run_call_count", {}))
        blocked_messages: list[ToolMessage] = []
        tracked = False
        for tool_call in last_ai_message.tool_calls:
            tool_name = tool_call["name"]
            limit = self.run_limits.get(tool_name)
            if limit is None:
                continue
            tracked = True
            attempted_count = counts.get(tool_name, 0) + 1
            counts[tool_name] = attempted_count
            if attempted_count > limit:
                blocked_messages.append(
                    ToolMessage(
                        content=(
                            "Tool call limit exceeded. "
                            f"Do not call '{tool_name}' again."
                        ),
                        tool_call_id=tool_call["id"],
                        name=tool_name,
                        status="error",
                    )
                )

        if not tracked:
            return None
        update: dict[str, Any] = {"per_tool_run_call_count": counts}
        if blocked_messages:
            update["messages"] = blocked_messages
        return update

    async def aafter_model(self, state, runtime) -> dict[str, Any] | None:
        """Run the same deterministic policy in async graphs."""

        return self.after_model(state, runtime)


__all__ = ["PerToolCallLimitMiddleware", "PerToolCallLimitState"]
