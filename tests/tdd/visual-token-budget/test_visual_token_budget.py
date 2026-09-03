from assistant_agent.media.video.token_budget import (
    ContextWindowPolicy,
    normalize_provider_token_usage,
)


def test_visual_token_budget_keeps_policy_and_usage_normalization() -> None:
    decision = ContextWindowPolicy(input_token_limit=100).evaluate(
        70,
        reserved_output_tokens=10,
    )

    assert decision.effective_input_limit == 90
    assert decision.triggered is True
    assert decision.hard is False
    assert normalize_provider_token_usage(
        {"input_tokens": 4, "output_tokens": 3}
    ) == {
        "prompt_tokens": 4,
        "completion_tokens": 3,
        "total_tokens": 7,
    }
