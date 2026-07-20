"""Canonical tool manifest and resolver helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ToolExposureClass = Literal["read", "generate", "write", "dangerous"]

DIRECT_CHAT_CAPABILITY = "direct_chat"
MULTI_STEP_ORCHESTRATION_CAPABILITY = "multi_step_orchestration"
ASK_FOLLOWUP_CAPABILITY = "ask_followup"
IMAGE_UNDERSTANDING_CAPABILITY = "image_understanding"
IMAGE_UNDERSTANDING_TOOL_NAME = "vision_understanding"
VIDEO_UNDERSTANDING_CAPABILITY = "video_understanding"
VIDEO_UNDERSTANDING_TOOL_NAME = "video_understanding"
IMAGE_GENERATION_CAPABILITY = "image_generation"
IMAGE_GENERATION_TOOL_NAME = "image_generation"
WEB_SEARCH_CAPABILITY = "web_search"
WEB_SEARCH_TOOL_NAME = "web_search"
WEB_FETCH_CAPABILITY = "web_fetch"
WEB_FETCH_TOOL_NAME = "web_fetch"
VISUAL_IMAGE_SEARCH_CAPABILITY = "visual_image_search"
VISUAL_IMAGE_SEARCH_TOOL_NAME = "visual_image_search"
SHOPPING_SEARCH_CAPABILITY = "shopping_search"
SHOPPING_SEARCH_TOOL_NAME = "shopping_search"
RENDER_3D_CAPABILITY = "render_3d"
RENDER_3D_TOOL_NAME = "render_3d"
MEMORY_RETRIEVAL_CAPABILITY = "memory_retrieval"
MEMORY_RETRIEVAL_TOOL_NAME = "memory_retrieval"
MEMORY_SAVE_CAPABILITY = "memory_save"
MEMORY_SAVE_TOOL_NAME = "memory_save"
MEMORY_MEDIA_INGEST_TOOL_NAME = "memory_media_ingest"
MEMORY_INGEST_STATUS_TOOL_NAME = "memory_ingest_status"
WEATHER_TOOL_NAME = "weather"
CALENDAR_SEARCH_TOOL_NAME = "calendar_search"
CALENDAR_CREATE_TOOL_NAME = "calendar_create"
CONTACTS_SEARCH_TOOL_NAME = "contacts_search"
REMINDER_CREATE_TOOL_NAME = "reminder_create"
TOOL_SEARCH_TOOL_NAME = "tool_search"
PYTHON_INTERPRETER_TOOL_NAME = "python_interpreter"
DELEGATE_TO_AGENT_TOOL_NAME = "delegate_to_agent"
TASK_PLAN_SUBMIT_TOOL_NAME = "task_plan_submit"

REMOVED_SHOPPING_TOOL_NAMES = ("product_search", "price_compare")
LEGACY_SHOPPING_ACTION_ALIASES = ("search_product", "compare_price")
SHOPPING_SEARCH_PROVIDER_BINDINGS = (
    "shopping_search.search_provider",
    "shopping_search.compare_provider",
)
LEGACY_SHOPPING_PROVIDER_FIELDS = (
    "product_search_provider",
    "price_compare_provider",
)


@dataclass(frozen=True)
class ToolManifest:
    """Stable identity facts for one public assistant tool."""

    public_name: str
    capability: str
    exposure_class: ToolExposureClass
    action: str | None = None
    removed_tool_aliases: tuple[str, ...] = ()
    legacy_intent_aliases: tuple[str, ...] = ()
    legacy_action_aliases: tuple[str, ...] = ()
    provider_bindings: tuple[str, ...] = ()
    legacy_provider_fields: tuple[str, ...] = ()


TOOL_MANIFESTS: tuple[ToolManifest, ...] = (
    ToolManifest(
        public_name=IMAGE_UNDERSTANDING_TOOL_NAME,
        capability=IMAGE_UNDERSTANDING_CAPABILITY,
        exposure_class="read",
        action="understand_image",
        legacy_intent_aliases=("understand_image",),
    ),
    ToolManifest(
        public_name=VIDEO_UNDERSTANDING_TOOL_NAME,
        capability=VIDEO_UNDERSTANDING_CAPABILITY,
        exposure_class="read",
        action="understand_video",
        legacy_intent_aliases=("understand_video",),
    ),
    ToolManifest(
        public_name=IMAGE_GENERATION_TOOL_NAME,
        capability=IMAGE_GENERATION_CAPABILITY,
        exposure_class="generate",
        action="generate_image",
        legacy_intent_aliases=("generate_image",),
    ),
    ToolManifest(
        public_name=WEB_SEARCH_TOOL_NAME,
        capability=WEB_SEARCH_CAPABILITY,
        exposure_class="read",
        action="search_web",
        legacy_intent_aliases=("search_web",),
    ),
    ToolManifest(
        public_name=WEB_FETCH_TOOL_NAME,
        capability=WEB_FETCH_CAPABILITY,
        exposure_class="read",
        action="fetch_web",
        legacy_intent_aliases=("fetch_web", "read_url"),
        legacy_action_aliases=("read_url",),
    ),
    ToolManifest(
        public_name=VISUAL_IMAGE_SEARCH_TOOL_NAME,
        capability=VISUAL_IMAGE_SEARCH_CAPABILITY,
        exposure_class="read",
        action="search_image_by_image",
        legacy_intent_aliases=("search_image_by_image",),
    ),
    ToolManifest(
        public_name=SHOPPING_SEARCH_TOOL_NAME,
        capability=SHOPPING_SEARCH_CAPABILITY,
        exposure_class="read",
        action=SHOPPING_SEARCH_CAPABILITY,
        removed_tool_aliases=REMOVED_SHOPPING_TOOL_NAMES,
        legacy_intent_aliases=REMOVED_SHOPPING_TOOL_NAMES,
        legacy_action_aliases=LEGACY_SHOPPING_ACTION_ALIASES,
        provider_bindings=SHOPPING_SEARCH_PROVIDER_BINDINGS,
        legacy_provider_fields=LEGACY_SHOPPING_PROVIDER_FIELDS,
    ),
    ToolManifest(
        public_name=RENDER_3D_TOOL_NAME,
        capability=RENDER_3D_CAPABILITY,
        exposure_class="generate",
        action=RENDER_3D_CAPABILITY,
        legacy_intent_aliases=(RENDER_3D_CAPABILITY,),
    ),
    ToolManifest(
        public_name=MEMORY_RETRIEVAL_TOOL_NAME,
        capability=MEMORY_RETRIEVAL_CAPABILITY,
        exposure_class="read",
        action="retrieve_memory",
        legacy_intent_aliases=("retrieve_memory",),
    ),
    ToolManifest(
        public_name=MEMORY_SAVE_TOOL_NAME,
        capability=MEMORY_SAVE_CAPABILITY,
        exposure_class="write",
        action="save_memory",
        legacy_intent_aliases=("save_memory",),
    ),
    ToolManifest(
        public_name=MEMORY_MEDIA_INGEST_TOOL_NAME,
        capability=MEMORY_MEDIA_INGEST_TOOL_NAME,
        exposure_class="write",
    ),
    ToolManifest(
        public_name=MEMORY_INGEST_STATUS_TOOL_NAME,
        capability=MEMORY_INGEST_STATUS_TOOL_NAME,
        exposure_class="read",
    ),
    ToolManifest(public_name=WEATHER_TOOL_NAME, capability=WEATHER_TOOL_NAME, exposure_class="read"),
    ToolManifest(
        public_name=CALENDAR_SEARCH_TOOL_NAME,
        capability=CALENDAR_SEARCH_TOOL_NAME,
        exposure_class="read",
    ),
    ToolManifest(
        public_name=CALENDAR_CREATE_TOOL_NAME,
        capability=CALENDAR_CREATE_TOOL_NAME,
        exposure_class="write",
    ),
    ToolManifest(
        public_name=CONTACTS_SEARCH_TOOL_NAME,
        capability=CONTACTS_SEARCH_TOOL_NAME,
        exposure_class="read",
    ),
    ToolManifest(
        public_name=REMINDER_CREATE_TOOL_NAME,
        capability=REMINDER_CREATE_TOOL_NAME,
        exposure_class="write",
    ),
    ToolManifest(
        public_name=TOOL_SEARCH_TOOL_NAME,
        capability=TOOL_SEARCH_TOOL_NAME,
        exposure_class="read",
    ),
    ToolManifest(
        public_name=PYTHON_INTERPRETER_TOOL_NAME,
        capability=PYTHON_INTERPRETER_TOOL_NAME,
        exposure_class="dangerous",
    ),
)

_MANIFEST_BY_PUBLIC_NAME = {manifest.public_name: manifest for manifest in TOOL_MANIFESTS}
_PUBLIC_NAME_BY_CAPABILITY = {manifest.capability: manifest.public_name for manifest in TOOL_MANIFESTS}
_CAPABILITY_BY_PUBLIC_NAME = {manifest.public_name: manifest.capability for manifest in TOOL_MANIFESTS}
_ACTION_BY_CAPABILITY = {
    manifest.capability: manifest.action
    for manifest in TOOL_MANIFESTS
    if manifest.action is not None
}
_CAPABILITY_BY_ACTION = {
    manifest.action: manifest.capability
    for manifest in TOOL_MANIFESTS
    if manifest.action is not None
}
_REMOVED_ALIAS_TO_PUBLIC_NAME = {
    alias: manifest.public_name
    for manifest in TOOL_MANIFESTS
    for alias in manifest.removed_tool_aliases
}
_LEGACY_INTENT_ALIAS_TO_CAPABILITY = {
    alias: manifest.capability
    for manifest in TOOL_MANIFESTS
    for alias in manifest.legacy_intent_aliases
}
_LEGACY_ACTION_ALIAS_TO_ACTION = {
    alias: manifest.capability
    for manifest in TOOL_MANIFESTS
    for alias in manifest.legacy_action_aliases
}


def public_tool_names() -> tuple[str, ...]:
    """Return manifest-owned public tool names."""

    return tuple(manifest.public_name for manifest in TOOL_MANIFESTS)


def removed_tool_names() -> tuple[str, ...]:
    """Return removed public tool names that must not be exposed."""

    return tuple(_REMOVED_ALIAS_TO_PUBLIC_NAME)


def manifest_for_tool_name(tool_name: str) -> ToolManifest | None:
    """Return the manifest for one canonical public tool name."""

    return _MANIFEST_BY_PUBLIC_NAME.get(tool_name)


def canonical_tool_for_capability(capability: str) -> str | None:
    """Return the canonical public tool for a capability, if manifest-owned."""

    return _PUBLIC_NAME_BY_CAPABILITY.get(capability)


def canonical_capability_for_tool(tool_name: str) -> str | None:
    """Return the capability for a canonical public tool, if manifest-owned."""

    return _CAPABILITY_BY_PUBLIC_NAME.get(tool_name)


def canonical_action_for_capability(capability: str) -> str | None:
    """Return the canonical planner action for a capability, if manifest-owned."""

    return _ACTION_BY_CAPABILITY.get(capability)


def canonical_capability_for_action(action: str) -> str | None:
    """Return the canonical capability for a planner action, if manifest-owned."""

    return _CAPABILITY_BY_ACTION.get(action) or _LEGACY_ACTION_ALIAS_TO_ACTION.get(action)


def legacy_intent_aliases() -> dict[str, str]:
    """Return legacy intent aliases mapped to canonical capabilities."""

    return dict(_LEGACY_INTENT_ALIAS_TO_CAPABILITY)


def replacement_for_removed_tool(tool_name: str) -> str | None:
    """Return the canonical replacement for a removed tool name."""

    return _REMOVED_ALIAS_TO_PUBLIC_NAME.get(tool_name)


def canonical_action_for_legacy_alias(action: str) -> str | None:
    """Return the canonical action for a removed legacy action alias."""

    return _LEGACY_ACTION_ALIAS_TO_ACTION.get(action)


def provider_bindings_for_tool(tool_name: str) -> tuple[str, ...]:
    """Return canonical provider binding labels for a tool."""

    manifest = manifest_for_tool_name(tool_name)
    return manifest.provider_bindings if manifest is not None else ()


def legacy_provider_fields_for_tool(tool_name: str) -> tuple[str, ...]:
    """Return legacy provider field names kept for config compatibility."""

    manifest = manifest_for_tool_name(tool_name)
    return manifest.legacy_provider_fields if manifest is not None else ()
