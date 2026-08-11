"""Backend-neutral data contract for production-derived runtime regressions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def request_text(item_id: str, inputs: Mapping[str, Any]) -> str:
    """Validate one Dataset input object and return its user request text."""

    if inputs.get("truncated") is True:
        raise RuntimeError(f"runtime regression item {item_id!r} input is truncated")
    role = inputs.get("role")
    if role is not None and role != "user":
        raise RuntimeError(
            f"runtime regression item {item_id!r} input role must be user"
        )
    text = inputs.get("content", inputs.get("request"))
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"runtime regression item {item_id!r} has no user content")
    return text


def validate_failure_baseline(
    item_id: str,
    reference_outputs: object,
) -> dict[str, Any]:
    """Validate and return one original failed assistant output object."""

    if not isinstance(reference_outputs, dict):
        raise RuntimeError(
            f"runtime regression item {item_id!r} expected_output must be an object"
        )
    if reference_outputs.get("role") != "assistant":
        raise RuntimeError(
            f"runtime regression item {item_id!r} expected_output role must be assistant"
        )
    content = reference_outputs.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            f"runtime regression item {item_id!r} expected_output has no assistant content"
        )
    return dict(reference_outputs)


def assistant_output(state: Any) -> dict[str, Any]:
    """Project one production runtime state to the shared actual-output object."""

    message = state.response.message if state.response is not None else ""
    return {
        "role": "assistant",
        "content": message,
        "chars": len(message),
        "truncated": False,
        "terminal_status": state.status,
    }
