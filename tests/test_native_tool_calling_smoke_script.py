import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path("scripts/smoke_native_tool_calling.py")


def test_native_tool_calling_smoke_import_is_safe(monkeypatch) -> None:
    module_name = "smoke_native_tool_calling_import_test"
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


def test_native_tool_calling_smoke_default_scripted_outputs_json() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--query", "帮我找一款通勤蓝牙耳机"],
        env={},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["provider"] == "scripted-native"
    assert payload["tool_call_mode"] == "auto"
    assert payload["tool_sequence"] == ["product_search"]
    assert payload["provider_decisions"][0]["finish_reason"] == "tool_calls"
    assert payload["provider_decisions"][0]["message_kind"] == "tool_call"
    assert payload["provider_decisions"][1]["finish_reason"] == "stop"
    assert payload["provider_decisions"][1]["message_kind"] == "final_answer"
    assert payload["native_tool_calls"][0]["name"] == "product_search"
    assert payload["expectation_failed"] is False


def test_native_tool_calling_real_provider_missing_config_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--real-provider", "--query", "帮我找通勤耳机"],
        env={"MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke" in result.stdout
    assert "Traceback" not in result.stderr


def test_native_tool_calling_real_provider_missing_key_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--real-provider", "--query", "帮我找通勤耳机"],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "missing DEEPSEEK_CHAT_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr
