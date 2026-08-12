"""Process-local invocation claims for native assistant graph executions."""

from __future__ import annotations

from threading import Lock
from typing import Literal, Protocol


GraphInvocationKind = Literal["invoke", "resume", "replay", "fork"]
GraphInvocationClaimResult = Literal["claimed", "same_invocation"]


class GraphInvocationClaimConflict(RuntimeError):
    """Raised when a different invocation reuses an already claimed run id."""

    code = "graph_invocation_run_id_reused"


class GraphInvocationClaimStore(Protocol):
    """Atomically claim one owner/thread/run identity for an invocation token."""

    def claim(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        run_id: str,
        invocation_kind: GraphInvocationKind,
        invocation_token: str,
    ) -> GraphInvocationClaimResult: ...


class InMemoryGraphInvocationClaimStore:
    """Lock-protected process-local claim store used by the in-memory graph host."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._claims: dict[tuple[str, str, str], str] = {}

    def claim(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        run_id: str,
        invocation_kind: GraphInvocationKind,
        invocation_token: str,
    ) -> GraphInvocationClaimResult:
        del invocation_kind  # Diagnostic label; uniqueness is owner/thread/run.
        if not all(
            isinstance(value, str) and value
            for value in (owner_digest, thread_id, run_id, invocation_token)
        ):
            raise ValueError("graph invocation claim fields must be non-empty strings")
        key = (owner_digest, thread_id, run_id)
        with self._lock:
            existing = self._claims.get(key)
            if existing is None:
                self._claims[key] = invocation_token
                return "claimed"
            if existing == invocation_token:
                return "same_invocation"
        raise GraphInvocationClaimConflict(
            "Graph invocation run_id is already owned by another invocation."
        )


__all__ = [
    "GraphInvocationClaimConflict",
    "GraphInvocationClaimResult",
    "GraphInvocationClaimStore",
    "GraphInvocationKind",
    "InMemoryGraphInvocationClaimStore",
]
