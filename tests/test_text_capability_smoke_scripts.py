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


def test_direct_chat_smoke_deepseek_missing_key_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(DIRECT_CHAT_SCRIPT), "--text", "帮我写一段商品介绍"],
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


def test_direct_chat_smoke_invalid_proxy_scheme_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(DIRECT_CHAT_SCRIPT), "--text", "hello"],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING": "1",
            "DEEPSEEK_CHAT_API_KEY": "test-key",
            "ALL_PROXY": "socks://127.0.0.1:17891",
            "all_proxy": "socks://127.0.0.1:17891",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "environment_invalid" in result.stdout
    assert "ALL_PROXY" in result.stdout
    assert "unsupported proxy URL scheme 'socks'" in result.stdout
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


def test_text_image_generation_smoke_qwen_missing_image_key_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(IMAGE_GENERATION_SCRIPT), "--prompt", "生成一张日系极简商品海报"],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "qwen",
            "QWEN_IMAGE_API_KEY": "",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "missing QWEN_IMAGE_API_KEY" in result.stdout
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


def test_direct_chat_smoke_uses_explicit_environment(monkeypatch) -> None:
    module_name = "smoke_direct_chat_dotenv_test"
    spec = importlib.util.spec_from_file_location(module_name, DIRECT_CHAT_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    called = {}

    class FakeRuntime:
        def __init__(self, config):
            called["chat_provider"] = config.chat_provider

        def run_state(self, request, event_sink=None):
            from assistant_agent.agent.state import AgentState
            from assistant_agent.schemas.planning import IntentResult
            from assistant_agent.schemas.requests import AgentResponse

            state = AgentState.from_request(request)
            state.set_intent(IntentResult(intent="direct_chat", confidence=1.0, rationale="test"))
            state.set_response(AgentResponse(message="ok", data={"contract": {"capability": "direct_chat"}}))
            return state

    monkeypatch.setattr(module, "AgentGraphRuntime", FakeRuntime)

    result = module.main(
        ["--text", "hello"],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "mock",
        },
    )

    assert result == 0
    assert called["chat_provider"] == "mock"


def test_direct_chat_smoke_reports_native_stream_observability(monkeypatch, capsys) -> None:
    module_name = "smoke_direct_chat_native_stream_test"
    spec = importlib.util.spec_from_file_location(module_name, DIRECT_CHAT_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    called = {}

    class FakeRuntime:
        def __init__(self, config):
            called["native_provider_streaming"] = config.native_provider_streaming

        def run_state(self, request, event_sink=None):
            from assistant_agent.agent.state import AgentState
            from assistant_agent.schemas.events import AgentEvent
            from assistant_agent.schemas.planning import IntentResult
            from assistant_agent.schemas.requests import AgentResponse

            state = AgentState.from_request(request)
            state.request.metadata["native_runtime"] = True
            state.provider_budget.record_call(
                run_id=state.run_id,
                capability="direct_chat",
                provider="deepseek",
                model="deepseek-chat",
                latency_ms=12,
                status="succeeded",
            )
            if event_sink is not None:
                event_sink.emit(
                    AgentEvent(
                        type="response_delta",
                        session_id=state.session_id,
                        run_id=state.run_id,
                        text="流式",
                    )
                )
            state.set_intent(IntentResult(intent="direct_chat", confidence=1.0, rationale="test"))
            state.set_response(AgentResponse(message="流式回答", data={"native_runtime": True}))
            return state

    monkeypatch.setattr(module, "AgentGraphRuntime", FakeRuntime)

    result = module.main(
        ["--text", "hello"],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "mock",
            "MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING": "1",
        },
    )

    assert result == 0
    assert called["native_provider_streaming"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime_profile"] == "provider_smoke"
    assert payload["native_provider_streaming"] is True
    assert payload["native_runtime"] is True
    assert payload["event_counts"]["response_delta"] == 1
    assert payload["response_delta_text"] == "流式"
    assert payload["provider_budget"]["provider_call_count"] == 1
    assert payload["provider_budget"]["calls_by_capability"] == {"direct_chat": 1}


def test_text_image_generation_smoke_default_mock_outputs_json() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(IMAGE_GENERATION_SCRIPT),
            "--no-env-file",
            "--prompt",
            "生成一张日系极简商品海报",
        ],
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
