"""Direct Mem0 graph nodes; Mem0 remains a memory engine, not a BaseStore."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.commit_ledger import (
    MemoryCommitLedger,
    MemoryCommitRequest,
    SQLiteMemoryCommitLedger,
    memory_commit_input_digest,
    stable_memory_event_id,
)
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.mem0.models import (
    Mem0CompletedTurn,
    Mem0Identity,
    Mem0IngestionResult,
    Mem0RecallMemory,
)
from assistant_agent.memory.node_bundle import MemoryNodeBundle
from assistant_agent.memory.node_observability import observe_memory_node
from assistant_agent.runtime.assistant_graph_state import (
    MemoryCommitState,
    MemoryContext,
    MemoryContextItem,
    ResponsePublishState,
    validate_assistant_turn_state,
)


_COMMIT_SCHEMA_VERSION = "completed_turn_v1"


class Mem0NodeClient(Protocol):
    def recall_long_term_memory(
        self, identity: Mem0Identity
    ) -> list[Mem0RecallMemory]: ...

    def ingest_completed_turn(self, turn: Mem0CompletedTurn) -> Mem0IngestionResult: ...


def build_mem0_memory_bundle(
    *,
    client: Mem0NodeClient,
    ledger: MemoryCommitLedger | None = None,
    identity_namespace: str,
) -> MemoryNodeBundle:
    """Bind one client and ledger directly into synchronous graph nodes."""

    resolved_ledger = ledger or SQLiteMemoryCommitLedger(
        Path(".local") / "langgraph" / "memory_commits.sqlite3"
    )

    def recall_node(state: Any, runtime: Any) -> Any:
        validated = validate_assistant_turn_state(state)
        invocation_kind, refresh_memory = _invocation_policy(validated, runtime)
        if validated.get("memory_context") is not None and not refresh_memory:
            return validated
        if invocation_kind in {"resume", "replay", "fork"} and not refresh_memory:
            from assistant_agent.runtime.assistant_graph_state import (
                AssistantStateCompatibilityError,
            )

            raise AssistantStateCompatibilityError(
                "Continuation checkpoint has no frozen memory_context."
            )
        try:
            memories = client.recall_long_term_memory(
                bind_mem0_identity(_identity(validated), namespace=identity_namespace)
            )
        except Exception:
            return _with_memory_context(
                validated,
                status="degraded",
                items=(),
                issue_codes=("mem0_recall_failed",),
            )
        items = _normalize_memories(memories)
        return _with_memory_context(
            validated,
            status="ready" if items else "empty",
            items=items,
        )

    def commit_node(state: Any, runtime: Any) -> Any:
        validated = validate_assistant_turn_state(state)
        existing = MemoryCommitState.model_validate(validated["memory_commit"])
        if existing.status != "not_requested":
            return validated
        invocation_kind, _ = _invocation_policy(validated, runtime)
        if (
            invocation_kind in {"replay", "fork"}
            or validated["turn_provenance"] == "time_travel"
        ):
            return _with_commit(
                validated,
                status="skipped",
                issue_code="time_travel_commit_disabled",
            )
        published = ResponsePublishState.model_validate(validated["response_publish"])
        response = validated.get("final_response")
        user_text = _current_user_text(validated["request"])
        if published.status != "published":
            return _with_commit(
                validated, status="skipped", issue_code="response_not_published"
            )
        if response is None or not user_text or not str(response["message"]).strip():
            return _with_commit(
                validated, status="skipped", issue_code="memory_commit_input_empty"
            )
        assistant_text = str(response["message"]).strip()
        input_digest = memory_commit_input_digest(
            user_text=user_text,
            assistant_text=assistant_text,
            schema_version=_COMMIT_SCHEMA_VERSION,
        )
        event_id = stable_memory_event_id(
            backend_id="mem0",
            turn_origin_id=validated["turn_origin_id"],
            input_digest=input_digest,
            schema_version=_COMMIT_SCHEMA_VERSION,
        )
        request = MemoryCommitRequest(
            memory_event_id=event_id,
            backend_id="mem0",
            turn_origin_id=validated["turn_origin_id"],
            input_schema_version=_COMMIT_SCHEMA_VERSION,
            input_digest=input_digest,
        )
        reservation = resolved_ledger.reserve(request)
        if reservation.disposition == "succeeded":
            return _with_commit(validated, status="succeeded", event_id=event_id)
        if reservation.disposition == "failed":
            return _with_commit(
                validated,
                status="failed",
                event_id=event_id,
                issue_code="mem0_commit_failed",
            )
        if reservation.disposition == "in_progress":
            return _with_commit(
                validated,
                status="skipped",
                event_id=event_id,
                issue_code="memory_commit_in_progress",
            )
        if reservation.disposition == "outcome_unknown":
            return _with_commit(
                validated,
                status="skipped",
                event_id=event_id,
                issue_code="memory_commit_outcome_unknown",
            )
        owner_token = reservation.owner_token
        if owner_token is None:  # pragma: no cover - ledger contract violation.
            raise RuntimeError("memory commit reservation owner is missing")
        native_identity = bind_mem0_identity(
            _identity(validated), namespace=identity_namespace
        )
        try:
            result = client.ingest_completed_turn(
                Mem0CompletedTurn(
                    identity=native_identity,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    occurred_at=datetime.now(timezone.utc),
                    source_turn=event_id,
                )
            )
        except TimeoutError:
            resolved_ledger.outcome_unknown(
                event_id,
                owner_token=owner_token,
                outcome_code="timed_out",
            )
            return _with_commit(
                validated,
                status="timed_out",
                event_id=event_id,
                issue_code="mem0_commit_timed_out",
            )
        except Exception:
            resolved_ledger.fail(
                event_id,
                owner_token=owner_token,
                outcome_code="client_error",
            )
            return _with_commit(
                validated,
                status="failed",
                event_id=event_id,
                issue_code="mem0_commit_failed",
            )
        except BaseException:
            resolved_ledger.outcome_unknown(
                event_id,
                owner_token=owner_token,
                outcome_code="interrupted",
            )
            raise
        if result.accepted:
            resolved_ledger.succeed(
                event_id,
                owner_token=owner_token,
                outcome_code="accepted",
            )
            return _with_commit(validated, status="succeeded", event_id=event_id)
        resolved_ledger.fail(
            event_id,
            owner_token=owner_token,
            outcome_code="rejected",
        )
        return _with_commit(
            validated,
            status="failed",
            event_id=event_id,
            issue_code="mem0_commit_failed",
        )

    async def aclose() -> None:
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is None:
            return
        result = closer()
        if inspect.isawaitable(result):
            await result

    return MemoryNodeBundle(
        backend_id="mem0",
        recall_node=observe_memory_node(
            recall_node, backend_id="mem0", phase="recall"
        ),
        commit_node=observe_memory_node(
            commit_node, backend_id="mem0", phase="commit"
        ),
        store=None,
        aclose=aclose,
    )


def _identity(state: Any) -> RequestIdentity:
    request = state["request"]
    run = state["run"]
    return RequestIdentity.for_user(
        user_id=str(request["user_id"]),
        agent_id=str(run["agent_id"]),
        session_id=str(request["session_id"]),
    )


def _invocation_policy(state: Any, runtime: Any) -> tuple[str, bool]:
    context = getattr(runtime, "context", None)
    invocation_kind = str(
        getattr(context, "invocation_kind", state.get("invocation_kind", "invoke"))
    )
    refresh_memory = bool(getattr(context, "refresh_memory", False))
    return invocation_kind, invocation_kind == "fork" and refresh_memory


def _normalize_memories(
    memories: Sequence[Mem0RecallMemory],
) -> tuple[MemoryContextItem, ...]:
    ordered = sorted(
        memories,
        key=lambda memory: (
            -(memory.relevance if memory.relevance is not None else -1.0),
            -_timestamp(memory.created_at),
            memory.memory_id,
        ),
    )
    items: list[MemoryContextItem] = []
    total_chars = 0
    for memory in ordered:
        text = memory.text.strip()[:4_000]
        if not text or len(items) >= 32:
            continue
        remaining = 12_000 - total_chars
        if remaining <= 0:
            break
        text = text[:remaining]
        items.append(
            MemoryContextItem(
                memory_id=memory.memory_id[:512],
                text=text,
                source="mem0",
                relevance=memory.relevance,
                updated_at=memory.created_at,
            )
        )
        total_chars += len(text)
    return tuple(items)


def _timestamp(value: datetime) -> float:
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _with_memory_context(
    state: Any,
    *,
    status: str,
    items: tuple[MemoryContextItem, ...],
    issue_codes: tuple[str, ...] = (),
) -> Any:
    snapshot_payload = "\0".join(
        (
            state["turn_origin_id"],
            status,
            *(f"{item.memory_id}:{item.text}" for item in items),
        )
    )
    snapshot_id = "mem0:" + hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest()
    updated = dict(state)
    updated["memory_context"] = MemoryContext(
        backend_id="mem0",
        status=status,
        snapshot_id=snapshot_id,
        items=items,
        issue_codes=issue_codes,
    ).model_dump(mode="json")
    return validate_assistant_turn_state(updated)


def _with_commit(
    state: Any,
    *,
    status: str,
    event_id: str | None = None,
    issue_code: str | None = None,
) -> Any:
    updated = dict(state)
    updated["memory_commit"] = MemoryCommitState(
        status=status,
        memory_event_id=event_id,
        issue_code=issue_code,
    ).model_dump(mode="json")
    return validate_assistant_turn_state(updated)


def _current_user_text(request: Any) -> str:
    text = request.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    for message in reversed(request.get("messages", ())):
        if message.get("role") == "user" and str(message.get("text", "")).strip():
            return str(message["text"]).strip()
    return ""


__all__ = ["Mem0NodeClient", "build_mem0_memory_bundle"]
