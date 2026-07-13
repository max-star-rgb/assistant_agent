from copy import deepcopy

from assistant_agent.agent.system_prompt_policy import (
    SystemPromptOptions,
    SystemPromptProfile,
    render_system_instruction,
)
from assistant_agent.schemas.context import AssistantContextPack
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import RunToolSet, ToolSpec
from assistant_agent.services.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
)
from assistant_agent.services.context.renderer import (
    render_final_only_context,
    render_native_tool_context,
)


def _pack(text: str = "帮我查耳机") -> AssistantContextPack:
    product = ToolSpec(name="product_search", required_inputs=["query"])
    hidden = ToolSpec(name="render_3d", required_inputs=["scene_description"])
    return AssistantContextPack(
        request=UserRequest(user_id="u1", session_id="s1", text=text),
        observations=[{"tool_name": "product_search", "status": "succeeded"}],
        tool_specs=[product, hidden],
        prompt_tool_specs=[product],
        iteration=1,
        max_iterations=5,
    )


def _compile(
    pack: AssistantContextPack,
    mode: PromptCompileMode,
    **overrides: object,
):
    values = {
        "user_id": "u1",
        "session_id": "s1",
        "mode": mode,
        "user_query_fallback": "fallback",
        "profile": SystemPromptProfile.TEXT_DEFAULT,
        "options": SystemPromptOptions(product_mode=True),
        "context_pack": pack,
        "observations": tuple(pack.observations),
        "native_calls": ({},),
        "tool_call_id_prefix": "call_",
    }
    values.update(overrides)
    return PromptCompiler().compile(PromptCompileRequest(**values))  # type: ignore[arg-type]


def test_native_tool_mode_preserves_provider_request_contract() -> None:
    pack = _pack()
    callback = lambda _text, _payload: None

    result = _compile(
        pack,
        PromptCompileMode.NATIVE_TOOL,
        stream_callback=callback,
    )

    request = result.chat_request
    assert request.messages[0]["content"] == render_system_instruction(
        SystemPromptProfile.TEXT_DEFAULT,
        options=SystemPromptOptions(product_mode=True),
    )
    assert request.messages[1]["content"] == render_native_tool_context(pack).native_user_message
    assert request.messages[2]["tool_calls"][0]["id"] == "call_1"
    assert request.messages[3]["tool_call_id"] == "call_1"
    assert [tool["function"]["name"] for tool in request.tools] == ["product_search"]
    assert request.tool_choice == "auto"
    assert request.temperature == 0.2
    assert request.max_tokens == 1024
    assert request.stream_callback is callback


def test_native_tool_mode_preserves_intentionally_empty_governed_tool_set() -> None:
    pack = _pack()
    pack.prompt_tool_specs = []
    pack.run_tool_set = RunToolSet(
        registered_tool_names=["product_search", "render_3d"],
        qualified_tool_names=[],
        exposed_tool_names=[],
        executable_tool_names=[],
        excluded_reasons={
            "product_search": ["disabled_by_default"],
            "render_3d": ["skill_activation_required"],
        },
    )

    result = _compile(pack, PromptCompileMode.NATIVE_TOOL)

    assert result.selected_tool_specs == ()
    assert result.chat_request.tools == []


def test_native_final_only_keeps_tool_evidence_but_disables_tools() -> None:
    pack = _pack()

    result = _compile(
        pack,
        PromptCompileMode.NATIVE_FINAL_ONLY,
        profile=SystemPromptProfile.FINAL_ONLY,
        options=SystemPromptOptions(),
        user_query_fallback="native runtime final answer",
        tool_call_id_prefix="native_runtime_call_",
    )

    assert result.chat_request.messages[1]["content"] == render_native_tool_context(pack).native_user_message
    assert result.chat_request.messages[2]["tool_calls"][0]["id"] == "native_runtime_call_1"
    assert any(message["role"] == "tool" for message in result.chat_request.messages)
    assert result.chat_request.tools == []
    assert result.chat_request.tool_choice == "none"


def test_summary_final_only_uses_summary_prompt_as_query_and_user_message() -> None:
    pack = _pack()
    expected = render_final_only_context(pack).final_only_prompt

    result = _compile(
        pack,
        PromptCompileMode.SUMMARY_FINAL_ONLY,
        profile=SystemPromptProfile.FINAL_ONLY,
        options=SystemPromptOptions(),
        native_calls=(),
    )

    assert result.chat_request.user_query == expected
    assert result.chat_request.messages == [
        {
            "role": "system",
            "content": render_system_instruction(SystemPromptProfile.FINAL_ONLY),
        },
        {"role": "user", "content": expected},
    ]
    assert result.chat_request.tools == []
    assert result.chat_request.tool_choice is None


def test_compile_preserves_raw_call_and_does_not_mutate_inputs() -> None:
    pack = _pack()
    calls = (
        {
            "raw": {
                "id": "raw-1",
                "type": "function",
                "function": {
                    "name": "product_search",
                    "arguments": '{"query":"耳机"}',
                },
            }
        },
    )
    before_pack = pack.model_dump(mode="json")
    before_calls = deepcopy(calls)

    result = _compile(
        pack,
        PromptCompileMode.NATIVE_TOOL,
        native_calls=calls,
    )

    assert result.chat_request.messages[2]["tool_calls"][0] == calls[0]["raw"]
    assert pack.model_dump(mode="json") == before_pack
    assert calls == before_calls


def test_native_tool_mode_uses_caller_fallback_for_empty_user_text() -> None:
    pack = _pack(text="")

    result = _compile(
        pack,
        PromptCompileMode.NATIVE_TOOL,
        user_query_fallback="native_tools assistant turn",
    )

    assert result.chat_request.user_query == "native_tools assistant turn"
