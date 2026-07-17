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


REPO_ROOT = Path(__file__).resolve().parents[3]
ATTACHED_IMAGE_1 = Path("/home/lenovo1/图片/0717619c-e0de-4fe7-b21c-50b6754eb8b8.png")
DOWNSAMPLED_IMAGE_1_JPEG = REPO_ROOT / ".local" / "integration" / "tools" / "image-1-cake-480p.jpg"


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


def _attached_image_as_jpeg() -> Path:
    if _is_jpeg(DOWNSAMPLED_IMAGE_1_JPEG):
        return DOWNSAMPLED_IMAGE_1_JPEG
    if not ATTACHED_IMAGE_1.is_file():
        pytest.skip(f"Image #1 is not available at {ATTACHED_IMAGE_1}")
    DOWNSAMPLED_IMAGE_1_JPEG.parent.mkdir(parents=True, exist_ok=True)
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
        "scale=480:480:force_original_aspect_ratio=decrease",
        "-vcodec",
        "mjpeg",
        "-q:v",
        "6",
        str(DOWNSAMPLED_IMAGE_1_JPEG),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True)
    except OSError:
        pytest.skip("/usr/bin/ffmpeg is required to convert Image #1 to the JPEG VLM input")
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")
        pytest.fail(f"failed to downsample Image #1 to 480p JPEG: {message}")
    return DOWNSAMPLED_IMAGE_1_JPEG


def _is_jpeg(path: Path) -> bool:
    if not path.is_file():
        return False
    data = path.read_bytes()
    return data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")


def test_real_qwen_vlm_tool_understands_attached_image_1_provider_smoke(
    capsys,
) -> None:
    tool, adapter = _configured_qwen_tool()
    frame = _attached_image_as_jpeg()
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

    with capsys.disabled():
        print("\n=== VLM RAW OUTPUT ===", flush=True)
        print(adapter.last_raw_response_text or "<no raw VLM text received>", flush=True)
        print("=== TOOL RESULT ===", flush=True)
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), flush=True)
        print("=== SUCCESS LAYERS ===", flush=True)
        print(
            json.dumps(
                _success_layers(result, adapter),
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )

    layers = _success_layers(result, adapter)
    assert layers["execution_success"] is True, layers
    assert layers["semantic_success"] is True, layers
    assert layers["snapshot_publishable"] is True, layers
    assert adapter.last_raw_response_text
    serialized = json.dumps(result.data, ensure_ascii=False).lower()
    assert any(
        token in serialized
        for token in ("蛋糕", "cake", "草莓", "strawberry", "蓝莓", "blueberry")
    )


def _success_layers(result, adapter: QwenRealtimeVisionAdapter) -> dict[str, object]:
    data = result.data if isinstance(result.data, dict) else {}
    errors = data.get("errors")
    data_errors = errors if isinstance(errors, list) else []
    summary = data.get("summary")
    source = data.get("source")
    execution_success = result.success is True
    semantic_success = execution_success and isinstance(summary, str) and bool(summary) and not data_errors
    snapshot_publishable = semantic_success and source == "background_keyframe_observation"
    return {
        "execution_success": execution_success,
        "semantic_success": semantic_success,
        "snapshot_publishable": snapshot_publishable,
        "tool_success": result.success,
        "contract_status": result.contract.status if result.contract is not None else None,
        "source": source,
        "error": result.error,
        "data_errors": data_errors,
        "raw_text_received": bool(adapter.last_raw_response_text),
        "observation_phase": adapter.last_observation_phase,
        "diagnostics": adapter.last_observation_diagnostics,
    }
