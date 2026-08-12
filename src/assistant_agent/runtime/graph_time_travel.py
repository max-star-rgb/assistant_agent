"""Safe product contracts for native graph time-travel capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GraphCheckpointSelector(_StrictModel):
    """Opaque reference to one re-entry-safe native graph checkpoint."""

    history_ref: str = Field(pattern=r"^ghr_[0-9a-f]{32}$")


class GraphCheckpointSummary(_StrictModel):
    """Bounded product-safe projection of one native state snapshot."""

    history_ref: str = Field(pattern=r"^ghr_[0-9a-f]{32}$")
    created_at: datetime
    status: Literal["running", "waiting_user", "completed", "failed", "cancelled"]
    next_nodes: tuple[str, ...]
    has_interrupt: bool
    graph_version: str
    state_schema_version: int


class GraphReplayRequest(_StrictModel):
    """Request to replay one owner-bound, re-entry-safe checkpoint."""

    selector: GraphCheckpointSelector


class GraphForkPatch(_StrictModel):
    """Allowlisted product fields that may differ on one native branch."""

    request_text: str | None = Field(default=None, max_length=32_000)
    response_style: Literal[
        "conversation", "concise", "structured", "voice"
    ] | None = None


class GraphForkRequest(_StrictModel):
    """Request one owner-bound branch without exposing native checkpoint IDs."""

    selector: GraphCheckpointSelector
    patch: GraphForkPatch


def fork_patch_for_assistant_state(
    historical: Mapping[str, Any],
    patch: GraphForkPatch,
) -> dict[str, Any]:
    """Apply only product-owned request fields to validated checkpoint state."""

    if not isinstance(patch, GraphForkPatch):
        raise TypeError("patch must be a GraphForkPatch")
    from assistant_agent.runtime.assistant_graph_state import (
        validate_assistant_turn_state,
    )

    persisted = validate_assistant_turn_state(historical)
    updated = deepcopy(dict(persisted))
    request = dict(cast(Mapping[str, Any], persisted["request"]))
    if "request_text" in patch.model_fields_set:
        prior_text = request.get("text")
        messages = list(request.get("messages") or ())
        if (
            prior_text is not None
            and messages
            and messages[-1].get("role") == "user"
            and messages[-1].get("text") == prior_text
        ):
            messages.pop()
        request["text"] = patch.request_text
        if patch.request_text is not None:
            messages.append(
                {"role": "user", "text": patch.request_text, "tool_call_id": None}
            )
        request["messages"] = messages[-128:]
    if "response_style" in patch.model_fields_set and patch.response_style is not None:
        request["response_style"] = patch.response_style
    updated["request"] = request
    return cast(dict[str, Any], validate_assistant_turn_state(updated))


def graph_history_ref(
    *,
    thread_id: str,
    snapshot_config: Mapping[str, Any],
) -> str:
    """Derive an opaque selector without retaining a process-local lookup map."""

    canonical = json.dumps(
        {
            "domain": "assistant_graph_history_ref_v1",
            "thread_id": thread_id,
            "config": snapshot_config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "ghr_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "GraphCheckpointSelector",
    "GraphCheckpointSummary",
    "GraphForkPatch",
    "GraphForkRequest",
    "GraphReplayRequest",
    "fork_patch_for_assistant_state",
    "graph_history_ref",
]
