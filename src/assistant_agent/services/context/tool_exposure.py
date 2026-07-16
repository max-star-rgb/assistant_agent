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
from assistant_agent.services.tool_policy import ToolPolicyView


@dataclass(frozen=True)
class ToolExposureFacts:
    """Prompt-safe structured facts used to expose entry-profile tools."""

    trusted_agent_service: bool
    entry_profile: str | None
    active_image_ids: tuple[str, ...] = ()
    active_video_ids: tuple[str, ...] = ()
    active_audio_id: str | None = None

    @property
    def has_active_video(self) -> bool:
        return bool(self.active_video_ids)

    @property
    def active_media_types(self) -> frozenset[str]:
        active: set[str] = set()
        if self.active_image_ids:
            active.add("image")
        if self.active_video_ids:
            active.add("video")
        if self.active_audio_id:
            active.add("audio")
        return frozenset(active)


@dataclass(frozen=True)
class ToolExposureDecision:
    """Decision for whether one tool is exposed for the current turn."""

    exposed: bool
    reasons: tuple[str, ...] = ()
    excluded_reasons: tuple[str, ...] = ()
    facts: ToolExposureFacts | None = None


def tool_exposure_facts(request: UserRequest) -> ToolExposureFacts:
    """Extract bounded structured facts for tool exposure."""

    return ToolExposureFacts(
        trusted_agent_service=is_trusted_agent_service_request(request),
        entry_profile=_entry_profile(request.metadata),
        active_image_ids=tuple(_string_list(request.image_ids)),
        active_video_ids=tuple(_string_list(request.video_ids)),
        active_audio_id=_string_value(request.audio_id),
    )


def entry_profile_tool_exposure(
    request: UserRequest,
    policy: ToolPolicyView,
) -> ToolExposureDecision:
    """Return whether one tool is exposed by the current entry profile."""

    facts = tool_exposure_facts(request)
    if not facts.trusted_agent_service:
        return ToolExposureDecision(
            exposed=True,
            reasons=("default_entry_profile",),
            facts=facts,
        )
    if facts.entry_profile not in policy.allowed_entry_profiles:
        return ToolExposureDecision(
            exposed=False,
            excluded_reasons=("entry_profile_not_exposed",),
            facts=facts,
        )
    if not _has_required_media(policy, facts):
        return ToolExposureDecision(
            exposed=False,
            excluded_reasons=("entry_profile_not_exposed",),
            facts=facts,
        )
    return ToolExposureDecision(
        exposed=True,
        reasons=(f"entry_profile_allowed:{facts.entry_profile}",),
        facts=facts,
    )


def _has_required_media(policy: ToolPolicyView, facts: ToolExposureFacts) -> bool:
    required = set(policy.requires_media)
    if not required:
        return True
    return required.issubset(facts.active_media_types)


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


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
