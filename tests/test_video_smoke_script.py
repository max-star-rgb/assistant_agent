import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path("scripts/smoke_video_understanding.py")


def test_video_smoke_import_is_safe(monkeypatch) -> None:
    module_name = "smoke_video_understanding_import_test"
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


def test_video_smoke_default_mock_outputs_json() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--video-ref",
            "mock://video/product-demo",
            "--text",
            "总结这个视频",
        ],
        env={"MULTIMODAL_AGENT_VIDEO_PROVIDER": "mock"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["provider"] == "mock"
    assert payload["capability"] == "video_understanding"
    assert payload["intent"] == "video_understanding"
    assert payload["tool_calls"][0]["tool_name"] == "video_understanding"
    assert payload["video_result"]["provider"] == "mock"
    assert payload["video_result"]["output_ref"] == "mock://video/understanding/product-demo"
    assert payload["video_result"]["summary"]
    assert payload["errors"] == []
    assert str(Path.cwd()) not in result.stdout


def test_video_smoke_http_missing_config_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--video-ref", "mock://video/product-demo"],
        env={"MULTIMODAL_AGENT_VIDEO_PROVIDER": "http"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "VIDEO_UNDERSTANDING_BASE_URL" in result.stdout
    assert "VIDEO_UNDERSTANDING_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr
    assert "Bearer" not in result.stdout
    assert "Authorization" not in result.stdout
    assert "base64" not in result.stdout


def test_video_smoke_ark_missing_config_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--video-ref", "video1"],
        env={"MULTIMODAL_AGENT_VIDEO_PROVIDER": "ark"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "ARK_VISION_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr
    assert "Bearer" not in result.stdout
