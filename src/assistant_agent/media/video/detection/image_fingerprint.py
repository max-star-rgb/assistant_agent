"""Small pixel normalization helper for legacy embedding adapters only."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from assistant_agent.media.video.types import VideoFrame


DEFAULT_FINGERPRINT_SIZE = (160, 90)


def grayscale_fingerprint(
    frame: VideoFrame,
    size: tuple[int, int] = DEFAULT_FINGERPRINT_SIZE,
) -> list[float]:
    """Normalize image-like test inputs and resample to a fixed vector."""

    pixels = frame.pixels
    if pixels is None:
        pixels = frame.metadata.get("pixel_signature")
    values = _flatten_grayscale(pixels)
    if not values:
        return []
    target_count = size[0] * size[1]
    if len(values) == target_count:
        return values
    return [
        values[min(len(values) - 1, int(index * len(values) / target_count))]
        for index in range(target_count)
    ]


def _flatten_grayscale(value: Any) -> list[float]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes | bytearray | memoryview):
        return [_normalize(item) for item in bytes(value)]
    if isinstance(value, Iterable) and not isinstance(
        value,
        str | bytes | bytearray | memoryview,
    ):
        items = list(value)
        if _looks_like_color(items):
            return [_normalize(items)]
        flattened: list[float] = []
        for item in items:
            flattened.extend(_flatten_grayscale(item))
        return flattened
    return [_normalize(value)]


def _looks_like_color(value: Any) -> bool:
    return (
        isinstance(value, list)
        and 3 <= len(value) <= 4
        and all(isinstance(item, int | float) for item in value)
    )


def _normalize(value: Any) -> float:
    if _looks_like_color(value):
        numeric = sum(float(item) for item in value[:3]) / 3.0
    elif isinstance(value, bool):
        numeric = 255.0 if value else 0.0
    elif isinstance(value, int | float):
        numeric = float(value)
    else:
        return 0.0
    if numeric > 1.0:
        numeric /= 255.0
    return max(0.0, min(1.0, numeric))
