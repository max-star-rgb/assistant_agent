import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path("scripts/smoke_render_3d.py")


def test_render_smoke_import_is_safe(monkeypatch) -> None:
    module_name = "smoke_render_3d_import_test"
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


def test_render_smoke_default_mock_outputs_json() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--scene",
            "北欧风客厅",
            "--product",
            "浅灰色布艺沙发",
        ],
        env={"MULTIMODAL_AGENT_RENDER_PROVIDER": "mock"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["provider"] == "mock"
    assert payload["capability"] == "render_3d"
    assert payload["intent"] == "render_3d"
    assert payload["tool_calls"][0]["tool_name"] == "render_3d"
    assert payload["render_result"]["provider"] == "mock"
    assert payload["render_result"]["output_ref"] == "mock://render/preview.png"
    assert payload["render_result"]["preview_url"] == "mock://render/preview.png"
    assert payload["errors"] == []
    assert str(Path.cwd()) not in result.stdout


def test_render_smoke_http_missing_config_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--scene", "现代办公室"],
        env={"MULTIMODAL_AGENT_RENDER_PROVIDER": "http"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "RENDER_BASE_URL" in result.stdout
    assert "RENDER_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr
