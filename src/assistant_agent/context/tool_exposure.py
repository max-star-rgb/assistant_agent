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

from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolSpec

ToolExposureCategory = Literal["read", "generate", "write", "dangerous"]


@dataclass(frozen=True)
class ToolExposureFacts:
    """Prompt-safe structured facts used to expose entry-profile tools."""

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
        active_image_ids=tuple(_string_list(request.image_ids)),
        active_video_ids=tuple(_string_list(request.video_ids)),
        active_audio_id=_string_value(request.audio_id),
    )


def evaluate_tool_exposure(
    request: UserRequest,
    spec: ToolSpec,
    *,
    configured_for_exposure: bool = False,
    explicitly_enabled: bool = False,
) -> ToolExposureDecision:
    """Return whether one tool is exposed for the current turn."""

    facts = tool_exposure_facts(request)
    if not _has_required_media(spec, facts):
        return ToolExposureDecision(
            exposed=False,
            excluded_reasons=("required_media_not_available",),
            facts=facts,
        )
    category = tool_exposure_category(spec)
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


def tool_exposure_category(spec: ToolSpec) -> ToolExposureCategory:
    """Return the explicit category declared by the tool contract."""

    return spec.category


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


def _has_required_media(spec: ToolSpec, facts: ToolExposureFacts) -> bool:
    required = set(spec.requires_media)
    if required:
        return bool(required.intersection(facts.active_media_types))
    return True


def _string_list(value: list[str]) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
