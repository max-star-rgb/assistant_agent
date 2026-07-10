import importlib.util
import json
import subprocess
import sys
from argparse import Namespace
from collections.abc import AsyncIterator
from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.llm_events import LLMEvent, LLMToolCallDelta
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
    assert sample["message_kind"] == "final_answer"
    assert sample["finish_reason"] is None
    assert sample["tool_call_count"] == 0
    assert sample["tool_names"] == []
    assert payload["summary"]["provider.ttft_ms"]["count"] == 1


def test_deepseek_latency_provider_tool_call_only_sample_uses_tool_schema(monkeypatch) -> None:
    module = _load_module()

    class FakeChatAdapter:
        provider = "deepseek"
        model = "deepseek-chat"

        def chat(self, request: ChatRequest) -> ChatResult:
            assert request.tools
            assert request.tool_choice == "auto"
            return ChatResult(
                response_text="",
                tool_calls=[
                    NativeToolCall(
                        id="call_1",
                        name="product_search",
                        arguments={"query": "通勤蓝牙耳机", "limit": 2},
                        raw={
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "product_search", "arguments": "{}"},
                        },
                    )
                ],
                provider=self.provider,
                model=self.model,
                latency_ms=12,
                finish_reason="tool_calls",
                message_kind="tool_call",
                output_ref="provider://chat/deepseek",
            )

    sample = module._measure_provider_sample(
        FakeChatAdapter(),
        Namespace(
            user_id="latency_user",
            session_id="latency_session",
            text="请使用商品搜索工具帮我找一款通勤蓝牙耳机",
            max_tokens=128,
            temperature=0.2,
            expect_first_call_kind="tool_call",
            expect_tool=["product_search"],
        ),
        1,
    )

    assert sample["status"] == "success"
    assert sample["message_kind"] == "tool_call"
    assert sample["tool_call_count"] == 1
    assert sample["tool_names"] == ["product_search"]
    assert sample["response_chars"] == 0
    assert sample["ttft_ms"] is None


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


def test_deepseek_latency_runtime_sample_uses_single_native_chat_call(monkeypatch) -> None:
    module = _load_module()

    class FakeChatAdapter:
        provider = "deepseek"
        model = "deepseek-chat"

        def chat(self, request: ChatRequest) -> ChatResult:
            assert request.tools
            assert request.tool_choice == "auto"
            if request.stream_callback is not None:
                request.stream_callback(
                    "杭州",
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "token_streaming": True,
                    },
                )
            return ChatResult(
                response_text="杭州适合喝龙井茶。",
                provider=self.provider,
                model=self.model,
                latency_ms=5,
                finish_reason="stop",
                message_kind="final_answer",
            )

    monkeypatch.setattr(module, "create_chat_adapter", lambda config: FakeChatAdapter())
    sample = module._measure_runtime_sample(
        ProviderConfig(
            runtime_profile=ProviderConfig.from_env({"MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke"}).runtime_profile,
            chat_provider="deepseek",
            chat_api_key="test-key",
            chat_base_url="https://api.deepseek.com/v1",
            chat_model="deepseek-chat",
            chat_adapter_kind="openai_compatible",
        ),
        Namespace(
            user_id="latency_user",
            session_id="latency_session",
            text="你好",
        ),
        1,
    )

    assert sample["status"] == "success"
    assert len(sample["chat_calls"]) == 1
    chat_call = sample["chat_calls"][0]
    assert chat_call["first_delta_preview"] == "杭州"
    assert chat_call["message_kind"] == "final_answer"
    assert chat_call["finish_reason"] == "stop"
    assert chat_call["tool_call_count"] == 0
    assert chat_call["tool_names"] == []
    assert sample["progress_messages"] == []
    assert sample["first_response_delta_preview"] == "杭州"


