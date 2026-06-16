import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


DIRECT_CHAT_SCRIPT = Path("scripts/smoke_direct_chat.py")
IMAGE_GENERATION_SCRIPT = Path("scripts/smoke_text_image_generation.py")


@pytest.mark.parametrize("script_path", [DIRECT_CHAT_SCRIPT, IMAGE_GENERATION_SCRIPT])
def test_text_capability_smoke_script_import_is_safe(monkeypatch, script_path: Path) -> None:
    module_name = f"{script_path.stem}_import_test"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None
    assert spec.loader is not None

    import urllib.request

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("smoke script import must not call provider")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "main")


def test_direct_chat_smoke_missing_key_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(DIRECT_CHAT_SCRIPT), "--text", "帮我写一段商品介绍"],
        env={"MULTIMODAL_AGENT_CHAT_PROVIDER": "openai"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "missing OPENAI_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr


def test_text_image_generation_smoke_missing_key_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(IMAGE_GENERATION_SCRIPT), "--prompt", "生成一张日系极简商品海报"],
        env={"MULTIMODAL_AGENT_IMAGE_PROVIDER": "openai"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "missing OPENAI_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr


def test_direct_chat_smoke_default_mock_outputs_json() -> None:
    result = subprocess.run(
        [sys.executable, str(DIRECT_CHAT_SCRIPT), "--text", "帮我写一段商品介绍"],
        env={"MULTIMODAL_AGENT_CHAT_PROVIDER": "mock"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["provider"] == "mock"
    assert payload["capability"] == "direct_chat"
    assert payload["intent"] == "direct_chat"
    assert payload["tool_calls"] == []
    assert payload["contract"]["capability"] == "direct_chat"


def test_text_image_generation_smoke_default_mock_outputs_json() -> None:
    result = subprocess.run(
        [sys.executable, str(IMAGE_GENERATION_SCRIPT), "--prompt", "生成一张日系极简商品海报"],
        env={"MULTIMODAL_AGENT_IMAGE_PROVIDER": "mock"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["provider"] == "mock"
    assert payload["capability"] == "image_generation"
    assert payload["intent"] == "image_generation"
    assert payload["tool_calls"][0]["tool_name"] == "image_generation"
    assert payload["image_result"]["output_ref"] == "local://generated/poster.png"
    assert payload["image_result"]["contract"]["capability"] == "image_generation"
    assert payload["generated_dir"] == ".local/generated"
    assert str(Path.cwd()) not in result.stdout
