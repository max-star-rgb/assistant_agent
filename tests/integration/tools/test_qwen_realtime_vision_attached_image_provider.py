from __future__ import annotations

import json
import os
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
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.video_tool import VideoUnderstandingTool
from manual_tool_smoke import (
    attached_image_1_as_jpeg,
    filtered_tool_result_for_llm,
    print_json_section,
    print_text_section,
    proxy_env_names,
    skip_unless_manual_tool_provider_smoke_selected,
    visual_tool_success_layers,
)


THIS_FILE = Path(__file__)


def _configured_qwen_tool() -> tuple[
    VideoUnderstandingTool, QwenRealtimeVisionAdapter, ProviderConfig
]:
    skip_unless_manual_tool_provider_smoke_selected(
        THIS_FILE,
        env_var="RUN_REAL_VLM_IMAGE_TEST",
    )
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
    frame = attached_image_1_as_jpeg()
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
        print_json_section(
            "VLM PROVIDER CONFIG",
            _provider_config_diagnostics(config),
        )
        print_text_section(
            "VLM RAW OUTPUT",
            adapter.last_raw_response_text,
            empty="<no raw VLM text received>",
        )
        print_json_section(
            "TOOL RESULT",
            result.model_dump(mode="json"),
        )
        print_json_section(
            "FILTERED TOOL RESULT (LLM-FACING)",
            filtered_tool_result_for_llm(result),
        )
        print_json_section(
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
    return visual_tool_success_layers(
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
        "proxy_env_names": proxy_env_names(),
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
