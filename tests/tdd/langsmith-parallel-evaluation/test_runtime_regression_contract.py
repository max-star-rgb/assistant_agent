from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant_agent.evaluation.runtime_regression_contract import (
    assistant_output,
    request_text,
    validate_failure_baseline,
)


def test_contract_preserves_runtime_regression_object_shape() -> None:
    assert request_text(
        "item-1",
        {"role": "user", "content": "问题", "truncated": False},
    ) == "问题"

    baseline = validate_failure_baseline(
        "item-1",
        {
            "role": "assistant",
            "content": "失败回答",
            "chars": 4,
            "truncated": False,
            "terminal_status": "completed",
        },
    )
    actual = assistant_output(
        SimpleNamespace(
            response=SimpleNamespace(message="修复后的回答"),
            status="completed",
        )
    )

    assert baseline["content"] == "失败回答"
    assert actual == {
        "role": "assistant",
        "content": "修复后的回答",
        "chars": 6,
        "truncated": False,
        "terminal_status": "completed",
    }


@pytest.mark.parametrize("value", [None, '{"role":"assistant"}'])
def test_contract_rejects_non_object_baseline(value: object) -> None:
    with pytest.raises(RuntimeError, match="must be an object"):
        validate_failure_baseline("item-1", value)


def test_contract_rejects_truncated_input() -> None:
    with pytest.raises(RuntimeError, match="truncated"):
        request_text(
            "item-1",
            {"role": "user", "content": "不完整", "truncated": True},
        )
