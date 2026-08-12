"""Process-local invocation claims for native assistant graph executions."""

from __future__ import annotations

import hashlib
import json
from threading import Lock
from typing import Literal, Protocol


GraphInvocationKind = Literal["invoke", "resume", "replay", "fork"]
GraphInvocationClaimResult = Literal["claimed", "same_invocation"]
GraphInvocationPhase = Literal["pre_native", "native_started", "terminal"]


class GraphInvocationClaimConflict(RuntimeError):
    """Raised when a different invocation reuses an already claimed run id."""

    code = "graph_invocation_run_id_reused"


class GraphInvocationClaimCapacityExceeded(RuntimeError):
    """Raised instead of evicting a claim whose checkpoint may still exist."""

    code = "graph_invocation_claim_capacity_exceeded"


class GraphInvocationThreadActive(RuntimeError):
    """Raised when retention tries to delete a thread with native work active."""

    code = "graph_thread_active"


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

    def begin_native(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        run_id: str,
        invocation_token: str,
    ) -> None: ...

    def assert_owned(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        run_id: str,
        invocation_token: str,
    ) -> None: ...

    def mark_terminal(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        run_id: str,
        invocation_token: str,
    ) -> None: ...

    def begin_thread_delete(self, *, owner_digest: str, thread_id: str) -> None: ...

    def finish_thread_delete(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        commit: bool,
    ) -> int: ...

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
        self._claims: dict[
            tuple[str, str, str], tuple[str, GraphInvocationPhase]
        ] = {}
        self._deleting_threads: set[tuple[str, str]] = set()

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
            if (owner_digest, thread_id) in self._deleting_threads:
                raise GraphInvocationClaimConflict(
                    "Graph invocation thread is being deleted by its retention owner."
                )
            existing = self._claims.get(key)
            if existing is None:
                if len(self._claims) >= self._max_entries:
                    raise GraphInvocationClaimCapacityExceeded(
                        "Graph invocation claim capacity is exhausted; the owning "
                        "thread/checkpoint lifecycle must release retained claims."
                    )
                self._claims[key] = (invocation_token, "pre_native")
                return "claimed"
            if existing == (invocation_token, "pre_native"):
                return "same_invocation"
        raise GraphInvocationClaimConflict(
            "Graph invocation run_id is already owned by another invocation."
        )

    def begin_native(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        run_id: str,
        invocation_token: str,
    ) -> None:
        """Atomically admit exactly one native start for the claimed invocation."""

        _validate_claim_fields(owner_digest, thread_id, run_id, invocation_token)
        key = (owner_digest, thread_id, run_id)
        with self._lock:
            if (owner_digest, thread_id) in self._deleting_threads:
                raise GraphInvocationClaimConflict(
                    "Graph invocation thread is being deleted by its retention owner."
                )
            existing = self._claims.get(key)
            if existing == (invocation_token, "pre_native"):
                self._claims[key] = (invocation_token, "native_started")
                return
        raise GraphInvocationClaimConflict(
            "Graph invocation has already started or is owned by another token."
        )

    def assert_owned(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        run_id: str,
        invocation_token: str,
    ) -> None:
        """Verify an in-graph gate still belongs to the active invocation token."""

        _validate_claim_fields(owner_digest, thread_id, run_id, invocation_token)
        key = (owner_digest, thread_id, run_id)
        with self._lock:
            if (owner_digest, thread_id) in self._deleting_threads:
                raise GraphInvocationClaimConflict(
                    "Graph invocation thread is being deleted by its retention owner."
                )
            existing = self._claims.get(key)
            if existing is None:
                if len(self._claims) >= self._max_entries:
                    raise GraphInvocationClaimCapacityExceeded(
                        "Graph invocation claim capacity is exhausted; the owning "
                        "thread/checkpoint lifecycle must release retained claims."
                    )
                self._claims[key] = (invocation_token, "native_started")
                return
            if existing is not None and existing[0] == invocation_token:
                return
        raise GraphInvocationClaimConflict(
            "Graph invocation run_id is owned by another invocation."
        )

    def mark_terminal(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        run_id: str,
        invocation_token: str,
    ) -> None:
        """Record a terminal phase without releasing the retained claim."""

        _validate_claim_fields(owner_digest, thread_id, run_id, invocation_token)
        key = (owner_digest, thread_id, run_id)
        with self._lock:
            if self._claims.get(key) == (invocation_token, "native_started"):
                self._claims[key] = (invocation_token, "terminal")
                return
        raise GraphInvocationClaimConflict(
            "Graph invocation cannot enter terminal from its current claim phase."
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

    def begin_thread_delete(self, *, owner_digest: str, thread_id: str) -> None:
        """Freeze one thread before its external checkpoint deletion begins."""

        _validate_claim_fields(owner_digest, thread_id)
        key = (owner_digest, thread_id)
        with self._lock:
            if key in self._deleting_threads:
                raise GraphInvocationClaimConflict(
                    "Graph invocation thread deletion is already in progress."
                )
            if any(
                claim_key[0] == owner_digest
                and claim_key[1] == thread_id
                and phase == "native_started"
                for claim_key, (_token, phase) in self._claims.items()
            ):
                raise GraphInvocationThreadActive(
                    "Graph invocation thread still has native execution in progress."
                )
            self._deleting_threads.add(key)

    def finish_thread_delete(
        self,
        *,
        owner_digest: str,
        thread_id: str,
        commit: bool,
    ) -> int:
        """Commit claim deletion or unfreeze after checkpoint deletion failure."""

        _validate_claim_fields(owner_digest, thread_id)
        if not isinstance(commit, bool):
            raise TypeError("commit must be a boolean")
        thread_key = (owner_digest, thread_id)
        with self._lock:
            if thread_key not in self._deleting_threads:
                raise GraphInvocationClaimConflict(
                    "Graph invocation thread deletion is not in progress."
                )
            deleted = 0
            if commit:
                keys = [
                    key
                    for key in self._claims
                    if key[0] == owner_digest and key[1] == thread_id
                ]
                for key in keys:
                    del self._claims[key]
                deleted = len(keys)
            self._deleting_threads.remove(thread_key)
            return deleted


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
    "GraphInvocationPhase",
    "GraphInvocationClaimResult",
    "GraphInvocationClaimStore",
    "GraphInvocationKind",
    "GraphInvocationThreadActive",
    "InMemoryGraphInvocationClaimStore",
    "derive_child_invocation_token",
    "graph_invocation_owner_digest",
]
