"""Image-to-3D tool plugin assembly."""

from assistant_agent.media.image_to_3d import ImageTo3DAdapter, ImageTo3DSettings
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.image_to_3d.tool import (
    create_image_to_3d_tool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class ImageTo3DToolPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        if context.mock_mode:
            return [create_image_to_3d_tool()]
        config = context.media_config
        required = (
            config.td_gen_ip,
            config.td_gen_port,
            config.public_ip,
            config.public_port,
        )
        if not all(required):
            return []
        if context.thread_resource_manager is None:
            return []
        settings = ImageTo3DSettings(
            td_gen_url=(
                f"http://{config.td_gen_ip}:{config.td_gen_port}"
                "/3dgen/v1/openapi/img-to-3d"
            ),
            public_base_url=f"http://{config.public_ip}:{config.public_port}",
            timeout_seconds=config.image_to_3d_timeout_seconds,
        )
        return [
            create_image_to_3d_tool(
                adapter=ImageTo3DAdapter(
                    settings,
                    artifact_root_resolver=lambda user_id, thread_id: (
                        context.thread_resource_manager.resolve(
                            user_id,
                            thread_id,
                        ).artifact_root
                    ),
                )
            )
        ]
