"""Native image editing Tool with a configured local mock output."""

import asyncio
from typing import Annotated, Any, Literal

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.media.generated_artifacts import generated_artifact_payload_for_thread
from assistant_agent.native_agent.context import AssistantRunContext, authenticated_user_identity
from assistant_agent.runtime.thread_resources import ThreadResourceManager
from assistant_agent.tools.native_boundary import configure_builtin_tool, native_tool_exception
from assistant_agent.tools.image_history import latest_image_ref
from assistant_agent.tools.plugins.builtin.image_generation.backend import MockImageGenerationAdapter
from assistant_agent.tools.plugins.builtin.image_generation.tool import execute_image_output


class MockImageEditingAdapter(MockImageGenerationAdapter):
    model = "mock-image-editing"
    fixed_prompt = "编辑这个白色的台灯"


def create_image_editing_tool(
    *, mock_file: str, thread_resource_manager: ThreadResourceManager,
) -> BaseTool:
    adapter = MockImageEditingAdapter(mock_file)

    @tool("image_editing", response_format="content_and_artifact")
    async def image_editing(
        prompt: Annotated[
            Literal["编辑这个白色的台灯"],
            Field(description="mock 联调固定参数，必须原样传入：编辑这个白色的台灯。"),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """mock 图片编辑：prompt 必须原样填写‘编辑这个白色的台灯’，不要扩写或替换为其他修改要求。

        自动使用本线程最近生成或编辑的图片，返回‘已编辑这个白色台灯’，图片由系统自动交付。
        图生 3D 流程先生成图片，再调用本工具，最后将编辑结果交给 image_to_3d。
        """
        try:
            src_image = latest_image_ref(
                runtime.state, tool_names=("image_generation", "image_editing"),
            )
            if src_image is None:
                raise ValueError("image_editing requires an existing image; call image_generation first")
            source = await asyncio.to_thread(
                generated_artifact_payload_for_thread, src_image, thread_resource_manager,
                identity=authenticated_user_identity(runtime),
                thread_id=str(runtime.execution_info.thread_id or ""),
            )
            if source is None:
                raise ValueError("image_editing requires an existing image in the current thread")
            return await execute_image_output(
                adapter, prompt, runtime, thread_resource_manager=thread_resource_manager,
                capability="image_editing",
            )
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(exc, tool_name="image_editing") from exc

    return configure_builtin_tool(image_editing, bounded_expected_errors=True)
