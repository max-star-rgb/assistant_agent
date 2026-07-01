from assistant_agent.schemas.capabilities import (
    CANONICAL_INTENTS,
    CAPABILITY_CONTRACTS,
    contract_for_intent,
)
from assistant_agent.tools.registry import create_default_registry


def test_each_required_capability_has_contract() -> None:
    for intent_name in CANONICAL_INTENTS:
        contract = CAPABILITY_CONTRACTS[intent_name]

        assert contract.name == intent_name
        assert contract.input_requirements
        assert contract.output_contract


def test_text_only_capabilities_do_not_require_media() -> None:
    assert CAPABILITY_CONTRACTS["direct_chat"].text_required is True
    assert CAPABILITY_CONTRACTS["direct_chat"].image_required is False
    assert CAPABILITY_CONTRACTS["direct_chat"].video_required is False

    assert CAPABILITY_CONTRACTS["image_generation"].text_required is True
    assert CAPABILITY_CONTRACTS["image_generation"].image_required is False
    assert CAPABILITY_CONTRACTS["image_generation"].video_required is False


def test_media_understanding_contracts_require_matching_media() -> None:
    assert CAPABILITY_CONTRACTS["image_understanding"].image_required is True
    assert CAPABILITY_CONTRACTS["image_understanding"].video_required is False

    assert CAPABILITY_CONTRACTS["video_understanding"].video_required is True
    assert CAPABILITY_CONTRACTS["video_understanding"].image_required is False


def test_tool_contracts_match_default_registry_tool_names() -> None:
    registry_tools = set(create_default_registry().list())

    for contract in CAPABILITY_CONTRACTS.values():
        if contract.tool_name is not None:
            assert contract.tool_name in registry_tools


def test_contract_lookup_accepts_legacy_aliases() -> None:
    assert contract_for_intent("chat").name == "direct_chat"
    assert contract_for_intent("understand_image").name == "image_understanding"
    assert contract_for_intent("generate_image").name == "image_generation"
    assert contract_for_intent("retrieve_memory").name == "memory_retrieval"
