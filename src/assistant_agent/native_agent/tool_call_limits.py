"""Per-superstep parallel Tool call limits for the native agent loop."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage


class PerToolCallLimitMiddleware(AgentMiddleware):
    """Bound same-turn parallel calls independently for each Tool."""

    def __init__(
        self,
        *,
        max_parallel_calls_per_tool: int = 12,
    ) -> None:
        super().__init__()
        if (
            isinstance(max_parallel_calls_per_tool, bool)
            or not isinstance(max_parallel_calls_per_tool, int)
            or max_parallel_calls_per_tool < 1
        ):
            raise ValueError("parallel per-Tool call limit must be positive")
        self.max_parallel_calls_per_tool = max_parallel_calls_per_tool

    def after_model(self, state, runtime) -> dict[str, Any] | None:
        """Satisfy same-Tool calls beyond the current turn's parallel limit."""

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

        parallel_counts: dict[str, int] = {}
        blocked_messages: list[ToolMessage] = []
        for tool_call in last_ai_message.tool_calls:
            tool_name = tool_call["name"]
            if parallel_counts.get(tool_name, 0) >= self.max_parallel_calls_per_tool:
                blocked_messages.append(
                    ToolMessage(
                        content=(
                            "The per-tool parallel call limit was reached for this model "
                            "turn. Continue remaining calls in a later turn."
                        ),
                        tool_call_id=tool_call["id"],
                        name=tool_name,
                        status="error",
                    )
                )
                continue
            parallel_counts[tool_name] = parallel_counts.get(tool_name, 0) + 1

        return {"messages": blocked_messages} if blocked_messages else None

    async def aafter_model(self, state, runtime) -> dict[str, Any] | None:
        """Run the same deterministic policy in async graphs."""

        return self.after_model(state, runtime)

__all__ = ["PerToolCallLimitMiddleware"]
