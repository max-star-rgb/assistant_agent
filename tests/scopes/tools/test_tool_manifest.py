from assistant_agent.services.tool_manifest import (
    REMOVED_SHOPPING_TOOL_NAMES,
    SHOPPING_SEARCH_CAPABILITY,
    SHOPPING_SEARCH_TOOL_NAME,
    canonical_action_for_legacy_alias,
    canonical_capability_for_tool,
    canonical_tool_for_capability,
    manifest_for_tool_name,
    public_tool_names,
    removed_tool_names,
    replacement_for_removed_tool,
)


def test_shopping_tool_manifest_resolves_public_name_and_removed_aliases() -> None:
    manifest = manifest_for_tool_name(SHOPPING_SEARCH_TOOL_NAME)

    assert manifest is not None
    assert manifest.public_name == SHOPPING_SEARCH_TOOL_NAME
    assert manifest.capability == SHOPPING_SEARCH_CAPABILITY
    assert manifest.exposure_class == "read"
    assert set(manifest.removed_tool_aliases) == set(REMOVED_SHOPPING_TOOL_NAMES)
    assert canonical_tool_for_capability(SHOPPING_SEARCH_CAPABILITY) == SHOPPING_SEARCH_TOOL_NAME
    assert canonical_capability_for_tool(SHOPPING_SEARCH_TOOL_NAME) == SHOPPING_SEARCH_CAPABILITY
    assert public_tool_names() == (SHOPPING_SEARCH_TOOL_NAME,)
    assert set(removed_tool_names()) == set(REMOVED_SHOPPING_TOOL_NAMES)
    assert replacement_for_removed_tool("product_search") == SHOPPING_SEARCH_TOOL_NAME
    assert replacement_for_removed_tool("price_compare") == SHOPPING_SEARCH_TOOL_NAME
    assert canonical_action_for_legacy_alias("search_product") == SHOPPING_SEARCH_CAPABILITY
    assert canonical_action_for_legacy_alias("compare_price") == SHOPPING_SEARCH_CAPABILITY
