from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.web_search_adapter import MockWebSearchAdapter
from assistant_agent.services.image_generation_adapter import MockImageGenerationAdapter
from assistant_agent.services.product_adapter import MockPriceCompareAdapter, MockProductSearchAdapter
from assistant_agent.services.video_adapter import MockVideoUnderstandingAdapter
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.image_generation_tool import ImageGenerationTool
from assistant_agent.tools.memory_tool import MemoryTool
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool
from assistant_agent.tools.video_tool import VideoUnderstandingTool
from assistant_agent.tools.web_search_tool import WebSearchTool


def test_tool_observation_prefers_model_observation_over_full_data() -> None:
    result = ToolResult(
        tool_name="example_tool",
        success=True,
        data={
            "summary": "full runtime payload",
            "provider": "mock",
            "latency_ms": 10,
            "contract": {"capability": "example_tool"},
        },
        model_observation={
            "summary": "answer-facing payload",
            "fact": "visible to model",
        },
    )

    observation = observation_from_tool_result(result)

    assert observation.summary == "answer-facing payload"
    assert observation.structured_output == {
        "summary": "answer-facing payload",
        "fact": "visible to model",
    }
    assert "provider" not in observation.structured_output
    assert "contract" not in observation.structured_output


def test_web_search_model_observation_keeps_sources_without_execution_metadata() -> (
    None
):
    result = WebSearchTool(adapter=MockWebSearchAdapter()).run(
        {"query": "OpenAI latest news", "limit": 1}
    )

    observation = observation_from_tool_result(result)

    assert result.model_observation is not None
    assert observation.structured_output["results"][0]["url"].startswith(
        "mock://web-search/"
    )
    assert observation.structured_output["results"][0]["published_at"]
    assert "provider" in result.data
    assert "provider" not in observation.structured_output
    assert "latency_ms" not in observation.structured_output


def test_shopping_search_model_observation_preserves_llm_usable_items_only() -> None:
    result = ShoppingSearchTool(
        search_adapter=MockProductSearchAdapter(),
        price_compare_adapter=MockPriceCompareAdapter(),
    ).run(
        {"query": "白色低帮运动鞋"}
    )

    observation = observation_from_tool_result(result)
    item = observation.structured_output["search"]["items"][0]

    assert result.model_observation is not None
    assert item["title"]
    assert item["product_url"]
    assert "raw_url" not in item
    assert "source" not in item
    assert "provider" in result.data
    assert "provider" not in observation.structured_output
    assert "contract" not in observation.structured_output


def test_video_understanding_model_observation_keeps_semantics_without_provider_metadata() -> (
    None
):
    result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run(
        {"video_ref": "mock://video/demo", "user_query": "视频里有什么"}
    )

    observation = observation_from_tool_result(result)

    assert result.model_observation is not None
    assert observation.structured_output["summary"]
    assert observation.structured_output["source"] == "recent_frame_fallback"
    assert "provider" in result.data
    assert "provider" not in observation.structured_output
    assert "model" not in observation.structured_output
    assert "latency_ms" not in observation.structured_output


def test_image_generation_model_observation_exposes_artifact_not_provider_payload() -> (
    None
):
    result = ImageGenerationTool(adapter=MockImageGenerationAdapter()).run(
        {"prompt": "生成一张产品海报"}
    )

    observation = observation_from_tool_result(result)

    assert result.model_observation is not None
    assert observation.structured_output["status"] == "succeeded"
    assert observation.structured_output["image_url"]
    assert "生成一张产品海报" in observation.structured_output["prompt_used"]
    assert "provider" in result.data
    assert "provider" not in observation.structured_output
    assert "request_id" not in observation.structured_output
    assert "contract" not in observation.structured_output


def test_memory_model_observation_hides_runtime_identity_and_internal_ids() -> None:
    result = MemoryTool().run(
        {"action": "retrieve", "user_id": "u1", "query": "上次关注了什么包"},
        ToolContext(user_id="u1", session_id="s1"),
    )

    observation = observation_from_tool_result(result)
    item = observation.structured_output["items"][0]

    assert result.model_observation is not None
    assert item["summary"] == "用户上次关注了一个黑色通勤包。"
    assert "memory_id" in result.data["items"][0]
    assert "user_id" in result.data["items"][0]
    assert "memory_id" not in item
    assert "user_id" not in item
    assert "contract" not in observation.structured_output
