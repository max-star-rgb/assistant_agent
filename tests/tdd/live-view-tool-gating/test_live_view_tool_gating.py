from assistant_agent.context.models import AssistantContextPack, RealtimeVideoContext
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
)
from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.ids import LIVE_VIEW_INSPECT_TOOL_NAME
from assistant_agent.tools.models import ToolSpec


def test_live_video_is_exposed_only_as_a_tool_until_the_model_calls_it() -> None:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="你好",
        video_ids=["live-video-sentinel"],
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )
    live_view_spec = ToolSpec(
        name=LIVE_VIEW_INSPECT_TOOL_NAME,
        description="Inspect the current live view.",
        category="read",
        requires_media=["video"],
        media_scope="live",
    )
    selection = select_prompt_tool_specs(request, [live_view_spec])
    pack = AssistantContextPack(
        request=request,
        realtime_video_context=RealtimeVideoContext(
            status="ready",
            summary="visual-summary-sentinel",
            objects=["visual-object-sentinel"],
            snapshot_sequence=3,
            snapshot_age_ms=100,
        ),
        tool_specs=[live_view_spec],
        prompt_tool_specs=selection.available_tool_specs,
        run_tool_catalog=selection.run_tool_catalog,
        iteration=0,
        max_iterations=1,
    )

    compiled = PromptCompiler().compile(
        PromptCompileRequest(
            user_id=request.user_id,
            session_id=request.session_id,
            mode=PromptCompileMode.NATIVE_TOOL,
            user_query_fallback="fallback-sentinel",
            context_pack=pack,
            observations=(),
            native_calls=(),
            tool_call_id_prefix="call_",
        )
    )

    assert compiled.chat_request.messages[-1] == {"role": "user", "content": "你好"}
    assert [tool["function"]["name"] for tool in compiled.chat_request.tools] == [
        LIVE_VIEW_INSPECT_TOOL_NAME
    ]
