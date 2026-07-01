"""Shared prompt and capability-output helpers outside the engine layer."""

from typing import Any

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.utils.prompting import (
    MAX_CONTEXT_CHARS,
    MAX_PROMPT_CHARS,
    build_image_prompt,
    build_image_prompt_text,
    clip_list,
    clip_text,
)


def build_text_capability_output(
    capability: str,
    status: str,
    output_ref: str | None = None,
    data: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stable public output contract without provider raw payloads."""

    payload = build_capability_output_contract(
        capability=capability,
        status=status,  # type: ignore[arg-type]
        output_ref=output_ref,
        data=data,
        errors=errors,
    ).model_dump(mode="json")
    if not payload.get("metadata"):
        payload.pop("metadata", None)
    return payload

