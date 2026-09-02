"""Prompt-safe diagnostics for observability hook dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.providers.provider_errors import sanitize_error_message


@dataclass(frozen=True)
class HookDispatchError:
    """Prompt-safe record of a failed event or trace dispatch target."""

    target_index: int
    target_name: str
    operation: str
    event_type: str | None
    canonical_event: str | None
    message: str


def build_hook_dispatch_error(
    *,
    target: object,
    target_index: int,
    operation: str,
    event: object | None,
    exc: BaseException,
) -> HookDispatchError:
    """Build a diagnostic without copying raw event payloads."""

    return HookDispatchError(
        target_index=target_index,
        target_name=type(target).__name__,
        operation=operation,
        event_type=_safe_text(_event_type(event)),
        canonical_event=_safe_text(getattr(event, "canonical_event", None)),
        message=sanitize_error_message(exc),
    )


def _event_type(event: object | None) -> Any:
    if event is None:
        return None
    return getattr(event, "type", None) or getattr(event, "event_type", None)


def _safe_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
