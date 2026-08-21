"""Runtime-fact-driven Tool disclosure independent from Skill activation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from assistant_agent.media.runtime_media import latest_runtime_media
from assistant_agent.media.visual_perception.history_probe import (
    VisualObservationHistoryProbe,
)
from assistant_agent.native_agent.context import authenticated_user_identity
from assistant_agent.tools.availability import (
    ToolAvailability,
    tool_availability,
)


class ConditionalToolExposureMiddleware(AgentMiddleware):
    """Filter pre-registered tools using trusted runtime media facts."""

    def __init__(
        self,
        history_probe: VisualObservationHistoryProbe | None = None,
        live_view_resolver: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        super().__init__()
        self._history_probe = history_probe
        self._live_view_resolver = live_view_resolver

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse | AIMessage],
    ) -> ModelResponse | AIMessage:
        return handler(self._request_with_visible_tools(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse | AIMessage]],
    ) -> ModelResponse | AIMessage:
        return await handler(self._request_with_visible_tools(request))

    def _request_with_visible_tools(self, request: ModelRequest) -> ModelRequest:
        visible_tools = [
            tool
            for tool in request.tools
            if not isinstance(tool, BaseTool) or self._is_available(tool, request)
        ]
        return request.override(tools=visible_tools)

    def _is_available(self, tool: BaseTool, request: ModelRequest) -> bool:
        availability = tool_availability(tool)
        if availability is ToolAvailability.ALWAYS:
            return True
        media = latest_runtime_media(request.state)
        if availability is ToolAvailability.UPLOADED_MEDIA_PRESENT:
            return media.has_uploaded_media
        runtime = request.runtime
        context = getattr(runtime, "context", None)
        video_handshake_completed = (
            getattr(context, "realtime_media_mode", "none") == "video"
        )
        live = self._trusted_live_view(runtime)
        if availability is ToolAvailability.VIDEO_HANDSHAKE_COMPLETED:
            return video_handshake_completed and live is not None
        if availability is ToolAvailability.VIDEO_FRAME_RECEIVED:
            return (
                video_handshake_completed
                and live is not None
                and bool(live.live_video_ids)
            )
        if availability is ToolAvailability.VISUAL_KEYFRAME_AVAILABLE:
            return (
                video_handshake_completed
                and live is not None
                and bool(live.live_video_ids)
                and live.target_video_id in live.live_video_ids
                and live.target_sequence is not None
                and bool(live.window_sequences)
                and live.window_sequences[-1] == live.target_sequence
            )
        if availability is ToolAvailability.VISUAL_HISTORY_AVAILABLE:
            return (
                video_handshake_completed
                and live is not None
                and live.target_sequence is not None
                and self._has_visual_history(
                    runtime,
                    as_of_sequence=live.target_sequence,
                )
            )
        return False

    def _trusted_live_view(self, runtime: Any) -> Any | None:
        if self._live_view_resolver is None:
            return None
        context = getattr(runtime, "context", None)
        token = getattr(context, "visual_capability_token", None)
        execution = getattr(runtime, "execution_info", None)
        session_id = getattr(execution, "thread_id", None)
        if not isinstance(token, str) or not isinstance(session_id, str):
            return None
        try:
            return self._live_view_resolver(
                authenticated_user_identity(runtime),
                session_id,
                token,
            )
        except Exception:  # noqa: BLE001 - availability must fail closed.
            return None

    def _has_visual_history(
        self,
        runtime: Any,
        *,
        as_of_sequence: int | None,
    ) -> bool:
        if self._history_probe is None:
            return False
        execution = getattr(runtime, "execution_info", None)
        session_id = getattr(execution, "thread_id", None)
        if not isinstance(session_id, str) or not session_id:
            return False
        try:
            user_id = authenticated_user_identity(runtime)
            return self._history_probe.has_searchable_observations(
                user_id=user_id,
                session_id=session_id,
                as_of_sequence=as_of_sequence,
            )
        except Exception:  # noqa: BLE001 - availability must fail closed.
            return False


__all__ = ["ConditionalToolExposureMiddleware"]
