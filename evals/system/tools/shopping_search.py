"""PyCharm-runnable fixed-input smoke for shopping_search."""

from _smoke_runner import run_tool_smoke
from _smoke_adapters import EmptyShoppingCompareAdapter, EmptyShoppingSearchAdapter

from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool


FIXED_INPUT = {
    "needs": [{"keyword": "无线鼠标", "quantity": 1}],
    "platforms": ["taobao"],
}


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            ShoppingSearchTool(
                search_adapter=EmptyShoppingSearchAdapter(),
                compare_adapter=EmptyShoppingCompareAdapter(),
            ),
            FIXED_INPUT,
        )
    )
