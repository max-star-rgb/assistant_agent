"""Run-scoped cooperative cancellation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CANCELLATION_ERROR_CODE = "agent_run_cancelled"
DEFAULT_CANCELLATION_MESSAGE = "Agent run cancelled."
_CANCEL_METADATA_KEYS = frozenset({"cancel_source", "cancel_reason", "deadline_ms"})


class AgentRunCancelled(RuntimeError):
    """Raised inside one agent run when its cancel token is set."""

    def __init__(
        self,
        message: str = DEFAULT_CANCELLATION_MESSAGE,
        *,
        phase: str | None = None,
        node_name: str | None = None,
        source: str = "agent_runtime",
        details: dict[str, Any] | None = None,
        state: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.phase = phase
        self.node_name = node_name
        self.source = source
        self.state = state
        self.details = cancellation_details(phase=phase, node_name=node_name, **(details or {}))


def is_cancelled(cancel_token: Any | None) -> bool:
    """Return whether a run-scoped cancel token has been triggered."""

    if cancel_token is None:
        return False
    checker = getattr(cancel_token, "is_cancelled", None)
    if callable(checker):
        return bool(checker())
    cancelled = getattr(cancel_token, "cancelled", None)
    return bool(cancelled) if isinstance(cancelled, bool) else False


def raise_if_cancelled(
    cancel_token: Any | None,
    *,
    phase: str | None = None,
    node_name: str | None = None,
    source: str = "agent_runtime",
    details: dict[str, Any] | None = None,
    state: Any | None = None,
) -> None:
    """Raise AgentRunCancelled when the run token has been cancelled."""

    if is_cancelled(cancel_token):
        cancel_details = cancellation_metadata(cancel_token)
        if details is not None:
            cancel_details.update(details)
        raise AgentRunCancelled(
            DEFAULT_CANCELLATION_MESSAGE,
            phase=phase,
            node_name=node_name,
            source=source,
            details=cancel_details,
            state=state,
        )


def cancellation_metadata(cancel_token: Any | None) -> dict[str, Any]:
    """Return safe cancellation metadata exposed by a cooperative token."""

    if cancel_token is None:
        return {}
    raw = _token_metadata(cancel_token)
    if not isinstance(raw, Mapping):
        return {}
    metadata: dict[str, Any] = {}
    for key in _CANCEL_METADATA_KEYS:
        if key in raw:
            metadata[key] = raw[key]
    source = raw.get("source")
    if "cancel_source" not in metadata and isinstance(source, str) and source:
        metadata["cancel_source"] = source
    reason = raw.get("reason")
    if "cancel_reason" not in metadata and isinstance(reason, str) and reason:
        metadata["cancel_reason"] = reason
    return metadata


def cancellation_details(
    *,
    phase: str | None = None,
    node_name: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build structured error details for a cancelled agent run."""

    details = {"code": CANCELLATION_ERROR_CODE}
    if phase is not None:
        details["cancel_phase"] = phase
    if node_name is not None:
        details["node_name"] = node_name
    details.update(extra)
    return details


def _token_metadata(cancel_token: Any) -> Any:
    for attr_name in ("cancel_metadata", "metadata"):
        value = getattr(cancel_token, attr_name, None)
        if callable(value):
            try:
                value = value()
            except TypeError:
                continue
        if isinstance(value, Mapping):
            return value
    return None
