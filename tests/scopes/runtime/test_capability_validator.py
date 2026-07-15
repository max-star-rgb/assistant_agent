from assistant_agent.agent.capability_validator import CapabilityValidator
from assistant_agent.schemas.intent_decision import IntentDecision
from assistant_agent.schemas.requests import UserRequest


def _request(
    text: str | None = "你好",
    image_ids: list[str] | None = None,
    video_ids: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> UserRequest:
    return UserRequest(
        user_id="u1",
        session_id="s1",
        text=text,
        image_ids=image_ids or [],
        video_ids=video_ids or [],
        metadata=metadata or {},
    )


def test_validator_accepts_valid_direct_chat() -> None:
    decision = IntentDecision(primary_intent="direct_chat", confidence=0.8)

    validated = CapabilityValidator().validate(decision, _request(text="解释一下"))

    assert validated.primary_intent == "direct_chat"
    assert validated.capabilities == ["direct_chat"]
    assert validated.plan_steps[0].capability == "direct_chat"
    assert validated.missing_inputs == []


def test_image_understanding_without_image_becomes_followup() -> None:
    decision = IntentDecision(primary_intent="image_understanding", confidence=0.7)

    validated = CapabilityValidator().validate(decision, _request(text="看看图里有什么"))

    assert validated.primary_intent == "ask_followup"
    assert validated.capabilities == ["ask_followup"]
    assert validated.missing_inputs == ["image"]


def test_video_understanding_without_video_becomes_followup() -> None:
    decision = IntentDecision(primary_intent="video_understanding", confidence=0.7)

    validated = CapabilityValidator().validate(decision, _request(text="总结这个视频"))

    assert validated.primary_intent == "ask_followup"
    assert validated.missing_inputs == ["video"]


def test_render_without_scene_becomes_followup() -> None:
    decision = IntentDecision(primary_intent="render_3d", confidence=0.7)

    validated = CapabilityValidator().validate(decision, _request(text="渲染一下"))

    assert validated.primary_intent == "ask_followup"
    assert validated.missing_inputs == ["scene_description"]


def test_price_compare_without_products_but_with_query_adds_search_then_compare() -> None:
    decision = IntentDecision(primary_intent="price_compare", confidence=0.8)

    validated = CapabilityValidator().validate(decision, _request(text="比较一下白色运动鞋价格"))

    assert validated.primary_intent == "multi_step_orchestration"
    assert validated.capabilities == ["product_search", "price_compare"]
    assert [step.capability for step in validated.plan_steps] == ["product_search", "price_compare"]
    assert [step.tool_name for step in validated.plan_steps] == ["product_search", "price_compare"]


def test_memory_retrieval_without_session_or_user_context_becomes_followup() -> None:
    request = UserRequest.model_construct(
        user_id="",
        session_id="",
        text="上次那个包",
        image_ids=[],
        video_ids=[],
        audio_id=None,
        metadata={},
    )
    decision = IntentDecision(primary_intent="memory_retrieval", confidence=0.8)

    validated = CapabilityValidator().validate(decision, request)

    assert validated.primary_intent == "ask_followup"
    assert validated.missing_inputs == ["user_id", "session_id"]


def test_validator_does_not_call_tools_or_providers() -> None:
    decision = IntentDecision(primary_intent="image_generation", confidence=0.8)

    validated = CapabilityValidator().validate(decision, _request(text="生成一张海报"))

    assert validated.primary_intent == "image_generation"
    assert validated.plan_steps[0].tool_name == "image_generation"
    assert not hasattr(validated, "tool_results")
