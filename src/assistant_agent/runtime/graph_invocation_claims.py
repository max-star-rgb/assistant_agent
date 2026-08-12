"""Process-local invocation claims for native assistant graph executions."""

from __future__ import annotations

import hashlib
import json
from threading import Lock
from typing import Literal, Protocol


GraphInvocationKind = Literal["invoke", "resume", "replay", "fork"]
GraphInvocationClaimResult = Literal["claimed", "same_invocation"]


class GraphInvocationClaimConflict(RuntimeError):
    """Raised when a different invocation reuses an already claimed run id."""

    code = "graph_invocation_run_id_reused"


class GraphInvocationClaimCapacityExceeded(RuntimeError):
    """Raised instead of evicting a claim whose checkpoint may still exist."""

    code = "graph_invocation_claim_capacity_exceeded"


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

    def delete_thread(self, *, owner_digest: str, thread_id: str) -> int: ...


class InMemoryGraphInvocationClaimStore:
    """Lock-protected process-local claim store used by the in-memory graph host."""

    def __init__(self, *, max_entries: int = 10_000) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise TypeError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._lock = Lock()
        self._max_entries = max_entries
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
                if len(self._claims) >= self._max_entries:
                    raise GraphInvocationClaimCapacityExceeded(
                        "Graph invocation claim capacity is exhausted; the owning "
                        "thread/checkpoint lifecycle must release retained claims."
                    )
                self._claims[key] = invocation_token
                return "claimed"
            if existing == invocation_token:
                return "same_invocation"
        raise GraphInvocationClaimConflict(
            "Graph invocation run_id is already owned by another invocation."
        )

    def delete_thread(self, *, owner_digest: str, thread_id: str) -> int:
        """Delete claims after the owning composition removes the whole thread."""

        _validate_claim_fields(owner_digest, thread_id)
        with self._lock:
            keys = [
                key
                for key in self._claims
                if key[0] == owner_digest and key[1] == thread_id
            ]
            for key in keys:
                del self._claims[key]
            return len(keys)


def graph_invocation_owner_digest(
    *,
    agent_id: str,
    user_id: str,
    session_id: str,
) -> str:
    """Return the stable opaque owner scope shared by app and graph gate."""

    _validate_claim_fields(agent_id, user_id, session_id)
    return hashlib.sha256(
        json.dumps(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "session_id": session_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def derive_child_invocation_token(
    *,
    parent_invocation_token: str,
    assignment_ref: str,
) -> str:
    """Derive a stable non-reversible child token scoped to one parent invocation."""

    _validate_claim_fields(parent_invocation_token, assignment_ref)
    payload = json.dumps(
        {
            "assignment_ref": assignment_ref,
            "parent_invocation_token": parent_invocation_token,
            "purpose": "assistant_graph_workflow_child_v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_claim_fields(*values: str) -> None:
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("graph invocation claim fields must be non-empty strings")


__all__ = [
    "GraphInvocationClaimCapacityExceeded",
    "GraphInvocationClaimConflict",
    "GraphInvocationClaimResult",
    "GraphInvocationClaimStore",
    "GraphInvocationKind",
    "InMemoryGraphInvocationClaimStore",
    "derive_child_invocation_token",
    "graph_invocation_owner_digest",
]
