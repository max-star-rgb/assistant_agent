"""Image-to-3D tool plugin assembly."""

from assistant_agent.media.image_to_3d import ImageTo3DAdapter, ImageTo3DSettings
from assistant_agent.runtime.generated_artifacts import GENERATED_ARTIFACT_DIR
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.image_to_3d.tool import (
    create_image_to_3d_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class ImageTo3DToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if context.mock_mode:
            return [create_image_to_3d_tool()]
        config = context.config
        required = (
            config.td_gen_ip,
            config.td_gen_port,
            config.public_ip,
            config.public_port,
        )
        if not all(required):
            return []
        settings = ImageTo3DSettings(
            td_gen_url=(
                f"http://{config.td_gen_ip}:{config.td_gen_port}"
                "/3dgen/v1/openapi/img-to-3d"
            ),
            public_base_url=f"http://{config.public_ip}:{config.public_port}",
            generated_artifact_path=GENERATED_ARTIFACT_DIR,
            timeout_seconds=config.image_to_3d_timeout_seconds,
        )
        return [create_image_to_3d_tool(adapter=ImageTo3DAdapter(settings))]
