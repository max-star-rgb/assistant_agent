"""Shared model-facing domain values used by native agents."""

from __future__ import annotations

from typing import Literal


ProviderSearchProfile = Literal[
    "none",
    "rail_official",
    "flight_official",
    "guide_official",
    "guide_xiaohongshu",
    "travel_general",
]


__all__ = ["ProviderSearchProfile"]
