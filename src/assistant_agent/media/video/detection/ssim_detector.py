"""Structural frame change detection with an SSIM-like score."""

from __future__ import annotations

from dataclasses import dataclass

from assistant_agent.media.video.detection.frame_difference import DEFAULT_FINGERPRINT_SIZE, grayscale_fingerprint
from assistant_agent.media.video.types import VideoFrame


@dataclass(frozen=True)
class StructuralChangeResult:
    """Structural similarity and derived change score."""

    similarity: float
    structural_change_score: float


class SSIMChangeDetector:
    """Compute structural change while damping plain lighting shifts."""

    def __init__(self, *, fingerprint_size: tuple[int, int] = DEFAULT_FINGERPRINT_SIZE) -> None:
        self.fingerprint_size = fingerprint_size

    def compare(self, current: VideoFrame, reference: VideoFrame | None) -> StructuralChangeResult:
        if reference is None:
            return StructuralChangeResult(similarity=0.0, structural_change_score=1.0)
        current_values = grayscale_fingerprint(current, self.fingerprint_size)
        reference_values = grayscale_fingerprint(reference, self.fingerprint_size)
        similarity = structural_similarity(current_values, reference_values)
        return StructuralChangeResult(similarity=similarity, structural_change_score=max(0.0, 1.0 - similarity))


def structural_similarity(left: list[float], right: list[float]) -> float:
    """Return a bounded SSIM approximation for two grayscale vectors."""

    count = min(len(left), len(right))
    if count == 0:
        return 1.0
    x = left[:count]
    y = right[:count]
    mean_x = sum(x) / count
    mean_y = sum(y) / count
    var_x = sum((value - mean_x) ** 2 for value in x) / count
    var_y = sum((value - mean_y) ** 2 for value in y) / count
    cov_xy = sum((x[index] - mean_x) * (y[index] - mean_y) for index in range(count)) / count
    c1 = 0.01**2
    c2 = 0.03**2
    denominator = ((mean_x**2 + mean_y**2 + c1) * (var_x + var_y + c2))
    if denominator == 0.0:
        return 1.0
    value = ((2 * mean_x * mean_y + c1) * (2 * cov_xy + c2)) / denominator
    return max(0.0, min(1.0, value))
