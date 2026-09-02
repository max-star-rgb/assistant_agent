"""Prompt-safe native Tool observation projections."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def prompt_observation_payload(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Project an internal observation into the semantic LLM-facing protocol."""

    tool_name = str(observation.get("tool_name") or "unknown")
    status = str(observation.get("status") or "failed")
    raw_data = observation.get("data")
    data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
    payload: dict[str, Any] = {"tool_name": tool_name, "status": status}
    summary = observation.get("summary")
    if isinstance(summary, str) and summary:
        payload["summary"] = summary

    outcome = observation.get("outcome")
    if outcome in {"success", "partial", "empty"}:
        payload["outcome"] = outcome

    warnings = observation.get("warnings")
    normalized_warnings = [
        warning for warning in warnings or [] if isinstance(warning, str) and warning
    ]
    if normalized_warnings:
        payload["warnings"] = normalized_warnings

    explicit_complete = observation.get("is_complete")
    payload["is_complete"] = (
        explicit_complete if isinstance(explicit_complete, bool) else status == "succeeded"
    )
    if data:
        payload["data"] = data

    error = observation.get("error")
    if isinstance(error, Mapping):
        payload["error"] = {
            key: error[key]
            for key in ("code", "message", "retryable")
            if error.get(key) not in (None, "")
        }

    output_ref = observation.get("output_ref")
    if (
        isinstance(output_ref, str)
        and output_ref
        and not _mapping_contains_value(data, output_ref)
    ):
        payload["output_ref"] = output_ref
    return payload


def native_tool_observation_payload(
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only the JSON content sent in a native role=tool message."""

    return {
        key: value
        for key, value in prompt_observation_payload(observation).items()
        if key != "tool_name"
    }


def _mapping_contains_value(value: Any, expected: str) -> bool:
    if isinstance(value, Mapping):
        return any(_mapping_contains_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_mapping_contains_value(item, expected) for item in value)
    return value == expected
