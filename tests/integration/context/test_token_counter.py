"""Offline coverage for model-tokenizer context accounting."""

import json
import sys
from types import SimpleNamespace

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.context.compactor import LLMCompactor
from assistant_agent.context.token_counter import TokenizerJsonTokenCounter
from assistant_agent.context.token_counter import create_context_token_counter
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore


def test_context_window_default_tracks_the_selected_model() -> None:
    qwen_config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_API_KEY": "test-key",
            "QWEN_CHAT_MODEL": "qwen3.6-flash",
        }
    )

    assert qwen_config.context_input_token_limit == 1_000_000
    assert ProviderConfig.from_env({}).context_input_token_limit == 128_000


def _write_word_level_tokenizer(tmp_path):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    tokenizer = Tokenizer(
        WordLevel(
            {
                "[UNK]": 0,
                "hello": 1,
                "world": 2,
            },
            unk_token="[UNK]",
        )
    )
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer_path = tmp_path / "tokenizer.json"
    tokenizer.save(str(tokenizer_path))
    return tokenizer_path


def test_tokenizer_counter_loads_a_real_tokenizers_json(tmp_path) -> None:
    tokenizer_path = _write_word_level_tokenizer(tmp_path)

    counter = TokenizerJsonTokenCounter(
        tokenizer_path,
        tokenizer_id="word-level-test",
    )

    assert counter.count_text("hello world") == 2


def test_runtime_wires_the_configured_real_tokenizer_and_llm_compactor(
    tmp_path,
) -> None:
    class NoCallChatAdapter:
        provider = "qwen"
        model = "qwen3.6-flash"

        def chat(self, request: ChatRequest) -> ChatResult:
            raise AssertionError("runtime construction must not call the Provider")

    tokenizer_path = _write_word_level_tokenizer(tmp_path)
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            provider_mode="real",
            chat_provider="qwen",
            qwen_api_key="test-key",
            context_compactor_mode="llm",
            context_tokenizer_path=str(tokenizer_path),
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=NoCallChatAdapter(),
        session_store=InMemorySessionStore(),
    )

    assert isinstance(runtime.context_token_counter, TokenizerJsonTokenCounter)
    assert isinstance(runtime.context_compactor, LLMCompactor)


def test_enabled_real_compaction_requires_an_explicit_local_tokenizer_path() -> None:
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="qwen",
        qwen_api_key="test-key",
        context_compactor_mode="llm",
    )

    with pytest.raises(
        ValueError,
        match="MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH",
    ):
        create_context_token_counter(config)


def test_mock_mode_never_loads_a_context_tokenizer() -> None:
    config = ProviderConfig(context_compactor_mode="llm")

    assert create_context_token_counter(config) is None


def test_tokenizer_counter_counts_the_compiled_provider_payload(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeEncoding:
        def __init__(self, value: str) -> None:
            self.ids = list(range(len(value)))

    class FakeTokenizer:
        loaded_path = ""
        encoded_values: list[str] = []

        @classmethod
        def from_file(cls, path: str):
            cls.loaded_path = path
            return cls()

        def encode(self, value: str, *, add_special_tokens: bool):
            assert add_special_tokens is False
            self.encoded_values.append(value)
            return FakeEncoding(value)

    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        SimpleNamespace(Tokenizer=FakeTokenizer),
    )
    tokenizer_path = tmp_path / "tokenizer.json"
    tokenizer_path.write_text("{}", encoding="utf-8")
    counter = TokenizerJsonTokenCounter(
        tokenizer_path,
        tokenizer_id="qwen-test",
    )
    request = ChatRequest(
        user_id="token-user",
        session_id="token-session",
        user_query="当前请求",
        messages=[
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "当前请求"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "probe",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice="auto",
        response_format={"type": "json_object"},
    )

    count = counter.count_chat_request(request)

    payload = json.loads(FakeTokenizer.encoded_values[-1])
    assert FakeTokenizer.loaded_path == str(tokenizer_path)
    assert count == len(FakeTokenizer.encoded_values[-1])
    assert payload["messages"] == request.messages
    assert payload["tools"] == request.tools
    assert payload["tool_choice"] == "auto"
    assert payload["response_format"] == {"type": "json_object"}
