"""Explicit memory tools; execution identity comes only from ToolRuntime."""
from typing import Literal

from langchain_core.tools import ToolException, tool
from langgraph.prebuilt import ToolRuntime

from assistant_agent.memory.edit_models import MemoryRequest
from assistant_agent.native_agent.context import AssistantRunContext, authenticated_user_identity
from assistant_agent.tools.native_boundary import configure_builtin_tool, native_content_and_artifact


def memory_tools(backend):
    if not getattr(backend, 'supports_edits', False):
        return []

    @tool(response_format='content_and_artifact')
    async def memory_read(runtime: ToolRuntime[AssistantRunContext], query: str = ''):
        """读取当前用户长期记忆；空 query 读取有界快照，查询结果中仅 editable=true 的记录可编辑。"""
        try:
            result = await backend.read(identity=authenticated_user_identity(runtime),
                thread_id=runtime.execution_info.thread_id, query=query)
            return native_content_and_artifact(result, result)
        except Exception as exc:
            raise ToolException('memory_read_failed') from exc

    @tool(response_format='content_and_artifact')
    async def memory_edit(action: Literal['append', 'replace'], runtime: ToolRuntime[AssistantRunContext],
                          content: str | None = None, memory_id: str | None = None,
                          old_text: str | None = None, new_text: str | None = None):
        """按用户明确要求新增或精确修改长期记忆。replace 先 read 获得 ID 与完整原文；new_text 为空删除。视觉记忆只读。"""
        operation_id = None
        try:
            args = MemoryRequest.model_validate({key: value for key, value in dict(action=action,
                content=content, memory_id=memory_id, old_text=old_text, new_text=new_text).items() if value is not None})
            thread_id = runtime.execution_info.thread_id
            if not thread_id or not runtime.tool_call_id:
                raise ValueError('tool_call_identity_required')
            operation_id = f'{thread_id}:{runtime.tool_call_id}'
            result = await backend.edit(identity=authenticated_user_identity(runtime), thread_id=thread_id,
                operation_id=operation_id, edit=args.model_dump(exclude_none=True))
            result = {**result, 'operation_id': operation_id}
            return native_content_and_artifact(result, result)
        except Exception as exc:
            if operation_id:
                result = {'status': 'error', 'code': 'outcome_unknown', 'operation_id': operation_id,
                          'usage': '请用 memory_edit_status 查询，不要重新提交新增或修改。'}
                return native_content_and_artifact(result, result)
            raise ToolException('memory_edit_failed_or_outcome_unknown') from exc

    @tool(response_format='content_and_artifact')
    async def memory_edit_status(operation_id: str, runtime: ToolRuntime[AssistantRunContext]):
        """查询当前用户的记忆编辑回执；outcome_unknown 不表示失败，不得通过重新新增恢复。"""
        if not operation_id or len(operation_id) > 512:
            raise ToolException('invalid_operation_id')
        try:
            result = await backend.edit_status(identity=authenticated_user_identity(runtime),
                thread_id=runtime.execution_info.thread_id, operation_id=operation_id)
            return native_content_and_artifact(result, result)
        except Exception as exc:
            raise ToolException('memory_status_unavailable') from exc

    return [configure_builtin_tool(value, bounded_expected_errors=True) for value in (memory_read, memory_edit, memory_edit_status)]
