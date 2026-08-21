"""Built-in project Skill loading plugin."""

from dataclasses import dataclass

from assistant_agent.skills.loading import (
    SkillCatalog,
    default_repo_root,
    load_repo_skill_descriptors,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    create_load_skill_reference_tool,
    create_load_skill_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


@dataclass(frozen=True)
class SkillLoadingPlugin:
    skill_catalog: SkillCatalog | None = None

    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        root = default_repo_root()
        catalog = self.skill_catalog
        if catalog is None:
            catalog = load_repo_skill_descriptors(root)
        if not catalog.descriptors:
            return []
        return [
            create_load_skill_tool(root=root),
            create_load_skill_reference_tool(root=root),
        ]
