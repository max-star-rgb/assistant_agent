from __future__ import annotations

from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.providers.qwen_realtime_vision import QwenRealtimeVisionAdapter
from assistant_agent.schemas.perception import VideoUnderstandingRequest
from assistant_agent.services.video_adapter import create_realtime_video_understanding_adapter


REPO_ROOT = Path(__file__).resolve().parents[2]
FRAME_ROOT = REPO_ROOT / "demo_data" / "videos" / "video1"


def _configured_adapter() -> QwenRealtimeVisionAdapter:
    config = ProviderConfig.from_env()
    if config.runtime_profile.name not in {"provider_smoke", "pilot"}:
        pytest.skip("set MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke or pilot")
    if config.vision_provider != "qwen":
        pytest.skip("set MULTIMODAL_AGENT_VISION_PROVIDER=qwen")
    if not config.qwen_realtime_vision_api_key:
        pytest.skip("set QWEN_API_KEY or DASHSCOPE_API_KEY")
    adapter = create_realtime_video_understanding_adapter(config)
    assert isinstance(adapter, QwenRealtimeVisionAdapter)
    return adapter


def _request(frame: Path, *, sequence: int) -> VideoUnderstandingRequest:
    return VideoUnderstandingRequest(
        video_ref="provider-smoke-camera",
        frame_refs=[str(frame)],
        user_query="只描述当前这一帧。",
        metadata={"frame_sequence": sequence},
    )


def _assert_success(adapter: QwenRealtimeVisionAdapter, frame: Path, *, sequence: int) -> None:
    result = adapter.understand_video(_request(frame, sequence=sequence))
    assert result.errors == []
    assert result.summary
    diagnostics = adapter.last_observation_diagnostics
    assert diagnostics["target_sequence"] == sequence
    assert diagnostics["completed_sequence"] == sequence
    _report_latency(diagnostics)


def _report_latency(diagnostics: dict[str, object]) -> None:
    first_delta_ms = diagnostics["first_delta_latency_ms"]
    total_ms = diagnostics["total_observation_latency_ms"]
    assert isinstance(first_delta_ms, int)
    assert isinstance(total_ms, int)
    print(
        "qwen_realtime_latency "
        f"first_delta_ms={first_delta_ms} target_lt_ms=500 met={first_delta_ms < 500} "
        f"total_ms={total_ms} target_lt_ms=1000 met={total_ms < 1000}"
    )


def test_qwen_realtime_single_frame_provider_smoke() -> None:
    adapter = _configured_adapter()
    try:
        _assert_success(adapter, FRAME_ROOT / "frame_000001.jpg", sequence=1)
    finally:
        adapter.close()


def test_qwen_realtime_five_frames_finish_on_latest_frame_provider_smoke() -> None:
    adapter = _configured_adapter()
    try:
        for sequence in range(1, 6):
            _assert_success(adapter, FRAME_ROOT / f"frame_{sequence:06d}.jpg", sequence=sequence)
        diagnostics = adapter.last_observation_diagnostics
        assert diagnostics["target_sequence"] == 5
        assert diagnostics["completed_sequence"] == 5
        assert diagnostics["connection_reused"] is True
    finally:
        adapter.close()


def test_qwen_realtime_recovers_after_forced_disconnect_provider_smoke() -> None:
    adapter = _configured_adapter()
    try:
        _assert_success(adapter, FRAME_ROOT / "frame_000001.jpg", sequence=1)
        first_generation = adapter.last_observation_diagnostics["session_generation"]
        adapter._discard_connection()
        _assert_success(adapter, FRAME_ROOT / "frame_000002.jpg", sequence=2)
        diagnostics = adapter.last_observation_diagnostics
        assert isinstance(first_generation, int)
        assert diagnostics["session_generation"] == first_generation + 1
        assert diagnostics["reconnect_count"] >= 1
        assert diagnostics["connection_reused"] is False
    finally:
        adapter.close()
