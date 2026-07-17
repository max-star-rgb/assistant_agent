from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.services.provider_selection import create_vision_adapter
from assistant_agent.services.vision_adapter import (
    MockVisionUnderstandingAdapter,
    VisionUnderstandingInput,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.vision_tool import VisionUnderstandingTool
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


def _configured_vision_tool() -> tuple[VisionUnderstandingTool, ProviderConfig]:
    skip_unless_manual_tool_provider_smoke_selected(
        THIS_FILE,
        env_var="RUN_REAL_VISION_IMAGE_TOOL_TEST",
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
    provider = config.resolved_vision_provider()
    missing = provider.missing_required_env()
    if missing:
        pytest.skip(f"set {', '.join(missing)} for {provider.provider} vision")
    adapter = create_vision_adapter(config)
    if isinstance(adapter, MockVisionUnderstandingAdapter):
        pytest.skip("set MULTIMODAL_AGENT_VISION_PROVIDER=openai|qwen|ark")
    return VisionUnderstandingTool(adapter=adapter), config


def test_real_vision_understanding_tool_understands_attached_image_1_provider_smoke(
    capsys,
) -> None:
    tool, config = _configured_vision_tool()
    frame = attached_image_1_as_jpeg()

    result = tool.run(
        VisionUnderstandingInput(
            image_ids=[str(frame)],
            question="识别这张图中的主体、食材和装饰。",
        ),
        ToolContext(),
    )

    with capsys.disabled():
        print()
        print_json_section(
            "VISION PROVIDER CONFIG",
            _provider_config_diagnostics(config),
        )
        print_text_section(
            "VISION RAW OUTPUT",
            getattr(tool.adapter, "last_raw_response_text", None),
            empty="<raw provider text is not exposed by this adapter>",
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
            _success_layers(result),
        )

    layers = _success_layers(result)
    assert layers["execution_success"] is True, layers
    assert layers["semantic_success"] is True, layers
    serialized = json.dumps(result.data, ensure_ascii=False).lower()
    assert any(
        token in serialized
        for token in ("蛋糕", "cake", "草莓", "strawberry", "蓝莓", "blueberry")
    )


def _success_layers(result) -> dict[str, object]:
    return visual_tool_success_layers(result)


def _provider_config_diagnostics(config: ProviderConfig) -> dict[str, object]:
    provider = config.resolved_vision_provider()
    return {
        "python": sys.executable,
        "cwd": str(Path.cwd()),
        "runtime_profile": config.runtime_profile.name,
        "vision_provider": provider.provider,
        "adapter_kind": provider.adapter_kind,
        "base_url": provider.base_url,
        "model": provider.model,
        "credential_source": provider.spec.api_key_env,
        "proxy_env_names": proxy_env_names(),
    }
