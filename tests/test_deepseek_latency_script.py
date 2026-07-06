import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult


SCRIPT_PATH = Path("scripts/measure_deepseek_latency.py")


def _load_module():
    module_name = "measure_deepseek_latency_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deepseek_latency_script_import_is_safe(monkeypatch) -> None:
    import urllib.request

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("latency script import must not call provider")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    module = _load_module()

    assert hasattr(module, "main")


def test_deepseek_latency_missing_profile_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--runs", "1", "--mode", "provider"],
        env={
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-key",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "provider_unconfigured" in result.stdout
    assert "MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke" in result.stdout
    assert "Traceback" not in result.stderr


def test_deepseek_latency_provider_mode_outputs_ttft(monkeypatch, capsys) -> None:
    module = _load_module()

    class FakeChatAdapter:
        provider = "deepseek"
        model = "deepseek-chat"

        def chat(self, request: ChatRequest) -> ChatResult:
            assert request.stream_callback is not None
            request.stream_callback(
                "首",
                {
                    "provider": self.provider,
                    "model": self.model,
                    "token_streaming": True,
                    "chunking_strategy": "provider_token_delta",
                },
            )
            return ChatResult(
                response_text="首个回答",
                provider=self.provider,
                model=self.model,
                latency_ms=12,
                output_ref="provider://chat/deepseek",
            )

    monkeypatch.setattr(module, "create_chat_adapter", lambda config: FakeChatAdapter())

    result = module.main(
        ["--runs", "1", "--mode", "provider", "--text", "你好"],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-key",
            "DEEPSEEK_CHAT_STREAM": "true",
        },
    )

    payload = json.loads(capsys.readouterr().out)
    sample = payload["samples"][0]["provider"]
    assert result == 0
    assert payload["status"] == "success"
    assert sample["status"] == "success"
    assert sample["ttft_ms"] is not None
    assert sample["stream_open_ms"] == 12
    assert sample["first_delta_preview"] == "首"
    assert payload["summary"]["provider.ttft_ms"]["count"] == 1


def test_deepseek_latency_runtime_mode_outputs_end_to_end_latency(monkeypatch, capsys) -> None:
    module = _load_module()

    class FakeRuntime:
        def __init__(self, config, chat_adapter):
            self.config = config
            self.chat_adapter = chat_adapter

        def run_state(self, request: UserRequest, event_sink=None):
            event_sink.emit(AgentEvent(type="task_started", session_id=request.session_id, run_id="run_1"))
            event_sink.emit(
                AgentEvent(
                    type="response_delta",
                    session_id=request.session_id,
                    run_id="run_1",
                    text="端到端",
                    payload={"token_streaming": True},
                )
            )

            class State:
                status = "completed"
                run_id = "run_1"
                trace_id = "trace_1"
                response = AgentResponse(message="端到端回答")
                errors = []

            return State()

    class FakeChatAdapter:
        provider = "deepseek"
        model = "deepseek-chat"

        def chat(self, request: ChatRequest) -> ChatResult:
            return ChatResult(response_text="ok", provider=self.provider, model=self.model, latency_ms=3)

    monkeypatch.setattr(module, "AgentGraphRuntime", FakeRuntime)
    monkeypatch.setattr(module, "create_chat_adapter", lambda config: FakeChatAdapter())

    result = module.main(
        ["--runs", "1", "--mode", "runtime", "--text", "你好"],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-key",
        },
    )

    payload = json.loads(capsys.readouterr().out)
    runtime = payload["samples"][0]["runtime"]
    assert result == 0
    assert runtime["status"] == "success"
    assert runtime["first_response_delta_ms"] is not None
    assert runtime["total_ms"] is not None
    assert runtime["first_response_delta_preview"] == "端到端"
    assert payload["summary"]["runtime.first_response_delta_ms"]["count"] == 1
