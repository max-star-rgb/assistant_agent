"""Generic argument-aware per-Tool call limits for the native agent loop."""

from __future__ import annotations

import hashlib
import json
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
    per_tool_run_argument_fingerprints: NotRequired[
        Annotated[dict[str, list[str]], UntrackedValue, PrivateStateAttr]
    ]


class PerToolCallLimitMiddleware(AgentMiddleware):
    """Bound distinct calls per Tool and reject repeated argument payloads."""

    state_schema = PerToolCallLimitState

    def __init__(
        self,
        run_limits: Mapping[str, int],
        *,
        default_run_limit: int = 12,
    ) -> None:
        super().__init__()
        if (
            isinstance(default_run_limit, bool)
            or not isinstance(default_run_limit, int)
            or default_run_limit < 1
        ):
            raise ValueError("default per-Tool run call limit must be positive")
        for tool_name, run_limit in run_limits.items():
            if (
                not isinstance(tool_name, str)
                or not tool_name
                or isinstance(run_limit, bool)
                or not isinstance(run_limit, int)
                or run_limit < 1
            ):
                raise ValueError("per-Tool run limits must use names and positive integers")
        self.default_run_limit = default_run_limit
        self.run_limits = {
            tool_name: min(run_limit, default_run_limit)
            for tool_name, run_limit in sorted(run_limits.items())
        }

    @classmethod
    def from_tools(
        cls,
        tools: Sequence[BaseTool],
        *,
        default_run_limit: int = 12,
    ) -> PerToolCallLimitMiddleware:
        """Build one limiter with optional stricter metadata overrides."""

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
        return cls(run_limits, default_run_limit=default_run_limit)

    def after_model(self, state, runtime) -> dict[str, Any] | None:
        """Count distinct arguments and satisfy duplicate or excess calls."""

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
        fingerprints = {
            tool_name: list(values)
            for tool_name, values in state.get(
                "per_tool_run_argument_fingerprints", {}
            ).items()
        }
        blocked_messages: list[ToolMessage] = []
        for tool_call in last_ai_message.tool_calls:
            tool_name = tool_call["name"]
            limit = self.run_limits.get(tool_name, self.default_run_limit)
            fingerprint = _argument_fingerprint(tool_call.get("args", {}))
            seen = fingerprints.setdefault(tool_name, [])
            if fingerprint in seen:
                blocked_messages.append(
                    ToolMessage(
                        content=(
                            "An identical call to this tool already ran in the current "
                            "run. Reuse its existing result instead of calling it again."
                        ),
                        tool_call_id=tool_call["id"],
                        name=tool_name,
                        status="error",
                    )
                )
                continue
            if counts.get(tool_name, 0) >= limit:
                blocked_messages.append(
                    ToolMessage(
                        content=(
                            "The per-tool distinct-argument limit was reached for this "
                            "run. Do not call this tool with additional arguments."
                        ),
                        tool_call_id=tool_call["id"],
                        name=tool_name,
                        status="error",
                    )
                )
                continue
            seen.append(fingerprint)
            counts[tool_name] = counts.get(tool_name, 0) + 1

        update: dict[str, Any] = {
            "per_tool_run_call_count": counts,
            "per_tool_run_argument_fingerprints": fingerprints,
        }
        if blocked_messages:
            update["messages"] = blocked_messages
        return update

    async def aafter_model(self, state, runtime) -> dict[str, Any] | None:
        """Run the same deterministic policy in async graphs."""

        return self.after_model(state, runtime)


def _argument_fingerprint(arguments: object) -> str:
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = ["PerToolCallLimitMiddleware", "PerToolCallLimitState"]
