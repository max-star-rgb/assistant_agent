"""Assistant capability taxonomy and contracts."""

from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.tool_ids import (
    IMAGE_GENERATION_TOOL_NAME,
    IMAGE_UNDERSTANDING_TOOL_NAME,
    MEMORY_RETRIEVAL_TOOL_NAME,
    MEMORY_SAVE_TOOL_NAME,
    SHOPPING_SEARCH_TOOL_NAME,
    VISUAL_IMAGE_SEARCH_TOOL_NAME,
    WEB_FETCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)


CapabilityName = Literal[
    "direct_chat",
    "image_generation",
    "image_understanding",
    "video_understanding",
    "web_search",
    "web_fetch",
    "visual_image_search",
    "shopping_search",
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
    "web_search",
    "web_fetch",
    "visual_image_search",
    "shopping_search",
    "memory_retrieval",
    "memory_save",
    "multi_step_orchestration",
    "ask_followup",
)


_NON_TOOL_INTENT_ALIASES: dict[str, CapabilityName] = {
    "chat": "direct_chat",
    "multi_tool_task": "multi_step_orchestration",
    "ask_followup": "ask_followup",
}
LEGACY_INTENT_ALIASES: dict[str, CapabilityName] = {
    **_NON_TOOL_INTENT_ALIASES,
    "understand_image": "image_understanding",
    "understand_video": "video_understanding",
    "generate_image": "image_generation",
    "search_web": "web_search",
    "fetch_web": "web_fetch",
    "read_url": "web_fetch",
    "search_image_by_image": "visual_image_search",
    "retrieve_memory": "memory_retrieval",
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
        tool_name=IMAGE_GENERATION_TOOL_NAME,
        text_required=True,
        media_optional=True,
    ),
    "image_understanding": CapabilityContract(
        name="image_understanding",
        input_requirements=["image"],
        output_contract="VisualUnderstandingResult",
        tool_name=IMAGE_UNDERSTANDING_TOOL_NAME,
        image_required=True,
        media_optional=False,
    ),
    "video_understanding": CapabilityContract(
        name="video_understanding",
        input_requirements=["video"],
        output_contract="VideoUnderstandingResult",
        tool_name=IMAGE_UNDERSTANDING_TOOL_NAME,
        video_required=True,
        media_optional=False,
    ),
    "web_search": CapabilityContract(
        name="web_search",
        input_requirements=["query"],
        output_contract="WebSearchResult",
        tool_name=WEB_SEARCH_TOOL_NAME,
        text_required=True,
        media_optional=False,
    ),
    "web_fetch": CapabilityContract(
        name="web_fetch",
        input_requirements=["url"],
        output_contract="WebFetchResult",
        tool_name=WEB_FETCH_TOOL_NAME,
        text_required=True,
        media_optional=False,
    ),
    "visual_image_search": CapabilityContract(
        name="visual_image_search",
        input_requirements=["public image_url or http(s) image_ids"],
        output_contract="VisualImageSearchResult",
        tool_name=VISUAL_IMAGE_SEARCH_TOOL_NAME,
        text_required=False,
        image_required=True,
        media_optional=False,
    ),
    "shopping_search": CapabilityContract(
        name="shopping_search",
        input_requirements=["text or visual_summary"],
        output_contract="ShoppingSearchResult",
        tool_name=SHOPPING_SEARCH_TOOL_NAME,
        text_required=False,
        media_optional=True,
    ),
    "memory_retrieval": CapabilityContract(
        name="memory_retrieval",
        input_requirements=["text", "user_id", "session_id"],
        output_contract="list[MemoryItem]",
        tool_name=MEMORY_RETRIEVAL_TOOL_NAME,
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
        input_requirements=["text or content", "user_id", "session_id", "source_intent for assistant-loop calls"],
        output_contract="saved MemoryItem or candidate/confirmation/rejection status",
        tool_name=MEMORY_SAVE_TOOL_NAME,
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
