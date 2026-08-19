"""Image generation tool plugin."""

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.generated_artifacts import GENERATED_ARTIFACT_DIR
from assistant_agent.tools.plugins.builtin.image_generation.backend import (
    LocalFixtureImageGenerationAdapter,
    create_image_generation_adapter,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    ImageGenerationTool,
)


# Development-only whole-chain fixture. Set to None to restore the real image Provider.
DEVELOPMENT_IMAGE_FIXTURE_ID: str | None = "349cc6c272f4ec7a88800f0f.png"


class ImageGenerationToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if DEVELOPMENT_IMAGE_FIXTURE_ID:
            return [
                ImageGenerationTool(
                    adapter=LocalFixtureImageGenerationAdapter(
                        DEVELOPMENT_IMAGE_FIXTURE_ID,
                        artifact_dir=GENERATED_ARTIFACT_DIR,
                    ),
                    artifact_base_url=context.config.artifact_base_url,
                )
            ]
        if not context.mock_mode and not image_generation_provider_ready(
            context.config
        ):
            return []
        return [
            ImageGenerationTool(
                adapter=create_image_generation_adapter(context.config),
                artifact_base_url=context.config.artifact_base_url,
            )
        ]


def image_generation_provider_ready(config: ProviderConfig) -> bool:
    return (
        config.image_generation_provider != "mock"
        and not config.resolved_image_generation_provider().missing_required_env()
    )