def test_deepseek_latency_runtime_sample_records_native_tool_call_sequence(monkeypatch) -> None:
    module = _load_module()

    class FakeChatAdapter:
        provider = "deepseek"
        model = "deepseek-chat"

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, request: ChatRequest) -> ChatResult:
            assert request.tools
            assert request.tool_choice == "auto"
            self.calls += 1
            if self.calls == 1:
                if request.stream_callback is not None:
                    request.stream_callback(
                        "好的",
                        {
                            "provider": self.provider,
                            "model": self.model,
                            "token_streaming": True,
                        },
                    )
                return ChatResult(
                    response_text="好的",
                    tool_calls=[
                        NativeToolCall(
                            id="call_1",
                            name="product_search",
                            arguments={"query": "通勤蓝牙耳机", "limit": 2},
                            raw={
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "product_search", "arguments": "{}"},
                            },
                        )
                    ],
                    provider=self.provider,
                    model=self.model,
                    latency_ms=7,
                    finish_reason="tool_calls",
                    message_kind="tool_call",
                )
            if request.stream_callback is not None:
                request.stream_callback(
                    "已找到",
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "token_streaming": True,
                    },
                )
            return ChatResult(
                response_text="已找到 2 个通勤蓝牙耳机候选。",
                provider=self.provider,
                model=self.model,
                latency_ms=9,
                finish_reason="stop",
                message_kind="final_answer",
            )

    monkeypatch.setattr(module, "create_chat_adapter", lambda config: FakeChatAdapter())
    sample = module._measure_runtime_sample(
        ProviderConfig(
            runtime_profile=ProviderConfig.from_env({"MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke"}).runtime_profile,
            chat_provider="deepseek",
            chat_api_key="test-key",
            chat_base_url="https://api.deepseek.com/v1",
            chat_model="deepseek-chat",
            chat_adapter_kind="openai_compatible",
        ),
        Namespace(
            user_id="latency_user",
            session_id="latency_session",
            text="帮我找通勤蓝牙耳机",
            expect_chat_calls=2,
            expect_first_call_kind="tool_call",
            expect_tool=["product_search"],
        ),
        1,
    )

    assert sample["status"] == "success"
    assert sample["assertion_failures"] == []
    assert len(sample["chat_calls"]) == 2
    assert sample["chat_calls"][0]["message_kind"] == "tool_call"
    assert sample["chat_calls"][0]["finish_reason"] == "tool_calls"
    assert sample["chat_calls"][0]["tool_call_count"] == 1
    assert sample["chat_calls"][0]["tool_names"] == ["product_search"]
    assert sample["chat_calls"][0]["first_delta_preview"] == "好的"
    assert sample["chat_calls"][1]["message_kind"] == "final_answer"
    assert sample["chat_calls"][1]["tool_call_count"] == 0
    assert sample["event_counts"]["progress_message"] == 1
    assert len(sample["progress_messages"]) == 1
    assert sample["progress_messages"][0]["elapsed_ms"] is not None
    assert sample["progress_messages"][0]["text"] == "我查一下。"
    assert sample["progress_messages"][0]["tool_name"] == "product_search"
    assert sample["progress_messages"][0]["replaceable"] is True
    assert sample["first_response_delta_preview"] == "已找到"


