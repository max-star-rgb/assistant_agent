"""Assistant capability taxonomy and contracts."""

from typing import Literal

from pydantic import BaseModel, Field


CapabilityName = Literal[
    "direct_chat",
    "image_generation",
    "image_understanding",
    "video_understanding",
    "product_search",
    "price_compare",
    "render_3d",
    "memory_retrieval",
    "multi_step_orchestration",
    "ask_followup",
    "memory_save",
]


CANONICAL_INTENTS: tuple[CapabilityName, ...] = (
    "direct_chat",
    "image_generation",
    "image_understanding",
    "video_understanding",
    "product_search",
    "price_compare",
    "render_3d",
    "memory_retrieval",
    "multi_step_orchestration",
    "ask_followup",
)


LEGACY_INTENT_ALIASES: dict[str, CapabilityName] = {
    "chat": "direct_chat",
    "generate_image": "image_generation",
    "understand_image": "image_understanding",
    "understand_video": "video_understanding",
    "search_product": "product_search",
    "compare_price": "price_compare",
    "render_3d": "render_3d",
    "retrieve_memory": "memory_retrieval",
    "multi_tool_task": "multi_step_orchestration",
    "ask_followup": "ask_followup",
    "save_memory": "memory_save",
}


class CapabilityContract(BaseModel):
    """Stable contract for one assistant capability."""

    name: CapabilityName
    input_requirements: list[str] = Field(default_factory=list)
    output_contract: str = Field(min_length=1)
    tool_name: str | None = None
    text_required: bool = False
    image_required: bool = False
    video_required: bool = False
    media_optional: bool = False


CAPABILITY_CONTRACTS: dict[CapabilityName, CapabilityContract] = {
    "direct_chat": CapabilityContract(
        name="direct_chat",
        input_requirements=["text"],
        output_contract="AgentResponse.message",
        tool_name=None,
        text_required=True,
        media_optional=True,
    ),
    "image_generation": CapabilityContract(
        name="image_generation",
        input_requirements=["text"],
        output_contract="ImageGenerationResult",
        tool_name="image_generation",
        text_required=True,
        media_optional=True,
    ),
    "image_understanding": CapabilityContract(
        name="image_understanding",
        input_requirements=["image"],
        output_contract="VisualUnderstandingResult",
        tool_name="vision_understanding",
        image_required=True,
        media_optional=False,
    ),
    "video_understanding": CapabilityContract(
        name="video_understanding",
        input_requirements=["video"],
        output_contract="VisualUnderstandingResult",
        tool_name="vision_understanding",
        video_required=True,
        media_optional=False,
    ),
    "product_search": CapabilityContract(
        name="product_search",
        input_requirements=["text or visual_summary"],
        output_contract="list[ProductResult]",
        tool_name="product_search",
        text_required=False,
        media_optional=True,
    ),
    "price_compare": CapabilityContract(
        name="price_compare",
        input_requirements=["product candidates or search query"],
        output_contract="PriceCompareResult",
        tool_name="price_compare",
        text_required=False,
        media_optional=True,
    ),
    "render_3d": CapabilityContract(
        name="render_3d",
        input_requirements=["scene description"],
        output_contract="RenderResult",
        tool_name="render_3d",
        text_required=True,
        media_optional=True,
    ),
    "memory_retrieval": CapabilityContract(
        name="memory_retrieval",
        input_requirements=["text", "user_id", "session_id"],
        output_contract="list[MemoryItem]",
        tool_name="memory_retrieval",
        text_required=True,
        media_optional=False,
    ),
    "multi_step_orchestration": CapabilityContract(
        name="multi_step_orchestration",
        input_requirements=["text", "optional media"],
        output_contract="TaskPlan + AgentResponse",
        tool_name=None,
        text_required=True,
        media_optional=True,
    ),
    "ask_followup": CapabilityContract(
        name="ask_followup",
        input_requirements=["missing or ambiguous request"],
        output_contract="TaskPlan.followup_question",
        tool_name=None,
        media_optional=True,
    ),
    "memory_save": CapabilityContract(
        name="memory_save",
        input_requirements=["text or content", "user_id", "session_id"],
        output_contract="MemoryItem",
        tool_name="memory_save",
        media_optional=False,
    ),
}


def canonical_intent(intent: str) -> CapabilityName:
    """Return the canonical assistant capability name for an intent or alias."""

    if intent in CAPABILITY_CONTRACTS:
        return intent  # type: ignore[return-value]
    try:
        return LEGACY_INTENT_ALIASES[intent]
    except KeyError as exc:
        raise ValueError(f"Unknown intent: {intent}") from exc


def contract_for_intent(intent: str) -> CapabilityContract:
    """Return capability contract for a canonical intent or legacy alias."""

    return CAPABILITY_CONTRACTS[canonical_intent(intent)]
