"""Structured per-turn tool exposure rules.

This module keeps hard exposure decisions on structured runtime facts such as
entry profile, attached media references, tool policy category, code-configured
visibility, and explicit structured opt-in. It never infers user intent from
natural-language request text; the LLM decides whether to call one of the
already exposed tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.agent_service_entry import is_trusted_agent_service_request
from assistant_agent.services.tool_policy import ToolPolicyView

ToolExposureCategory = Literal["read", "generate", "write", "dangerous"]
_DANGEROUS_TOOL_NAMES = {"python_interpreter"}
_MEDIA_BOUND_TOOL_TYPES = {
    "video_understanding": {"video"},
    "vision_understanding": {"image", "video"},
    "visual_image_search": {"image"},
}


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
    *,
    configured_for_exposure: bool = False,
    explicitly_enabled: bool = False,
) -> ToolExposureDecision:
    """Return whether one tool is exposed by the current entry profile."""

    facts = tool_exposure_facts(request)
    if (
        facts.trusted_agent_service
        and policy.allowed_entry_profiles
        and facts.entry_profile not in policy.allowed_entry_profiles
    ):
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
    category = tool_exposure_category(policy)
    if category == "read":
        return ToolExposureDecision(
            exposed=True,
            reasons=("tool_category:read",),
            facts=facts,
        )
    if category == "generate":
        if configured_for_exposure or explicitly_enabled:
            return ToolExposureDecision(
                exposed=True,
                reasons=(
                    "tool_category:generate",
                    _exposure_source_reason(
                        configured_for_exposure=configured_for_exposure,
                        explicitly_enabled=explicitly_enabled,
                    ),
                ),
                facts=facts,
            )
        return ToolExposureDecision(
            exposed=False,
            excluded_reasons=("generate_not_enabled_by_visibility",),
            facts=facts,
        )
    if category == "write":
        if configured_for_exposure or explicitly_enabled:
            return ToolExposureDecision(
                exposed=True,
                reasons=(
                    "tool_category:write",
                    _exposure_source_reason(
                        configured_for_exposure=configured_for_exposure,
                        explicitly_enabled=explicitly_enabled,
                    ),
                ),
                facts=facts,
            )
        return ToolExposureDecision(
            exposed=False,
            excluded_reasons=("write_not_enabled_by_visibility",),
            facts=facts,
        )
    if explicitly_enabled:
        return ToolExposureDecision(
            exposed=True,
            reasons=("tool_category:dangerous", "explicit_tool_exposure"),
            facts=facts,
        )
    return ToolExposureDecision(
        exposed=False,
        excluded_reasons=("dangerous_not_explicitly_enabled",),
        facts=facts,
    )


def tool_exposure_category(policy: ToolPolicyView) -> ToolExposureCategory:
    """Classify one tool for default exposure governance."""

    if (
        policy.tool_name in _DANGEROUS_TOOL_NAMES
        or policy.realtime_safety == "unsafe"
        or policy.toolset == "analysis.local"
    ):
        return "dangerous"
    if (
        policy.side_effect_level == "compensatable"
        or policy.risk_gate_level == "soft_gate"
    ):
        return "generate"
    if (
        policy.requires_confirmation
        or policy.side_effect_level in {"pending_confirmation", "committed"}
        or policy.resource_writes
    ):
        return "write"
    return "read"


def _exposure_source_reason(
    *,
    configured_for_exposure: bool,
    explicitly_enabled: bool,
) -> str:
    if explicitly_enabled:
        return "explicit_tool_exposure"
    if configured_for_exposure:
        return "configured_tool_exposure"
    return "default_tool_exposure"


def _has_required_media(policy: ToolPolicyView, facts: ToolExposureFacts) -> bool:
    required = set(policy.requires_media)
    if required:
        return required.issubset(facts.active_media_types)
    implicit_any = _MEDIA_BOUND_TOOL_TYPES.get(policy.tool_name)
    if implicit_any:
        return bool(implicit_any.intersection(facts.active_media_types))
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


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
