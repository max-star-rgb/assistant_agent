"""Trusted identity context injected by LangGraph Agent Server."""

from __future__ import annotations

from assistant_agent.native_agent.context import AssistantRunContext


class AgentServerRunContext(AssistantRunContext):
    """Public alias documenting the Agent Server ownership boundary."""


__all__ = ["AgentServerRunContext"]
