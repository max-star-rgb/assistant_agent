"""Regressions for deterministic finalization fallback text."""

from assistant_agent.context import finalization
from assistant_agent.context.finalization import finalize_fallback_text


def test_fallback_reports_failed_fact_even_when_other_tools_succeeded() -> None:
    text = finalize_fallback_text(
        [
            {
                "tool_name": "weather",
                "status": "failed",
                "summary": "受控天气服务超时，当前没有可用预报。",
                "error": {
                    "code": "provider_timeout",
                    "message": "受控天气服务超时，当前没有可用预报。",
                    "retryable": True,
                },
            },
            {
                "tool_name": "web_search",
                "status": "succeeded",
                "summary": "找到若干网页结果。",
                "is_complete": True,
            },
        ]
    )

    assert text == (
        "本轮部分信息获取失败：受控天气服务超时，当前没有可用预报。"
        "虽然取得了其他工具结果，但现有证据仍不足以可靠完成你的请求。"
        "我不会据此编造结论，请稍后重试。"
    )


def test_causal_pairs_skip_orphans_instead_of_fabricating_native_calls() -> None:
    pairer = getattr(finalization, "correlated_native_tool_pairs", lambda *_: [])
    pairs = pairer(
        [
            {"id": "call-first", "name": "weather"},
            {"id": "call-second", "name": "web_search"},
        ],
        [
            {
                "_provider_tool_call_id": "call-second",
                "tool_name": "web_search",
                "status": "succeeded",
                "summary": "search-result",
            },
            {
                "_provider_tool_call_id": "call-orphan",
                "tool_name": "calendar_search",
                "status": "succeeded",
                "summary": "orphan-result",
            },
            {
                "_provider_tool_call_id": "call-first",
                "tool_name": "weather",
                "status": "failed",
                "summary": "weather-timeout",
            },
            {
                "tool_name": "weather",
                "status": "failed",
                "summary": "uncorrelated-result",
            },
        ],
    )

    assert [
        (call["id"], observation["summary"])
        for _, call, observation in pairs
    ] == [
        ("call-second", "search-result"),
        ("call-first", "weather-timeout"),
    ]


def test_causal_pairs_drop_ambiguous_or_inconsistent_correlations() -> None:
    pairs = finalization.correlated_native_tool_pairs(
        [
            {"id": "call-duplicate", "name": "weather"},
            {"id": "call-duplicate", "name": "web_search"},
            {
                "id": "call-raw-mismatch",
                "name": "weather",
                "raw": {"id": "different-raw-id"},
            },
            {
                "id": "call-raw-name-mismatch",
                "name": "weather",
                "raw": {
                    "id": "call-raw-name-mismatch",
                    "function": {"name": "web_search"},
                },
            },
            {
                "id": "call-polluted",
                "name": "weather",
                "raw": {"id": "different-polluted-id"},
            },
            {"id": "call-polluted", "name": "weather"},
            {"id": "call-name-mismatch", "name": "weather"},
            {"id": "call-observation-duplicate", "name": "weather"},
            {
                "id": "call-valid",
                "name": "weather",
                "raw": {"id": "call-valid"},
            },
        ],
        [
            {
                "_provider_tool_call_id": "call-duplicate",
                "tool_name": "weather",
                "status": "failed",
                "summary": "ambiguous-call",
            },
            {
                "_provider_tool_call_id": "call-raw-mismatch",
                "tool_name": "weather",
                "status": "failed",
                "summary": "raw-mismatch",
            },
            {
                "_provider_tool_call_id": "call-name-mismatch",
                "tool_name": "web_search",
                "status": "succeeded",
                "summary": "name-mismatch",
            },
            {
                "_provider_tool_call_id": "call-raw-name-mismatch",
                "tool_name": "weather",
                "status": "failed",
                "summary": "raw-name-mismatch",
            },
            {
                "_provider_tool_call_id": "call-polluted",
                "tool_name": "weather",
                "status": "failed",
                "summary": "polluted-duplicate",
            },
            {
                "_provider_tool_call_id": "call-observation-duplicate",
                "tool_name": "weather",
                "status": "failed",
                "summary": "duplicate-observation-first",
            },
            {
                "_provider_tool_call_id": "call-observation-duplicate",
                "tool_name": "weather",
                "status": "failed",
                "summary": "duplicate-observation-second",
            },
            {
                "_provider_tool_call_id": "call-valid",
                "tool_name": "weather",
                "status": "failed",
                "summary": "valid-result",
            },
        ],
    )

    assert [
        (call["id"], observation["summary"])
        for _, call, observation in pairs
    ] == [("call-valid", "valid-result")]
