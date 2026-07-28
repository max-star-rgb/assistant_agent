"""Stable model-facing contract for ReAct tool observations."""

from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.observation import (
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
        },
        output_ref="artifact://batch/one",
    )

    observation = observation_from_tool_result(result)
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
        "ref": "artifact://batch/one",
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
        "recoverable": True,
    }
    assert "outcome" not in payload
    assert "data" not in payload
