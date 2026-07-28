"""Stable model-facing contract for ReAct tool observations."""

import pytest
from pydantic import ValidationError

from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.observation import (
    ToolObservation,
    native_tool_observation_payload,
    observation_from_tool_result,
)


def test_success_observation_keeps_summary_and_domain_data_on_separate_layers() -> None:
    result = ToolResult(
        tool_name="batch_reader",
        success=True,
        model_observation={
            "summary": "读取 3 项中的 2 项，另有 1 项超时。",
            "outcome": "partial",
            "warnings": ["当前结果不完整。"],
            "items": [{"id": "item-1"}, {"id": "item-2"}],
            "errors": [
                {
                    "code": "provider_timeout",
                    "message": "item-3 timed out",
                    "recoverable": True,
                }
            ],
        },
        output_ref="artifact://batch/one",
    )

    observation = observation_from_tool_result(result)
    assert observation.model_dump(mode="json") == {
        "tool_name": "batch_reader",
        "status": "succeeded",
        "summary": "读取 3 项中的 2 项，另有 1 项超时。",
        "outcome": "partial",
        "warnings": ["当前结果不完整。"],
        "is_complete": False,
        "output_ref": "artifact://batch/one",
        "data": {
            "items": [{"id": "item-1"}, {"id": "item-2"}],
        },
        "error": {
            "code": "provider_timeout",
            "message": "item-3 timed out",
            "retryable": True,
        },
    }
    payload = native_tool_observation_payload(
        observation.model_dump(mode="json")
    )

    assert payload == {
        "status": "succeeded",
        "summary": "读取 3 项中的 2 项，另有 1 项超时。",
        "outcome": "partial",
        "warnings": ["当前结果不完整。"],
        "is_complete": False,
        "data": {
            "items": [{"id": "item-1"}, {"id": "item-2"}],
        },
        "error": {
            "code": "provider_timeout",
            "message": "item-3 timed out",
            "retryable": True,
        },
        "output_ref": "artifact://batch/one",
    }


def test_failed_observation_separates_execution_status_from_structured_error() -> None:
    result = ToolResult(
        tool_name="batch_reader",
        success=False,
        error="provider_timeout: upstream timed out",
        model_observation={
            "summary": "上游读取超时。",
            "errors": [
                {
                    "code": "provider_timeout",
                    "message": "upstream timed out",
                    "recoverable": True,
                }
            ],
        },
    )

    observation = observation_from_tool_result(result)
    payload = native_tool_observation_payload(
        observation.model_dump(mode="json")
    )

    assert payload["status"] == "failed"
    assert payload["summary"] == "上游读取超时。"
    assert payload["is_complete"] is False
    assert payload["error"] == {
        "code": "provider_timeout",
        "message": "upstream timed out",
        "retryable": True,
    }
    assert "outcome" not in payload
    assert "data" not in payload


def test_observation_rejects_removed_compatibility_fields() -> None:
    with pytest.raises(ValidationError):
        ToolObservation.model_validate(
            {
                "tool_name": "weather",
                "status": "failed",
                "summary": "天气服务超时。",
                "structured_output": {"location": "上海"},
                "error_code": "provider_timeout",
                "error_message": "天气服务超时。",
                "next_step_hint": "retry",
            }
        )
