"""Lodging search Tool plugin."""

import os
from pathlib import Path

from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.lodging.tool import (
    create_lodging_search_tool,
)
from assistant_agent.tools.plugins.builtin.lodging.backend import (
    FlyAILodgingSearchAdapter,
)
from assistant_agent.tools.plugins.builtin.lodging.watch_tool import (
    create_hotel_price_watch_create_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class LodgingToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if context.mock_mode:
            tools: list[BaseTool] = [create_lodging_search_tool()]
        elif (
            context.config.lodging.lodging_provider == "flyai"
            and _is_executable_file(context.config.lodging.flyai_cli_path)
            and context.config.lodging.flyai_api_key
        ):
            tools = [
                create_lodging_search_tool(
                    FlyAILodgingSearchAdapter(
                        cli_path=context.config.lodging.flyai_cli_path,
                        api_key=context.config.lodging.flyai_api_key,
                        timeout_seconds=context.config.lodging.flyai_timeout_seconds,
                    )
                )
            ]
        else:
            return []
        if (
            context.config.durable_tasks_enabled
            and context.durable_task_service is not None
        ):
            tools.append(
                create_hotel_price_watch_create_tool(context.durable_task_service)
            )
        return tools


def _is_executable_file(path: str | None) -> bool:
    if not path:
        return False
    candidate = Path(path)
    return (
        candidate.is_absolute()
        and candidate.is_file()
        and os.access(candidate, os.X_OK)
    )
