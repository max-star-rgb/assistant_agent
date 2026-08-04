"""Structured per-turn tool exposure rules.

Registered tools are exposed by default. This module only applies structured
runtime constraints such as attached or trusted live media; it never infers
user intent from natural-language request text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.media.agent_service_entry import (
    is_trusted_agent_service_request,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolMediaScope, ToolSpec
from assistant_agent.tools.ids import VISUAL_MEMORY_SEARCH_TOOL_NAME


@dataclass(frozen=True)
class ToolExposureFacts:
    """Prompt-safe structured facts used to expose entry-profile tools."""

    active_image_ids: tuple[str, ...] = ()
    active_video_ids: tuple[str, ...] = ()
    active_audio_id: str | None = None
    trusted_live_video: bool = False
    visual_memory_available: bool = False

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

    @property
    def attached_media_types(self) -> frozenset[str]:
        active = set(self.active_media_types)
        if self.trusted_live_video:
            active.discard("video")
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
        trusted_live_video=(
            bool(_string_list(request.video_ids))
            and is_trusted_agent_service_request(request)
        ),
        visual_memory_available=request.metadata.get(
            "_trusted_visual_memory_available"
        ) is True,
    )


def evaluate_tool_exposure(
    request: UserRequest,
    spec: ToolSpec,
) -> ToolExposureDecision:
    """Return whether one tool is exposed for the current turn."""

    facts = tool_exposure_facts(request)
    if spec.name == VISUAL_MEMORY_SEARCH_TOOL_NAME and not facts.visual_memory_available:
        return ToolExposureDecision(
            exposed=False,
            excluded_reasons=("visual_memory_history_not_available",),
            facts=facts,
        )
    if not tool_media_requirements_satisfied(spec, facts):
        return ToolExposureDecision(
            exposed=False,
            excluded_reasons=(_media_exclusion_reason(spec.media_scope),),
            facts=facts,
        )
    return ToolExposureDecision(
        exposed=True,
        reasons=(f"tool_category:{spec.category}",),
        facts=facts,
    )


def tool_media_requirements_satisfied(
    spec: ToolSpec,
    facts: ToolExposureFacts,
) -> bool:
    """Return whether typed media facts satisfy a tool's media contract."""

    required = set(spec.requires_media)
    if not required:
        return True
    if spec.media_scope == "attached":
        return bool(required.intersection(facts.attached_media_types))
    if spec.media_scope == "live":
        return facts.trusted_live_video and "video" in required
    return bool(required.intersection(facts.active_media_types))


def _media_exclusion_reason(scope: ToolMediaScope) -> str:
    if scope == "attached":
        return "attached_media_not_available"
    if scope == "live":
        return "trusted_live_video_not_available"
    return "required_media_not_available"


def _string_list(value: list[str]) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
