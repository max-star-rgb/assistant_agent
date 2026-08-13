"""Optional LangMem backend using LangGraph's native ``BaseStore`` resource."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from langgraph.store.base import BaseStore

from assistant_agent.memory.commit_ledger import (
    MemoryCommitLedger,
    MemoryCommitRequest,
    memory_commit_input_digest,
    stable_memory_event_id,
)
from assistant_agent.memory.node_bundle import MemoryNodeBundle
from assistant_agent.runtime.assistant_graph_state import (
    AssistantStateCompatibilityError,
    MemoryCommitState,
    MemoryContext,
    MemoryContextItem,
    ResponsePublishState,
    validate_assistant_turn_state,
)


_COMMIT_SCHEMA_VERSION = "langmem_messages_v1"


class LangMemConfigurationError(RuntimeError):
    """Explicit LangMem configuration cannot be constructed safely."""


class LangMemManager(Protocol):
    def invoke(self, value: Any, *, config: Mapping[str, Any]) -> Any: ...


def create_langmem_memory_bundle(
    *,
    model: Any,
    store: BaseStore,
    ledger: MemoryCommitLedger,
    aclose: Callable[[], Any] | None = None,
) -> MemoryNodeBundle:
    """Lazily import LangMem and construct its Store-backed manager."""

    try:
        langmem = importlib.import_module("langmem")
        create_manager = getattr(langmem, "create_memory_store_manager")
    except (ImportError, AttributeError) as exc:
        raise LangMemConfigurationError(
            "LangMem optional dependency is required for backend 'langmem'."
        ) from exc
    try:
        manager = create_manager(
            model,
            namespace=("assistant_agent", "{langgraph_user_id}"),
            store=store,
        )
    except Exception as exc:
        raise LangMemConfigurationError(
            "LangMem manager configuration is invalid."
        ) from exc
    return build_langmem_memory_bundle(
        manager=manager,
        store=store,
        ledger=ledger,
        aclose=aclose,
    )


def build_langmem_memory_bundle(
    *,
    manager: LangMemManager,
    store: BaseStore,
    ledger: MemoryCommitLedger,
    aclose: Callable[[], Any] | None = None,
) -> MemoryNodeBundle:
    """Bind LangMem semantics to fixed graph recall and commit nodes."""

    def recall_node(state: Any, runtime: Any) -> Any:
        validated = validate_assistant_turn_state(state)
        invocation_kind, refresh_memory = _invocation_policy(validated, runtime)
        if validated.get("memory_context") is not None and not refresh_memory:
            return validated
        if invocation_kind in {"resume", "replay", "fork"} and not refresh_memory:
            raise AssistantStateCompatibilityError(
                "Continuation checkpoint has no frozen memory_context."
            )
        runtime_store = _runtime_store(runtime, expected=store)
        request = validated["request"]
        namespace = langmem_namespace(
            user_id=str(request["user_id"]),
            agent_id=str(validated["run"]["agent_id"]),
        )
        try:
            raw_items = runtime_store.search(
                namespace,
                query=_current_user_text(request) or None,
                limit=32,
            )
        except Exception:
            return _with_memory_context(
                validated,
                status="degraded",
                items=(),
                issue_codes=("langmem_recall_failed",),
            )
        items = _normalize_store_items(raw_items)
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
        _runtime_store(runtime, expected=store)
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
            backend_id="langmem",
            turn_origin_id=validated["turn_origin_id"],
            input_digest=input_digest,
            schema_version=_COMMIT_SCHEMA_VERSION,
        )
        reservation = ledger.reserve(
            MemoryCommitRequest(
                memory_event_id=event_id,
                backend_id="langmem",
                turn_origin_id=validated["turn_origin_id"],
                input_schema_version=_COMMIT_SCHEMA_VERSION,
                input_digest=input_digest,
            )
        )
        if reservation.disposition == "succeeded":
            return _with_commit(validated, status="succeeded", event_id=event_id)
        if reservation.disposition == "failed":
            return _with_commit(
                validated,
                status="failed",
                event_id=event_id,
                issue_code="langmem_commit_failed",
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
        opaque_user_id = langmem_namespace(
            user_id=str(validated["request"]["user_id"]),
            agent_id=str(validated["run"]["agent_id"]),
        )[-1]
        try:
            manager.invoke(
                {
                    "messages": [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": assistant_text},
                    ]
                },
                config={"configurable": {"langgraph_user_id": opaque_user_id}},
            )
        except TimeoutError:
            ledger.outcome_unknown(
                event_id,
                owner_token=owner_token,
                outcome_code="timed_out",
            )
            return _with_commit(
                validated,
                status="timed_out",
                event_id=event_id,
                issue_code="langmem_commit_timed_out",
            )
        except Exception:
            ledger.fail(
                event_id,
                owner_token=owner_token,
                outcome_code="manager_error",
            )
            return _with_commit(
                validated,
                status="failed",
                event_id=event_id,
                issue_code="langmem_commit_failed",
            )
        except BaseException:
            ledger.outcome_unknown(
                event_id,
                owner_token=owner_token,
                outcome_code="interrupted",
            )
            raise
        ledger.succeed(
            event_id,
            owner_token=owner_token,
            outcome_code="accepted",
        )
        return _with_commit(validated, status="succeeded", event_id=event_id)

    async def close_resources() -> None:
        if aclose is None:
            return
        result = aclose()
        if hasattr(result, "__await__"):
            await result

    return MemoryNodeBundle(
        backend_id="langmem",
        recall_node=recall_node,
        commit_node=commit_node,
        store=store,
        aclose=close_resources if aclose is not None else None,
    )


def langmem_namespace(*, user_id: str, agent_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"assistant_agent:langmem:user\0{user_id}\0agent\0{agent_id}".encode("utf-8")
    ).hexdigest()[:40]
    return ("assistant_agent", f"memory_subject_{digest}")


def _runtime_store(runtime: Any, *, expected: BaseStore) -> BaseStore:
    runtime_store = getattr(runtime, "store", None)
    if runtime_store is None or runtime_store is not expected:
        raise LangMemConfigurationError(
            "LangMem node requires its compiled runtime.store resource."
        )
    return runtime_store


def _normalize_store_items(raw_items: Any) -> tuple[MemoryContextItem, ...]:
    items: list[MemoryContextItem] = []
    total_chars = 0
    for raw in raw_items or ():
        text = _memory_text(getattr(raw, "value", None)).strip()[:4_000]
        key = str(getattr(raw, "key", "")).strip()[:512]
        if not text or not key or len(items) >= 32:
            continue
        remaining = 12_000 - total_chars
        if remaining <= 0:
            break
        text = text[:remaining]
        score = getattr(raw, "score", None)
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            score = None
        items.append(
            MemoryContextItem(
                memory_id=key,
                text=text,
                source="langmem",
                relevance=(
                    max(0.0, min(1.0, float(score))) if score is not None else None
                ),
                updated_at=_datetime(getattr(raw, "updated_at", None)),
            )
        )
        total_chars += len(text)
    return tuple(items)


def _memory_text(value: Any) -> str:
    current = value
    for _ in range(3):
        if isinstance(current, str):
            return current
        if not isinstance(current, Mapping):
            break
        if "content" in current:
            current = current["content"]
            continue
        if "memory" in current:
            current = current["memory"]
            continue
        break
    if isinstance(current, Mapping):
        return json.dumps(
            current,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return str(current) if current is not None else ""


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _invocation_policy(state: Any, runtime: Any) -> tuple[str, bool]:
    context = getattr(runtime, "context", None)
    invocation_kind = str(
        getattr(context, "invocation_kind", state.get("invocation_kind", "invoke"))
    )
    refresh_memory = bool(getattr(context, "refresh_memory", False))
    return invocation_kind, invocation_kind == "fork" and refresh_memory


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
    updated = dict(state)
    updated["memory_context"] = MemoryContext(
        backend_id="langmem",
        status=status,
        snapshot_id="langmem:"
        + hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest(),
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


__all__ = [
    "LangMemConfigurationError",
    "LangMemManager",
    "build_langmem_memory_bundle",
    "create_langmem_memory_bundle",
    "langmem_namespace",
]
