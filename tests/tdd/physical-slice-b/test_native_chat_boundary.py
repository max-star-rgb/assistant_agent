from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from assistant_agent.config import ChatConfig, load_app_config
from assistant_agent.providers.dashscope_langchain import DashScopeNativeChatModel
from scripts import migrate_mem0_memories_to_chinese as migration


class _TranslationModel:
    def __init__(self) -> None:
        self.messages = []
        self.kwargs = {}

    def invoke(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self.messages = messages
        self.kwargs = kwargs
        return AIMessage(content="用户偏好蓝色。")


def test_memory_translation_uses_the_native_chat_model(monkeypatch) -> None:
    model = _TranslationModel()
    captured = {}

    def create_chat_model(config, *, provider_mode):  # type: ignore[no-untyped-def]
        captured["config"] = config
        captured["provider_mode"] = provider_mode
        return model

    monkeypatch.setattr(migration, "create_chat_model", create_chat_model)
    config = ChatConfig(
        chat_provider="qwen",
        chat_api_key="key",
        chat_base_url="https://example.test/v1",
        chat_model="qwen-test",
        qwen_chat_enable_search=True,
        chat_stream=True,
        native_provider_streaming=True,
    )

    translator = migration.create_memory_translation_model(config)
    translated = migration.translate_memory_to_chinese(translator, "prefers blue")

    projected = captured["config"]
    assert captured["provider_mode"] == "real"
    assert projected.qwen_chat_enable_search is False
    assert projected.chat_stream is False
    assert projected.native_provider_streaming is False
    assert translated == "用户偏好蓝色。"
    assert isinstance(model.messages[0], SystemMessage)
    assert isinstance(model.messages[1], HumanMessage)
    assert model.kwargs == {"temperature": 0.0, "max_tokens": 1024}


def test_chat_config_does_not_duplicate_resolved_adapter_kind() -> None:
    assert not hasattr(load_app_config({}).chat, "chat_adapter_kind")


def test_native_dashscope_model_owns_its_generation_transport() -> None:
    class Transport:
        def post_json(self, **request):  # type: ignore[no-untyped-def]
            self.request = request
            return {
                "request_id": "request-sentinel",
                "output": {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "收到"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    transport = Transport()
    model = DashScopeNativeChatModel(
        api_key="key",
        base_url="https://workspace.example/compatible-mode/v1",
        model_name="qwen-test",
        http_transport=transport,
    )

    result = model.invoke([HumanMessage(content="你好")])

    assert result.text == "收到"
    assert transport.request["url"] == (
        "https://workspace.example/api/v1/services/aigc/text-generation/generation"
    )
