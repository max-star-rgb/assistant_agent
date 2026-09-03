from dataclasses import fields

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from assistant_agent.config import MediaConfig, ToolConfig, VisionConfig
from assistant_agent.media.video.visual_timeline_compactor import (
    LLMVisualTimelineCompactor,
    create_visual_timeline_context_service,
)
from assistant_agent.media.video.visual_timeline_context import VisualTimelineItem
from assistant_agent.native_agent.tools import NativeToolResources
from assistant_agent.providers.dashscope_langchain import DashScopeNativeChatModel
from assistant_agent.tools.plugins.builtin.media_inspection import plugin as media_plugin
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class _Counter:
    tokenizer_id = "test-tokenizer"

    def count_text(self, value: str) -> int:
        return len(value)


class _DashScopeTransport:
    def __init__(self) -> None:
        self.payload = None

    def post_json(self, **kwargs):
        self.payload = kwargs["payload"]
        return {
            "request_id": "test-request",
            "output": {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"summary":"摘要",'
                                '"relevant_observation_indexes":[0]}'
                            )
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }


def test_dashscope_native_model_maps_standard_compactor_options() -> None:
    model = DashScopeNativeChatModel(
        api_key="test-key",
        base_url="https://example.invalid/api/v1",
        model_name="qwen-test",
    )

    payload = model._build_payload(
        [HumanMessage(content="test")],
        stop=None,
        stream=False,
        temperature=0.0,
        max_tokens=64,
        response_format={"type": "json_object"},
    )

    assert payload["parameters"]["temperature"] == 0.0
    assert payload["parameters"]["max_tokens"] == 64
    assert payload["parameters"]["response_format"] == {"type": "json_object"}


def test_visual_timeline_compactor_uses_native_chat_model() -> None:
    transport = _DashScopeTransport()
    compactor = LLMVisualTimelineCompactor(
        DashScopeNativeChatModel(
            api_key="test-key",
            base_url="https://example.invalid/api/v1",
            model_name="qwen-test",
            enable_search=True,
            http_transport=transport,
        ),
        token_counter=_Counter(),
    )

    result = compactor.compact(
        query="找杯子",
        observations=[VisualTimelineItem(timestamp_ms=1, text="桌上有杯子")],
        source_token_count=10,
        summary_max_tokens=200,
    )

    assert result.summary == "摘要"
    assert result.relevant_observation_indexes == [0]
    assert result.provider_usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert transport.payload is not None
    assert "enable_search" not in transport.payload["parameters"]
    assert "search_options" not in transport.payload["parameters"]


def test_native_composition_builds_visual_timeline_service() -> None:
    assert "visual_context_tokenizer_path" not in VisionConfig.__dataclass_fields__
    service = create_visual_timeline_context_service(
        VisionConfig(visual_context_compactor_mode="llm"),
        FakeListChatModel(responses=["{}"]),
        provider_mode="real",
        token_counter=_Counter(),
    )

    assert service is not None
    assert service.token_counter.tokenizer_id == "test-tokenizer"

    assert (
        create_visual_timeline_context_service(
            VisionConfig(visual_context_compactor_mode="llm"),
            FakeListChatModel(responses=["{}"]),
            provider_mode="mock",
            token_counter=None,
        )
        is None
    )
    with pytest.raises(ValueError, match="MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH"):
        create_visual_timeline_context_service(
            VisionConfig(visual_context_compactor_mode="llm"),
            FakeListChatModel(responses=["{}"]),
            provider_mode="real",
            token_counter=None,
        )


def test_native_tool_resources_pass_timeline_service_to_media_plugin(monkeypatch) -> None:
    resource_names = {field.name for field in fields(NativeToolResources)}
    context_names = {field.name for field in fields(ToolPluginContext)}
    assert "visual_timeline_context_service" in resource_names
    assert "visual_timeline_context_service" in context_names

    sentinel = object()
    captured = {}

    @tool
    def probe() -> str:
        """Probe."""

        return "ok"

    def fake_create(**kwargs):
        captured.update(kwargs)
        return probe

    monkeypatch.setattr(media_plugin, "create_visual_memory_search_tool", fake_create)
    context = ToolPluginContext(
        provider_mode="mock",
        config=ToolConfig(),
        vision_config=VisionConfig(),
        media_config=MediaConfig(),
        visual_semantic_store_pool=object(),
        visual_memory_text_index=object(),
        visual_timeline_context_service=sentinel,
    )

    media_plugin.MediaInspectionPlugin().build_tools(context)

    assert captured["timeline_context_service"] is sentinel
