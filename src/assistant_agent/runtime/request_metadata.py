"""Trust-boundary helpers for externally supplied request metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


RUNTIME_OWNED_REQUEST_METADATA_KEYS = frozenset(
    {
        "system_prompt_profile",
        "channel",
        "source",
        "durable_task_binding",
        "durable_task_snapshot",
        "durable_idempotency_key",
        "worker_lease",
        "_trusted_durable_execution",
        "_trusted_workflow_assignment",
        "_trusted_workflow_max_iterations",
        "_trusted_workflow_allowed_tools",
    }
)


def sanitize_external_request_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Remove runtime-owned capabilities from untrusted entry metadata."""

    return {
        key: value
        for key, value in metadata.items()
        if key not in RUNTIME_OWNED_REQUEST_METADATA_KEYS
    }
