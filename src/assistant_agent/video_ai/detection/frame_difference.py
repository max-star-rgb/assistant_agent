"""Pixel-level frame difference detection."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, sqrt
from typing import Any, Iterable

from assistant_agent.video_ai.types import VideoFrame


DEFAULT_FINGERPRINT_SIZE = (160, 90)


@dataclass(frozen=True)
class FrameDifferenceResult:
    """Pixel and region-change signals between two frames."""

    pixel_change_score: float
    object_change_score: float


class FrameDifferenceDetector:
    """Compute a low-resolution mean absolute difference score."""

    def __init__(self, *, fingerprint_size: tuple[int, int] = DEFAULT_FINGERPRINT_SIZE) -> None:
        self.fingerprint_size = fingerprint_size

    def compare(self, current: VideoFrame, reference: VideoFrame | None) -> FrameDifferenceResult:
        if reference is None:
            return FrameDifferenceResult(pixel_change_score=1.0, object_change_score=1.0)
        current_values = grayscale_fingerprint(current, self.fingerprint_size)
        reference_values = grayscale_fingerprint(reference, self.fingerprint_size)
        if not current_values or not reference_values:
            return FrameDifferenceResult(pixel_change_score=0.0, object_change_score=0.0)

        count = min(len(current_values), len(reference_values))
        diffs = [abs(current_values[index] - reference_values[index]) for index in range(count)]
        pixel_score = _clamp(sum(diffs) / count)
        object_score = _object_change_score(diffs)
        return FrameDifferenceResult(pixel_change_score=pixel_score, object_change_score=object_score)


def grayscale_fingerprint(frame: VideoFrame, size: tuple[int, int] = DEFAULT_FINGERPRINT_SIZE) -> list[float]:
    """Return a normalized grayscale fingerprint resized by nearest neighbor."""

    target_width, target_height = size
    values, width, height = _extract_grayscale(frame)
    if not values or width <= 0 or height <= 0:
        return []
    if width == target_width and height == target_height:
        return values

    resized: list[float] = []
    for y in range(target_height):
        source_y = min(height - 1, int(y * height / target_height))
        row_offset = source_y * width
        for x in range(target_width):
            source_x = min(width - 1, int(x * width / target_width))
            resized.append(values[row_offset + source_x])
    return resized


def _extract_grayscale(frame: VideoFrame) -> tuple[list[float], int, int]:
    pixels = frame.pixels
    if pixels is None:
        signature = frame.metadata.get("pixel_signature")
        if signature is not None:
            pixels = signature
        else:
            return [], 0, 0

    if hasattr(pixels, "tolist"):
        pixels = pixels.tolist()
    if isinstance(pixels, bytes | bytearray | memoryview):
        return _bytes_to_grayscale(bytes(pixels), frame.width, frame.height)
    if isinstance(pixels, str):
        return _bytes_to_grayscale(pixels.encode("utf-8"), frame.width, frame.height)
    if _is_sequence(pixels):
        return _sequence_to_grayscale(pixels, frame.width, frame.height)
    return [], 0, 0


def _bytes_to_grayscale(raw: bytes, width: int | None, height: int | None) -> tuple[list[float], int, int]:
    if not raw:
        return [], 0, 0
    if width and height and width * height <= len(raw):
        limit = width * height
        return [_normalize(raw[index]) for index in range(limit)], width, height
    side = max(1, int(sqrt(len(raw))))
    limit = side * side
    sampled = raw[:limit]
    return [_normalize(value) for value in sampled], side, side


def _sequence_to_grayscale(pixels: Any, width: int | None, height: int | None) -> tuple[list[float], int, int]:
    rows = list(pixels)
    if not rows:
        return [], 0, 0
    if _is_sequence(rows[0]) and not _looks_like_color(rows[0]):
        matrix = [list(row) for row in rows if _is_sequence(row)]
        if not matrix:
            return [], 0, 0
        resolved_height = len(matrix)
        resolved_width = max(len(row) for row in matrix)
        values: list[float] = []
        for row in matrix:
            padded = [*row, *([row[-1]] * max(0, resolved_width - len(row)))] if row else [0] * resolved_width
            values.extend(_normalize(value) for value in padded[:resolved_width])
        return values, resolved_width, resolved_height

    flat = [_normalize(value) for value in rows]
    if width and height and width * height <= len(flat):
        return flat[: width * height], width, height
    resolved_width = width or max(1, int(sqrt(len(flat))))
    resolved_height = height or max(1, ceil(len(flat) / resolved_width))
    padded = [*flat, *([flat[-1] if flat else 0.0] * max(0, resolved_width * resolved_height - len(flat)))]
    return padded[: resolved_width * resolved_height], resolved_width, resolved_height


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, str | bytes | bytearray | memoryview)


def _looks_like_color(value: Any) -> bool:
    if not _is_sequence(value):
        return False
    items = list(value)
    return 3 <= len(items) <= 4 and all(isinstance(item, int | float) for item in items)


def _normalize(value: Any) -> float:
    if _looks_like_color(value):
        channels = list(value)[:3]
        return _clamp(sum(float(channel) for channel in channels) / (3.0 * 255.0))
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        numeric = float(value)
        if numeric > 1.0:
            numeric /= 255.0
        return _clamp(numeric)
    return 0.0


def _object_change_score(diffs: list[float]) -> float:
    if not diffs:
        return 0.0
    changed = [diff for diff in diffs if diff >= 0.08]
    changed_ratio = len(changed) / len(diffs)
    intensity = sum(changed) / len(changed) if changed else 0.0
    return _clamp((changed_ratio * 0.65) + (intensity * 0.35))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
