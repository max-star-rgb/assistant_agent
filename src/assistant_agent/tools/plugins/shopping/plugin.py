"""Shopping tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.services.product_adapter import (
    create_shopping_compare_adapter,
    create_shopping_search_adapter,
)
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import ToolPluginContext, ToolPluginDescriptor
from assistant_agent.tools.plugins.shopping.tool import (
    ShoppingDetailPresentTool,
    ShoppingSearchTool,
)


class ShoppingToolPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="shopping", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if not context.mock_mode and not shopping_provider_ready(context.config):
            return []
        return [
            ShoppingSearchTool(
                search_adapter=create_shopping_search_adapter(context.config),
                compare_adapter=create_shopping_compare_adapter(context.config),
            ),
            ShoppingDetailPresentTool(),
        ]


def shopping_provider_ready(config: ProviderConfig) -> bool:
    if config.shopping_search_provider == config.shopping_compare_provider == "haodanku":
        return bool(config.haodanku_api_key)
    if config.shopping_search_provider == config.shopping_compare_provider == "http":
        return bool(
            config.shopping_search_base_url
            and config.shopping_search_api_key
            and config.shopping_compare_base_url
            and config.shopping_compare_api_key
        )
    return False
