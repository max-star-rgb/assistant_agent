"""Run-scoped cooperative cancellation helpers."""

from __future__ import annotations

from typing import Any


CANCELLATION_ERROR_CODE = "agent_run_cancelled"
DEFAULT_CANCELLATION_MESSAGE = "Agent run cancelled."


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
        raise AgentRunCancelled(
            DEFAULT_CANCELLATION_MESSAGE,
            phase=phase,
            node_name=node_name,
            source=source,
            details=details,
            state=state,
        )


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
