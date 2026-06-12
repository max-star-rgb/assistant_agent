import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path("scripts/smoke_real_vision.py")
ENV_EXAMPLE_PATH = Path(".env.example")


def test_smoke_script_missing_api_key_exits_cleanly(tmp_path) -> None:
    image = tmp_path / "shoe.jpg"
    image.write_bytes(b"placeholder")

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--image", str(image)],
        env={"MULTIMODAL_AGENT_VISION_PROVIDER": "openai"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "missing OPENAI_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr


def test_smoke_script_import_does_not_call_provider(monkeypatch) -> None:
    module_name = "smoke_real_vision_import_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    import urllib.request

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("smoke script import must not call provider")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "main")


def test_smoke_script_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--image" in result.stdout
    assert "Vision Provider smoke" in result.stdout


def test_env_example_contains_no_real_secret_patterns() -> None:
    content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "OPENAI_API_KEY=" in content
    assert "QWEN_API_KEY=" in content
    assert "SEED_API_KEY=" in content
    assert "MULTIMODAL_AGENT_VISION_PROVIDER=mock" in content
    assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", content)
    assert not re.search(r"Bearer\s+[A-Za-z0-9._-]+", content, flags=re.IGNORECASE)
    assert "AIza" not in content


def test_smoke_output_helper_includes_structured_vision_result() -> None:
    module = _load_smoke_module()
    state = SimpleNamespace(
        tool_results=[
            SimpleNamespace(
                tool_name="vision_understanding",
                success=True,
                data={"summary": "真实视觉结果", "objects": ["鞋子"]},
            )
        ]
    )

    assert module._vision_result_payload(state) == {"summary": "真实视觉结果", "objects": ["鞋子"]}


def _load_smoke_module():
    module_name = "smoke_real_vision_test_helpers"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
