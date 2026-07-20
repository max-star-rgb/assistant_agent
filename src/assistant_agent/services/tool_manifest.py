"""Canonical tool manifest and resolver helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ToolExposureClass = Literal["read", "generate", "write", "dangerous"]

SHOPPING_SEARCH_CAPABILITY = "shopping_search"
SHOPPING_SEARCH_TOOL_NAME = "shopping_search"
REMOVED_SHOPPING_TOOL_NAMES = ("product_search", "price_compare")
LEGACY_SHOPPING_ACTION_ALIASES = ("search_product", "compare_price")


@dataclass(frozen=True)
class ToolManifest:
    """Stable identity facts for one public assistant tool."""

    public_name: str
    capability: str
    exposure_class: ToolExposureClass
    removed_tool_aliases: tuple[str, ...] = ()
    legacy_action_aliases: tuple[str, ...] = ()


TOOL_MANIFESTS: tuple[ToolManifest, ...] = (
    ToolManifest(
        public_name=SHOPPING_SEARCH_TOOL_NAME,
        capability=SHOPPING_SEARCH_CAPABILITY,
        exposure_class="read",
        removed_tool_aliases=REMOVED_SHOPPING_TOOL_NAMES,
        legacy_action_aliases=LEGACY_SHOPPING_ACTION_ALIASES,
    ),
)

_MANIFEST_BY_PUBLIC_NAME = {manifest.public_name: manifest for manifest in TOOL_MANIFESTS}
_PUBLIC_NAME_BY_CAPABILITY = {manifest.capability: manifest.public_name for manifest in TOOL_MANIFESTS}
_CAPABILITY_BY_PUBLIC_NAME = {manifest.public_name: manifest.capability for manifest in TOOL_MANIFESTS}
_REMOVED_ALIAS_TO_PUBLIC_NAME = {
    alias: manifest.public_name
    for manifest in TOOL_MANIFESTS
    for alias in manifest.removed_tool_aliases
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


def replacement_for_removed_tool(tool_name: str) -> str | None:
    """Return the canonical replacement for a removed tool name."""

    return _REMOVED_ALIAS_TO_PUBLIC_NAME.get(tool_name)


def canonical_action_for_legacy_alias(action: str) -> str | None:
    """Return the canonical action for a removed legacy action alias."""

    return _LEGACY_ACTION_ALIAS_TO_ACTION.get(action)
