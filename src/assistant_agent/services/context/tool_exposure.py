"""Structured per-turn tool exposure rules.

This module deliberately keeps exposure decisions on runtime facts such as
entry profile and attached media references. It must not infer intent from
user text; the LLM decides whether to call an exposed tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.agent_service_entry import is_trusted_agent_service_request


AGENT_SERVICE_BASE_TOOL_NAMES = frozenset(
    {
        "web_search",
        "shopping_search",
        "memory_retrieval",
        "memory_save",
    }
)


@dataclass(frozen=True)
class ToolExposureFacts:
    """Prompt-safe structured facts used to expose entry-profile tools."""

    trusted_agent_service: bool
    entry_profile: str | None
    active_video_ids: tuple[str, ...] = ()

    @property
    def has_active_video(self) -> bool:
        return bool(self.active_video_ids)


@dataclass(frozen=True)
class ToolExposureRule:
    """Declarative dynamic exposure rule for one tool."""

    tool_name: str
    agent_service: bool = False
    requires_active_video: bool = False


@dataclass(frozen=True)
class ToolExposureDecision:
    """Decision for whether one tool is exposed for the current turn."""

    exposed: bool
    reasons: tuple[str, ...] = ()
    excluded_reasons: tuple[str, ...] = ()
    facts: ToolExposureFacts | None = None


AGENT_SERVICE_DYNAMIC_RULES: dict[str, ToolExposureRule] = {
    "video_understanding": ToolExposureRule(
        tool_name="video_understanding",
        agent_service=True,
        requires_active_video=True,
    ),
}


def tool_exposure_facts(request: UserRequest) -> ToolExposureFacts:
    """Extract bounded structured facts for tool exposure."""

    return ToolExposureFacts(
        trusted_agent_service=is_trusted_agent_service_request(request),
        entry_profile=_entry_profile(request.metadata),
        active_video_ids=tuple(_string_list(request.video_ids)),
    )


def entry_profile_tool_exposure(
    request: UserRequest,
    tool_name: str,
) -> ToolExposureDecision:
    """Return whether ``tool_name`` is exposed by the current entry profile."""

    facts = tool_exposure_facts(request)
    if not facts.trusted_agent_service:
        return ToolExposureDecision(
            exposed=True,
            reasons=("default_entry_profile",),
            facts=facts,
        )
    if tool_name in AGENT_SERVICE_BASE_TOOL_NAMES:
        return ToolExposureDecision(
            exposed=True,
            reasons=("agent_service_base_tool",),
            facts=facts,
        )
    rule = AGENT_SERVICE_DYNAMIC_RULES.get(tool_name)
    if rule is not None and _matches_rule(rule, facts):
        return ToolExposureDecision(
            exposed=True,
            reasons=("agent_service_dynamic_tool",),
            facts=facts,
        )
    return ToolExposureDecision(
        exposed=False,
        excluded_reasons=("entry_profile_not_exposed",),
        facts=facts,
    )


def _matches_rule(rule: ToolExposureRule, facts: ToolExposureFacts) -> bool:
    if rule.agent_service and not facts.trusted_agent_service:
        return False
    if rule.requires_active_video and not facts.has_active_video:
        return False
    return True


def _entry_profile(metadata: dict[str, Any]) -> str | None:
    gateway = metadata.get("gateway")
    if not isinstance(gateway, dict):
        return None
    session_config = gateway.get("session_config")
    if not isinstance(session_config, dict):
        return None
    value = session_config.get("entry_profile")
    return value if isinstance(value, str) and value else None


def _string_list(value: list[str]) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
