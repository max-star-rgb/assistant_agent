from assistant_agent.schemas.llm_events import (
    LLMEvent,
    LLMEventAccumulator,
    LLMProviderError,
    LLMToolCallDelta,
)


def test_llm_event_accumulator_collects_token_deltas_and_completion_metadata() -> None:
    accumulator = LLMEventAccumulator()

    accumulator.apply(
        LLMEvent(
            event_type="token_delta",
            provider="deepseek",
            model="deepseek-chat",
            text="真实",
            metadata={"token_streaming": True},
        )
    )
    accumulator.apply(
        LLMEvent(
            event_type="token_delta",
            provider="deepseek",
            model="deepseek-chat",
            text=" DeepSeek 回复",
        )
    )
    accumulator.apply(
        LLMEvent(
            event_type="completed",
            provider="deepseek",
            model="deepseek-chat",
            finish_reason="stop",
            usage={"prompt_tokens": 4, "completion_tokens": 3},
        )
    )

    assert accumulator.provider == "deepseek"
    assert accumulator.model == "deepseek-chat"
    assert accumulator.response_text == "真实 DeepSeek 回复"
    assert accumulator.finish_reason == "stop"
    assert accumulator.usage == {"prompt_tokens": 4, "completion_tokens": 3}
    assert accumulator.finalize_tool_calls() == []


def test_llm_event_accumulator_aggregates_tool_call_deltas_by_index() -> None:
    accumulator = LLMEventAccumulator()

    accumulator.apply(
        LLMEvent(
            event_type="tool_call_delta",
            provider="deepseek",
            model="deepseek-chat",
            tool_call_delta=LLMToolCallDelta(
                index=0,
                id="call_1",
                type="function",
                name_delta="shopping_search",
                arguments_delta='{"query": "通勤',
            ),
        )
    )
    accumulator.apply(
        LLMEvent(
            event_type="tool_call_delta",
            provider="deepseek",
            model="deepseek-chat",
            tool_call_delta=LLMToolCallDelta(
                index=0,
                arguments_delta='耳机", "limit": 2}',
            ),
        )
    )
    accumulator.apply(
        LLMEvent(
            event_type="completed",
            provider="deepseek",
            model="deepseek-chat",
            finish_reason="tool_calls",
        )
    )

    calls = accumulator.finalize_tool_calls()

    assert accumulator.finish_reason == "tool_calls"
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "shopping_search"
    assert calls[0].arguments == {"query": "通勤耳机", "limit": 2}
    assert calls[0].provider_format == "llm_event"
    assert calls[0].raw == {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "shopping_search",
            "arguments": '{"query": "通勤耳机", "limit": 2}',
        },
    }


def test_llm_event_accumulator_can_preserve_source_provider_format() -> None:
    accumulator = LLMEventAccumulator()
    accumulator.apply(
        LLMEvent(
            event_type="tool_call_delta",
            provider="openai",
            model="gpt-test",
            tool_call_delta=LLMToolCallDelta(
                index=0,
                id="call_1",
                name_delta="web_search",
                arguments_delta='{"query": "今天新闻"}',
            ),
        )
    )

    calls = accumulator.finalize_tool_calls(provider_format="openai_compatible")

    assert len(calls) == 1
    assert calls[0].provider_format == "openai_compatible"
    assert calls[0].name == "web_search"


def test_llm_event_accumulator_keeps_latest_prompt_safe_error() -> None:
    accumulator = LLMEventAccumulator()

    accumulator.apply(
        LLMEvent(
            event_type="error",
            provider="openai",
            model="gpt-test",
            error=LLMProviderError(
                code="provider_timeout",
                message="provider timed out",
                recoverable=True,
            ),
        )
    )

    assert accumulator.provider == "openai"
    assert accumulator.model == "gpt-test"
    assert accumulator.error is not None
    assert accumulator.error.code == "provider_timeout"
    assert accumulator.error.message == "provider timed out"
    assert accumulator.error.recoverable is True
