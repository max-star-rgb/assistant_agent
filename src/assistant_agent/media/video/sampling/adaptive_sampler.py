"""Adaptive frame sampler for realtime video streams."""

from __future__ import annotations

from dataclasses import dataclass

from assistant_agent.media.video.types import SamplingDecision


@dataclass(frozen=True)
class AdaptiveSamplerConfig:
    """Runtime knobs for adaptive candidate frame sampling."""

    base_input_fps: float = 5.0
    still_fps: float = 0.2
    normal_fps: float = 1.0
    active_fps: float = 5.0
    burst_fps: float = 5.0
    still_threshold: float = 0.05
    active_threshold: float = 0.2
    burst_threshold: float = 0.65
    burst_duration_seconds: float = 2.0
    immediate_change_threshold: float | None = None


class AdaptiveFrameSampler:
    """Throttle keyframe analysis according to recent change scores."""

    def __init__(self, config: AdaptiveSamplerConfig | None = None) -> None:
        self.config = config or AdaptiveSamplerConfig()
        self.current_sampling_rate = self.config.normal_fps
        self._last_sampled_at: float | None = None
        self._burst_until: float | None = None

    def should_sample(self, *, timestamp_seconds: float, change_score: float, force: bool = False) -> SamplingDecision:
        rate, reason = self._rate_for(timestamp_seconds, change_score)
        self.current_sampling_rate = rate
        if force:
            self._last_sampled_at = timestamp_seconds
            return SamplingDecision(sampled=True, sampling_rate=rate, reason="forced_interval")
        if (
            self._last_sampled_at is not None
            and self.config.immediate_change_threshold is not None
            and change_score >= self.config.immediate_change_threshold
        ):
            self._last_sampled_at = timestamp_seconds
            return SamplingDecision(sampled=True, sampling_rate=rate, reason="immediate_change")
        if self._last_sampled_at is None:
            self._last_sampled_at = timestamp_seconds
            return SamplingDecision(sampled=True, sampling_rate=rate, reason="initial")

        interval = 1.0 / max(rate, 0.001)
        if timestamp_seconds - self._last_sampled_at + 1e-9 >= interval:
            self._last_sampled_at = timestamp_seconds
            return SamplingDecision(sampled=True, sampling_rate=rate, reason=reason)
        return SamplingDecision(sampled=False, sampling_rate=rate, reason=reason)

    def _rate_for(self, timestamp_seconds: float, change_score: float) -> tuple[float, str]:
        if change_score >= self.config.burst_threshold:
            self._burst_until = max(self._burst_until or timestamp_seconds, timestamp_seconds + self.config.burst_duration_seconds)
            return self._cap_rate(self.config.burst_fps), "burst"
        if self._burst_until is not None and timestamp_seconds <= self._burst_until:
            return self._cap_rate(self.config.burst_fps), "burst_window"
        if change_score >= self.config.active_threshold:
            return self._cap_rate(self.config.active_fps), "active_change"
        if change_score <= self.config.still_threshold:
            return self.config.still_fps, "still"
        return self._cap_rate(self.config.normal_fps), "moderate_change"

    def _cap_rate(self, rate: float) -> float:
        return min(rate, max(self.config.base_input_fps, 0.001))
