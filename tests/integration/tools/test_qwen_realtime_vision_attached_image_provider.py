from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.providers.qwen_realtime_vision import (
    DEFAULT_FORCE_IPV4_DIRECT_CONNECTION,
    DEFAULT_TCP_CONNECT_TIMEOUT_SECONDS,
    QwenRealtimeVisionAdapter,
)
from assistant_agent.schemas.perception import VideoUnderstandingRequest
from assistant_agent.services.video_adapter import (
    create_realtime_video_understanding_adapter,
)
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.video_tool import VideoUnderstandingTool


THIS_FILE = Path(__file__).resolve()
REPO_ROOT = Path(__file__).resolve().parents[3]
ATTACHED_IMAGE_1 = Path("/home/lenovo1/图片/0717619c-e0de-4fe7-b21c-50b6754eb8b8.png")
DOWNSAMPLED_IMAGE_1_JPEG = (
    REPO_ROOT / ".local" / "integration" / "tools" / "image-1-cake-480p.jpg"
)


def _configured_qwen_tool() -> tuple[
    VideoUnderstandingTool, QwenRealtimeVisionAdapter, ProviderConfig
]:
    _skip_unless_manual_tool_selected("RUN_REAL_VLM_IMAGE_TEST")
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
    return VideoUnderstandingTool(adapter=adapter), adapter, config


def test_real_qwen_vlm_tool_understands_attached_image_1_provider_smoke(
    capsys,
) -> None:
    tool, adapter, config = _configured_qwen_tool()
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
        print()
        _print_json_section(
            "VLM PROVIDER CONFIG",
            _provider_config_diagnostics(config),
        )
        _print_text_section(
            "VLM RAW OUTPUT",
            adapter.last_raw_response_text,
            empty="<no raw VLM text received>",
        )
        _print_json_section(
            "TOOL RESULT",
            result.model_dump(mode="json"),
        )
        _print_json_section(
            "FILTERED TOOL RESULT (LLM-FACING)",
            _filtered_tool_result_for_llm(result),
        )
        _print_json_section(
            "SUCCESS LAYERS",
            _success_layers(result, adapter),
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
    return _visual_tool_success_layers(
        result,
        raw_text_received=bool(adapter.last_raw_response_text),
        snapshot_source="background_keyframe_observation",
        extra={
            "observation_phase": adapter.last_observation_phase,
            "diagnostics": adapter.last_observation_diagnostics,
        },
    )


def _provider_config_diagnostics(config: ProviderConfig) -> dict[str, object]:
    return {
        "python": sys.executable,
        "cwd": str(Path.cwd()),
        "runtime_profile": config.runtime_profile.name,
        "vision_provider": config.vision_provider,
        "base_url": config.qwen_realtime_vision_base_url,
        "model": config.qwen_realtime_vision_model,
        "workspace_id_configured": config.qwen_realtime_vision_workspace_id is not None,
        "region": config.qwen_realtime_vision_region,
        "endpoint_style": _endpoint_style(config),
        "timeout_seconds": config.video_understanding_timeout_seconds,
        "tcp_connect_timeout_cap_seconds": DEFAULT_TCP_CONNECT_TIMEOUT_SECONDS,
        "direct_ipv4_default": DEFAULT_FORCE_IPV4_DIRECT_CONNECTION,
        "credential_source": _credential_source(),
        "proxy_env_names": _proxy_env_names(),
    }


def _endpoint_style(config: ProviderConfig) -> str:
    if os.environ.get("QWEN_REALTIME_VISION_BASE_URL"):
        return "explicit_base_url"
    if config.qwen_realtime_vision_workspace_id is not None:
        return "workspace_region"
    return "legacy_dashscope_default"


def _credential_source() -> str | None:
    if os.environ.get("QWEN_VISION_API_KEY"):
        return "QWEN_VISION_API_KEY"
    if os.environ.get("DASHSCOPE_API_KEY"):
        return "DASHSCOPE_API_KEY"
    return None


def _skip_unless_manual_tool_selected(env_var: str) -> None:
    if _manual_tool_selected(env_var):
        return
    pytest.skip(f"run this test file directly, or set {env_var}=1")


def _manual_tool_selected(env_var: str) -> bool:
    if os.environ.get(env_var) == "1":
        return True
    if os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE_COUNT") != "1":
        return False
    selected_file = os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE")
    return bool(selected_file and Path(selected_file).resolve() == THIS_FILE)


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
        pytest.skip(
            "/usr/bin/ffmpeg is required to convert Image #1 to the JPEG VLM input"
        )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")
        pytest.fail(f"failed to downsample Image #1 to 480p JPEG: {message}")
    return DOWNSAMPLED_IMAGE_1_JPEG


def _is_jpeg(path: Path) -> bool:
    if not path.is_file():
        return False
    data = path.read_bytes()
    return data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")


def _print_json_section(title: str, value: object) -> None:
    print(f"=== {title} ===", flush=True)
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def _print_text_section(title: str, text: str | None, *, empty: str) -> None:
    print(f"=== {title} ===", flush=True)
    print(text or empty, flush=True)


def _filtered_tool_result_for_llm(result) -> dict[str, object]:
    return observation_from_tool_result(result).model_dump(mode="json")


def _visual_tool_success_layers(
    result,
    *,
    raw_text_received: bool | None = None,
    snapshot_source: str | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    data = result.data if isinstance(result.data, dict) else {}
    errors = data.get("errors")
    data_errors = errors if isinstance(errors, list) else []
    summary = data.get("summary")
    source = data.get("source")
    execution_success = result.success is True
    semantic_success = (
        execution_success
        and isinstance(summary, str)
        and bool(summary.strip())
        and not data_errors
    )
    layers: dict[str, object] = {
        "execution_success": execution_success,
        "semantic_success": semantic_success,
        "tool_success": result.success,
        "contract_status": result.contract.status
        if result.contract is not None
        else None,
        "source": source,
        "error": result.error,
        "data_errors": data_errors,
    }
    if snapshot_source is not None:
        layers["snapshot_publishable"] = semantic_success and source == snapshot_source
    if raw_text_received is not None:
        layers["raw_text_received"] = raw_text_received
    if extra:
        layers.update(extra)
    return layers


def _proxy_env_names() -> list[str]:
    return sorted(
        key
        for key in os.environ
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    )
