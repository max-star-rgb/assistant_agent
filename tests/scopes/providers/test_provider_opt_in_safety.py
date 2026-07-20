import subprocess
import sys
from pathlib import Path

import pytest


SMOKE_CASES = [
    (
        Path("scripts/smoke_direct_chat.py"),
        ["--text", "hello"],
        {"MULTIMODAL_AGENT_CHAT_PROVIDER": "openai"},
    ),
    (
        Path("scripts/smoke_text_image_generation.py"),
        ["--prompt", "生成一张海报"],
        {"MULTIMODAL_AGENT_IMAGE_PROVIDER": "openai"},
    ),
    (
        Path("scripts/smoke_real_vision.py"),
        ["--image", "/tmp/nonexistent-smoke-image.jpg"],
        {"MULTIMODAL_AGENT_VISION_PROVIDER": "openai"},
    ),
    (
        Path("scripts/smoke_product_search.py"),
        ["--query", "white sneaker"],
        {"MULTIMODAL_AGENT_SHOPPING_PROVIDER": "http", "PRODUCT_SEARCH_API_KEY": "sk-provider-safety-test"},
    ),
    (
        Path("scripts/smoke_price_compare.py"),
        ["--query", "white sneaker"],
        {
            "MULTIMODAL_AGENT_SHOPPING_PROVIDER": "http",
            "PRODUCT_SEARCH_BASE_URL": "https://provider-safety.test",
            "PRODUCT_SEARCH_API_KEY": "sk-provider-safety-test",
            "PRICE_COMPARE_API_KEY": "sk-provider-safety-test",
        },
    ),
    (
        Path("scripts/smoke_render_3d.py"),
        ["--scene", "客厅展示"],
        {"MULTIMODAL_AGENT_RENDER_PROVIDER": "http", "RENDER_API_KEY": "sk-provider-safety-test"},
    ),
    (
        Path("scripts/smoke_video_understanding.py"),
        ["--video-ref", "mock://video/demo"],
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "VIDEO_UNDERSTANDING_API_KEY": "sk-provider-safety-test",
        },
    ),
    (
        Path("scripts/smoke_native_tool_calling.py"),
        ["--real-provider", "--query", "white sneaker"],
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "",
        },
    ),
]


@pytest.mark.parametrize(("script_path", "args", "env"), SMOKE_CASES)
def test_smoke_missing_config_is_clear_and_redacted(script_path: Path, args: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, str(script_path), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "missing" in result.stdout
    assert "Traceback" not in output
    assert "sk-provider-safety-test" not in output
    assert "Bearer" not in output
    assert "Authorization" not in output
