"""Lodging search Tool plugin."""

import os
from pathlib import Path

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.builtin.lodging.tool import LodgingSearchTool
from assistant_agent.tools.plugins.builtin.lodging.backend import (
    FlyAILodgingSearchAdapter,
)
from assistant_agent.tools.plugins.builtin.lodging.watch_tool import (
    HotelPriceWatchCreateTool,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)


class LodgingToolPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="lodging", plugin_version="1")

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        if context.mock_mode:
            tools: list[Tool] = [LodgingSearchTool()]
        elif context.config.lodging_provider == "flyai" and _is_executable_file(
            context.config.flyai_cli_path
        ) and context.config.flyai_api_key:
            tools = [
                LodgingSearchTool(
                    FlyAILodgingSearchAdapter(
                        cli_path=context.config.flyai_cli_path,
                        api_key=context.config.flyai_api_key,
                        timeout_seconds=context.config.flyai_timeout_seconds,
                    )
                )
            ]
        else:
            return []
        if (
            context.config.durable_tasks_enabled
            and context.durable_task_service is not None
        ):
            tools.append(HotelPriceWatchCreateTool(context.durable_task_service))
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
