"""Native side-effect Tool for explicitly submitting a memory to Mem0."""

from typing import Annotated, Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext, authenticated_user_identity
from assistant_agent.native_agent.memory import MemoryBackend
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool, native_content_and_artifact, native_tool_exception,
)


def create_memory_add_tool(backend: MemoryBackend) -> BaseTool:
    @tool("memory_add", response_format="content_and_artifact")
    async def memory_add(
        content: Annotated[str, Field(min_length=1, max_length=4000)],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """立即向 Mem0 提交一条长期记忆。用户明确要求记住某事实或偏好时调用。

        content 只包含用户明确提供、希望保存的事实，不加入助手猜测。
        成功表示 Mem0 已接受处理；不要承诺固定的记忆条数。
        """
        try:
            if not runtime.context.enable_memory:
                raise ValueError("当前会话未启用记忆")
            if not content.strip():
                raise ValueError("记忆内容不能为空")
            if not runtime.execution_info.thread_id:
                raise ValueError("记忆写入需要线程身份")
            await backend.commit(
                identity=authenticated_user_identity(runtime),
                thread_id=runtime.execution_info.thread_id,
                run_id=runtime.tool_call_id or runtime.execution_info.run_id,
                messages=[HumanMessage(content=content.strip())], store=runtime.store,
            )
            return native_content_and_artifact(
                {"status": "success", "summary": "已向 Mem0 提交记忆"},
                {"status": "success"},
            )
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(exc, tool_name="memory_add") from exc

    return configure_builtin_tool(memory_add, bounded_expected_errors=True)
