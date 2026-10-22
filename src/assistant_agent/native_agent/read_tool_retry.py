"""Native read-tool retry with bounded next-turn recovery guidance."""

from langchain.agents.middleware import ToolRetryMiddleware
from langchain_core.messages import HumanMessage, ToolMessage


_FAILURE = "工具执行失败，首次调用及 1 次自动重试均未成功。"
_GUIDANCE = (
    "运行反馈（非用户新请求）：以下工具首次调用及 1 次自动重试均失败：{names}。"
    "不要继续重复调用，也不要通过同一服务的其他工具入口重复尝试。"
    "建议结合可用的联网搜索和已有信息直接回答用户，并如实说明工具失败与信息局限。"
    "联网搜索不能代替已失败的实际操作，不得声称操作成功，"
    "不得编造实时酒店报价、库存或预订结果。"
)


class ReadToolRetryMiddleware(ToolRetryMiddleware):
    """Keep the official retry loop; project guidance only after exhausted retries."""

    def __init__(self, *, tools):
        super().__init__(
            tools=tools, max_retries=1, initial_delay=0, backoff_factor=0,
            jitter=False, on_failure=lambda exc: _FAILURE,
        )

    def _recovery_request(self, request):
        failed = set()
        for message in reversed(request.messages):
            if not isinstance(message, ToolMessage):
                break
            if message.status == "error" and message.content == _FAILURE:
                failed.add(message.name or "tool")
        if not failed:
            return request
        return request.override(messages=[
            *request.messages,
            HumanMessage(
                content=_GUIDANCE.format(names="、".join(sorted(failed))),
                name="tool_recovery",
            ),
        ])

    def wrap_model_call(self, request, handler):
        return handler(self._recovery_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._recovery_request(request))
