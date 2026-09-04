"""PyCharm-runnable fixed-input smoke for shopping_search."""

from _smoke_runner import run_tool_smoke
from _smoke_adapters import EmptyShoppingCompareAdapter, EmptyShoppingSearchAdapter

from assistant_agent.tools.plugins.builtin.shopping.tool import (
    create_shopping_search_tool,
)


FIXED_INPUT = {
    "queries": ["无线鼠标"],
}


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            create_shopping_search_tool(
                search_adapter=EmptyShoppingSearchAdapter(),
                compare_adapter=EmptyShoppingCompareAdapter(),
            ),
            FIXED_INPUT,
        )
    )
