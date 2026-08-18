"""Shopping tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.shopping.backend import (
    create_shopping_compare_adapter,
    create_shopping_search_adapter,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool


class ShoppingToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if context.mock_mode or not shopping_provider_ready(context.config):
            return []
        search_adapter = create_shopping_search_adapter(context.config)
        return [
            ShoppingSearchTool(
                search_adapter=search_adapter,
                compare_adapter=create_shopping_compare_adapter(context.config),
            ),
        ]


def shopping_provider_ready(config: ProviderConfig) -> bool:
    if (
        config.shopping_search_provider
        == config.shopping_compare_provider
        == "haodanku"
    ):
        return bool(config.haodanku_api_key)
    if config.shopping_search_provider == config.shopping_compare_provider == "http":
        return bool(
            config.shopping_search_base_url
            and config.shopping_search_api_key
            and config.shopping_compare_base_url
            and config.shopping_compare_api_key
        )
    return False
