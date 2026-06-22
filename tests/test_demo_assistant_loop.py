import importlib.util
import os
import subprocess
import sys
from pathlib import Path


DEMO_SCRIPT = Path("scripts/demo_assistant_loop.py")


def test_demo_assistant_loop_loads_env_file_without_overwriting(tmp_path, monkeypatch) -> None:
    module = _load_demo_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke",
                "MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek",
                "DEEPSEEK_API_KEY=placeholder",
                "DEEPSEEK_CHAT_MODEL=deepseek-chat",
            ]
        ),
        encoding="utf-8",
    )

    keys = [
        "MULTIMODAL_AGENT_RUNTIME_PROFILE",
        "MULTIMODAL_AGENT_CHAT_PROVIDER",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_CHAT_MODEL",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    monkeypatch.delenv("MULTIMODAL_AGENT_CHAT_PROVIDER", raising=False)
    try:
        loaded = module.load_env_file(env_file)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert loaded["MULTIMODAL_AGENT_CHAT_PROVIDER"] == "deepseek"
    assert "DEEPSEEK_API_KEY" in loaded


def test_demo_assistant_loop_loads_qwen_image_default_size(tmp_path, monkeypatch) -> None:
    module = _load_demo_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke",
                "MULTIMODAL_AGENT_IMAGE_PROVIDER=qwen",
                "DASHSCOPE_API_KEY=placeholder",
                "QWEN_IMAGE_DEFAULT_SIZE=256*256",
            ]
        ),
        encoding="utf-8",
    )

    keys = [
        "MULTIMODAL_AGENT_RUNTIME_PROFILE",
        "MULTIMODAL_AGENT_IMAGE_PROVIDER",
        "DASHSCOPE_API_KEY",
        "QWEN_IMAGE_DEFAULT_SIZE",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    monkeypatch.delenv("QWEN_IMAGE_DEFAULT_SIZE", raising=False)
    try:
        module.load_env_file(env_file)
        config = module.ProviderConfig.from_env()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert config.image_generation_provider == "qwen"
    assert config.qwen_image_default_size == "256*256"


def test_demo_assistant_loop_mock_run_prints_react_steps() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "mock",
            "生成一张白色运动鞋的电商主图",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "ReAct steps" in result.stdout
    assert "action: image_generation" in result.stdout
    assert "tool_sequence: image_generation" in result.stdout
    assert "DEEPSEEK_API_KEY" not in result.stdout
    assert "Authorization" not in result.stdout
    assert "Traceback" not in result.stderr


def test_demo_assistant_loop_deepseek_missing_key_exits_cleanly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DEMO_SCRIPT),
            "--no-env-file",
            "--provider",
            "deepseek",
            "你好",
        ],
        env={"PATH": str(Path(sys.executable).parent), "PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "DEEPSEEK_API_KEY" in result.stdout
    assert "Authorization" not in result.stdout
    assert "Traceback" not in result.stderr


def _load_demo_module():
    module_name = "demo_assistant_loop_test"
    spec = importlib.util.spec_from_file_location(module_name, DEMO_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