def test_deepseek_latency_runtime_sample_records_async_stream_tool_call_without_argument_leak(
    monkeypatch,
) -> None:
    module = _load_module()

    secret_query = "通勤蓝牙耳机-secret-args"

    class FakeStreamingChatAdapter:
        provider = "deepseek"
        model = "deepseek-chat"

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, request: ChatRequest) -> ChatResult:
            raise AssertionError("runtime should use stream_chat when native provider streaming is enabled")

        def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
            assert request.tools
            assert request.tool_choice == "auto"
            self.calls += 1
            if self.calls == 1:
                events = [
                    LLMEvent(
                        event_type="token_delta",
                        provider=self.provider,
                        model=self.model,
                        text="好的",
                    ),
                    LLMEvent(
                        event_type="tool_call_delta",
                        provider=self.provider,
                        model=self.model,
                        tool_call_delta=LLMToolCallDelta(
                            index=0,
                            id="call_1",
                            type="function",
                            name_delta="product_",
                            arguments_delta=f'{{"query": "{secret_query}", ',
                        ),
                    ),
                    LLMEvent(
                        event_type="tool_call_delta",
                        provider=self.provider,
                        model=self.model,
                        tool_call_delta=LLMToolCallDelta(
                            index=0,
                            name_delta="search",
                            arguments_delta='"limit": 2}',
                        ),
                    ),
                    LLMEvent(
                        event_type="completed",
                        provider=self.provider,
                        model=self.model,
                        finish_reason="tool_calls",
                    ),
                ]
            else:
                events = [
                    LLMEvent(
                        event_type="token_delta",
                        provider=self.provider,
                        model=self.model,
                        text="已找到",
                    ),
                    LLMEvent(
                        event_type="completed",
                        provider=self.provider,
                        model=self.model,
                        finish_reason="stop",
                    ),
                ]

            async def stream() -> AsyncIterator[LLMEvent]:
                for event in events:
                    yield event

            return stream()

    monkeypatch.setattr(module, "create_chat_adapter", lambda config: FakeStreamingChatAdapter())
    sample = module._measure_runtime_sample(
        ProviderConfig(
            runtime_profile=ProviderConfig.from_env({"MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke"}).runtime_profile,
            chat_provider="deepseek",
            chat_api_key="test-key",
            chat_base_url="https://api.deepseek.com/v1",
            chat_model="deepseek-chat",
            chat_adapter_kind="openai_compatible",
            native_provider_streaming=True,
        ),
        Namespace(
            user_id="latency_user",
            session_id="latency_session",
            text="帮我找通勤蓝牙耳机",
            expect_chat_calls=2,
            expect_first_call_kind="tool_call",
            expect_tool=["product_search"],
        ),
        1,
    )

    assert sample["status"] == "success"
    assert sample["native_provider_streaming"] is True
    assert sample["assertion_failures"] == []
    assert len(sample["chat_calls"]) == 2
    assert sample["chat_calls"][0]["stream_path"] == "async_stream"
    assert sample["chat_calls"][0]["message_kind"] == "tool_call"
    assert sample["chat_calls"][0]["finish_reason"] == "tool_calls"
    assert sample["chat_calls"][0]["tool_call_count"] == 1
    assert sample["chat_calls"][0]["tool_names"] == ["product_search"]
    assert sample["chat_calls"][0]["event_counts"] == {
        "completed": 1,
        "token_delta": 1,
        "tool_call_delta": 2,
    }
    assert sample["chat_calls"][0]["first_delta_preview"] == "好的"
    assert sample["chat_calls"][1]["stream_path"] == "async_stream"
    assert sample["chat_calls"][1]["message_kind"] == "final_answer"
    assert sample["first_response_delta_preview"] == "已找到"
    assert secret_query not in json.dumps(sample, ensure_ascii=False)


def test_deepseek_latency_runtime_assertion_failure_marks_sample_failed(monkeypatch, capsys) -> None:
    module = _load_module()

    class FakeChatAdapter:
        provider = "deepseek"
        model = "deepseek-chat"

        def chat(self, request: ChatRequest) -> ChatResult:
            if request.stream_callback is not None:
                request.stream_callback("杭州", {"token_streaming": True})
            return ChatResult(
                response_text="杭州适合喝龙井茶。",
                provider=self.provider,
                model=self.model,
                latency_ms=5,
                finish_reason="stop",
                message_kind="final_answer",
            )

    monkeypatch.setattr(module, "create_chat_adapter", lambda config: FakeChatAdapter())

    result = module.main(
        [
            "--runs",
            "1",
            "--mode",
            "runtime",
            "--text",
            "你好",
            "--expect-chat-calls",
            "2",
            "--expect-first-call-kind",
            "tool_call",
        ],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-key",
            "DEEPSEEK_CHAT_STREAM": "true",
        },
    )

    payload = json.loads(capsys.readouterr().out)
    failures = payload["samples"][0]["runtime"]["assertion_failures"]
    assert result == 1
    assert payload["status"] == "failed"
    assert {failure["code"] for failure in failures} == {
        "chat_call_count_mismatch",
        "first_call_kind_mismatch",
    }


def test_deepseek_latency_invalid_proxy_scheme_exits_cleanly() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--runs", "1", "--mode", "runtime"],
        env={
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING": "1",
            "DEEPSEEK_CHAT_API_KEY": "test-key",
            "DEEPSEEK_CHAT_STREAM": "true",
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
