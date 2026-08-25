"""Built-in project Skill loading plugin."""

from dataclasses import dataclass

from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    create_load_skill_reference_tool,
    create_load_skill_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


@dataclass(frozen=True)
class SkillLoadingPlugin:
    backend: BackendProtocol

    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        del context
        return [
            create_load_skill_tool(backend=self.backend),
            create_load_skill_reference_tool(backend=self.backend),
        ]
