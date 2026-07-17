from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.providers.qwen_realtime_vision import QwenRealtimeVisionAdapter
from assistant_agent.schemas.perception import VideoUnderstandingRequest
from assistant_agent.services.video_adapter import create_realtime_video_understanding_adapter
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.video_tool import VideoUnderstandingTool


ATTACHED_IMAGE_1 = Path("/home/lenovo1/图片/0717619c-e0de-4fe7-b21c-50b6754eb8b8.png")


def _configured_qwen_tool() -> tuple[VideoUnderstandingTool, QwenRealtimeVisionAdapter]:
    if not _real_provider_tool_selected():
        pytest.skip("run this file directly, or set RUN_REAL_VLM_IMAGE_TEST=1")
    env = {
        **os.environ,
        "MULTIMODAL_AGENT_RUNTIME_PROFILE": os.environ.get(
            "MULTIMODAL_AGENT_RUNTIME_PROFILE", "provider_smoke"
        ),
        "MULTIMODAL_AGENT_VISION_PROVIDER": os.environ.get(
            "MULTIMODAL_AGENT_VISION_PROVIDER", "qwen"
        ),
    }
    config = ProviderConfig.from_env(env)
    if config.runtime_profile.name not in {"provider_smoke", "pilot"}:
        pytest.skip("set MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke or pilot")
    if config.vision_provider != "qwen":
        pytest.skip("set MULTIMODAL_AGENT_VISION_PROVIDER=qwen")
    if not config.qwen_realtime_vision_api_key:
        pytest.skip("set QWEN_VISION_API_KEY or DASHSCOPE_API_KEY")
    adapter = create_realtime_video_understanding_adapter(config)
    assert isinstance(adapter, QwenRealtimeVisionAdapter)
    return VideoUnderstandingTool(adapter=adapter), adapter


def _real_provider_tool_selected() -> bool:
    return (
        os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED") == "1"
        or os.environ.get("RUN_REAL_VLM_IMAGE_TEST") == "1"
    )


def _attached_image_as_jpeg(tmp_path: Path) -> Path:
    if not ATTACHED_IMAGE_1.is_file():
        pytest.skip(f"Image #1 is not available at {ATTACHED_IMAGE_1}")
    output = tmp_path / "image-1-provider-input.jpg"
    command = [
        "/usr/bin/ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(ATTACHED_IMAGE_1),
        "-frames:v",
        "1",
        "-vf",
        "scale=1024:1024:force_original_aspect_ratio=decrease",
        "-vcodec",
        "mjpeg",
        "-q:v",
        "6",
        str(output),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True)
    except OSError:
        pytest.skip("/usr/bin/ffmpeg is required to convert Image #1 to the JPEG VLM input")
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")
        pytest.fail(f"failed to convert Image #1 to JPEG: {message}")
    return output


def test_real_qwen_vlm_tool_understands_attached_image_1_provider_smoke(
    tmp_path: Path,
) -> None:
    tool, adapter = _configured_qwen_tool()
    frame = _attached_image_as_jpeg(tmp_path)
    try:
        result = tool.run(
            VideoUnderstandingRequest(
                video_ref="attached-image-1-cake",
                frame_refs=[str(frame)],
                user_query="识别这张图中的主体、食材和装饰。",
                metadata={"frame_sequence": 1},
            ),
            ToolContext(metadata={"realtime_video_observation": True}),
        )
    finally:
        adapter.close()

    assert result.success is True
    assert result.data is not None
    assert result.data["source"] == "background_keyframe_observation"
    assert result.data["errors"] == []
    assert result.data["summary"]
    assert adapter.last_raw_response_text
    serialized = json.dumps(result.data, ensure_ascii=False).lower()
    assert any(
        token in serialized
        for token in ("蛋糕", "cake", "草莓", "strawberry", "蓝莓", "blueberry")
    )
    print("\n=== VLM RAW OUTPUT ===")
    print(adapter.last_raw_response_text)
    print("=== TOOL RESULT ===")
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
