"""Tokenless Agent Server identity and resource-authorization entry point."""

from __future__ import annotations

from collections.abc import Mapping
import hmac
import secrets
from uuid import UUID, uuid5

from langgraph_sdk import Auth
from langgraph_sdk.auth import is_studio_user

from assistant_agent.agent_server.config import (
    ASSISTANT_GRAPH_ID,
    MEMORY_GRAPH_ID,
    WORKER_GRAPH_ID,
)
from assistant_agent.agent_server.client import THREAD_GRAPH_METADATA_KEY
from assistant_agent.native_agent.context import (
    ASSISTANT_RUNTIME_METADATA_KEY,
    AssistantRuntimeFacts,
)

auth = Auth()

_INTERNAL_WORKER_HEADER = "X-Assistant-Internal-Worker"
_INTERNAL_WORKER_PERMISSION = "assistant:internal-worker"
# ponytail: process-local capability fits the current single-process server; use a
# shared secret or service identity before enabling multi-process workers.
_INTERNAL_WORKER_CAPABILITY = secrets.token_urlsafe(32)


@auth.on
async def deny_all(ctx: Auth.types.AuthContext, value: object) -> bool:
    _ = ctx, value
    return False


@auth.authenticate
async def authenticate(
    authorization: str | None,
    headers: dict[bytes, bytes],
) -> Auth.types.MinimalUserDict:
    del authorization
    identity = _header_text(headers, b"x-assistant-user")
    permissions = ["assistant:developer"]
    worker_capability = _header_text(
        headers,
        _INTERNAL_WORKER_HEADER.lower().encode(),
    )
    if worker_capability is not None and hmac.compare_digest(
        worker_capability.encode(),
        _INTERNAL_WORKER_CAPABILITY.encode(),
    ):
        permissions.append(_INTERNAL_WORKER_PERMISSION)
    return {
        "identity": identity or "local-developer",
        "permissions": permissions,
        "is_authenticated": True,
    }


def _internal_worker_headers(identity: str) -> dict[str, str]:
    return {
        "X-Assistant-User": identity,
        _INTERNAL_WORKER_HEADER: _INTERNAL_WORKER_CAPABILITY,
    }


def _header_text(headers: dict[bytes, bytes], name: bytes) -> str | None:
    raw = headers.get(name)
    if raw is None:
        return None
    value = raw.decode("utf-8", errors="strict").strip()
    return value or None


@auth.on.assistants.read
async def allow_assistant_read(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.read.value,
) -> bool:
    _ = ctx, value
    return True


@auth.on.assistants.search
async def allow_assistant_search(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.search.value,
) -> bool:
    _ = ctx, value
    return True


@auth.on.assistants.create
async def authorize_assistant_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.create.value,
) -> bool | None:
    """Allow the native Assistant payload and scope API-created resources."""

    if str(value.get("graph_id")) != ASSISTANT_GRAPH_ID:
        return False
    if is_studio_user(ctx.user):
        return True
    owner = str(ctx.user.identity)
    metadata = value.setdefault("metadata", {})
    metadata.update({"owner": owner, "managed_by": owner})
    return None


@auth.on.assistants.update
async def authorize_assistant_update(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.update.value,
) -> bool | None:
    """Let Agent Server version the payload and enforce API ownership."""

    graph_id = value.get("graph_id")
    if graph_id is not None and str(graph_id) != ASSISTANT_GRAPH_ID:
        return False
    if is_studio_user(ctx.user):
        return True
    owner = str(ctx.user.identity)
    metadata = value.setdefault("metadata", {})
    metadata.update({"owner": owner, "managed_by": owner})
    return {"owner": owner}


@auth.on.assistants.delete
async def authorize_assistant_delete(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.delete.value,
) -> Auth.types.FilterType | bool:
    """Scope custom Assistant deletion to its owner."""

    if is_studio_user(ctx.user):
        return True
    return {"owner": str(ctx.user.identity)}


@auth.on.threads.create
async def authorize_thread_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create.value,
) -> bool | None:
    metadata = value.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return False
    requested_graph_id = value.get("graph_id") or metadata.get(
        THREAD_GRAPH_METADATA_KEY
    )
    graph_id = str(requested_graph_id or ASSISTANT_GRAPH_ID)
    if graph_id not in {ASSISTANT_GRAPH_ID, MEMORY_GRAPH_ID, WORKER_GRAPH_ID}:
        return False
    if graph_id == WORKER_GRAPH_ID:
        if not _is_internal_worker(ctx) or not _authorized_worker_metadata(metadata):
            return False
    elif _is_internal_worker(ctx) or not _authorized_non_worker_metadata(metadata):
        return False
    metadata["owner"] = str(ctx.user.identity)
    metadata[THREAD_GRAPH_METADATA_KEY] = graph_id
    return None


@auth.on.threads.read
async def authorize_thread_read(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.read.value,
) -> Auth.types.FilterType:
    _ = value
    return {"owner": str(ctx.user.identity)}


@auth.on.threads.update
async def authorize_thread_update(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.update.value,
) -> Auth.types.FilterType | bool:
    if value.get("action") in {"interrupt", "rollback"}:
        return {"owner": str(ctx.user.identity)}
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return {"owner": str(ctx.user.identity)}
    if ASSISTANT_RUNTIME_METADATA_KEY in metadata:
        return False
    if THREAD_GRAPH_METADATA_KEY not in metadata:
        return {"owner": str(ctx.user.identity)}
    graph_id = str(metadata[THREAD_GRAPH_METADATA_KEY])
    if graph_id not in {ASSISTANT_GRAPH_ID, MEMORY_GRAPH_ID, WORKER_GRAPH_ID}:
        return False
    return {"owner": str(ctx.user.identity), THREAD_GRAPH_METADATA_KEY: graph_id}


@auth.on.threads.delete
async def authorize_thread_delete(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.delete.value,
) -> Auth.types.FilterType:
    _ = value
    return {"owner": str(ctx.user.identity)}


@auth.on.threads.search
async def authorize_thread_search(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.search.value,
) -> Auth.types.FilterType:
    _ = value
    return {"owner": str(ctx.user.identity)}


@auth.on.threads.create_run
async def authorize_run_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create_run.value,
) -> Auth.types.FilterType | bool:
    metadata = value.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return False
    assistant_id = value.get("assistant_id")
    if not isinstance(assistant_id, UUID):
        return False
    graph_id = _run_graph_id(assistant_id)
    if graph_id is None:
        return False
    if graph_id == MEMORY_GRAPH_ID:
        if _is_internal_worker(ctx) or not _authorized_non_worker_metadata(metadata):
            return False
        metadata["owner"] = str(ctx.user.identity)
        return {
            "owner": str(ctx.user.identity),
            THREAD_GRAPH_METADATA_KEY: MEMORY_GRAPH_ID,
        }
    if graph_id == WORKER_GRAPH_ID:
        if not _is_internal_worker(ctx) or not _authorized_worker_metadata(metadata):
            return False
        metadata["owner"] = str(ctx.user.identity)
        return {
            "owner": str(ctx.user.identity),
            THREAD_GRAPH_METADATA_KEY: WORKER_GRAPH_ID,
        }
    context = value.setdefault("context", {})
    if not isinstance(context, dict):
        return False
    if _is_internal_worker(ctx) or not _authorized_non_worker_metadata(metadata):
        return False
    metadata["owner"] = str(ctx.user.identity)
    return {
        "owner": str(ctx.user.identity),
        THREAD_GRAPH_METADATA_KEY: ASSISTANT_GRAPH_ID,
    }


def _run_graph_id(assistant_id: UUID) -> str | None:
    from langgraph_api.graph import NAMESPACE_GRAPH  # noqa: PLC0415

    if assistant_id in {
        uuid5(NAMESPACE_GRAPH, "assistant-native-v1"),
        uuid5(NAMESPACE_GRAPH, "assistant-native-v2"),
        uuid5(NAMESPACE_GRAPH, "assistant-native-v3"),
        uuid5(NAMESPACE_GRAPH, "assistant-worker-v1"),
    }:
        return None
    return {
        uuid5(NAMESPACE_GRAPH, ASSISTANT_GRAPH_ID): ASSISTANT_GRAPH_ID,
        uuid5(NAMESPACE_GRAPH, MEMORY_GRAPH_ID): MEMORY_GRAPH_ID,
        uuid5(NAMESPACE_GRAPH, WORKER_GRAPH_ID): WORKER_GRAPH_ID,
    }.get(assistant_id, ASSISTANT_GRAPH_ID)


def _authorized_worker_metadata(metadata: Mapping[str, object]) -> bool:
    payload = metadata.get(ASSISTANT_RUNTIME_METADATA_KEY)
    if not isinstance(payload, Mapping):
        return False
    try:
        facts = AssistantRuntimeFacts.model_validate(dict(payload))
    except ValueError:
        return False
    return (
        facts.entry_profile == "async_worker"
        and facts.repository_snapshot_sha is not None
    )


def _authorized_non_worker_metadata(metadata: Mapping[str, object]) -> bool:
    if ASSISTANT_RUNTIME_METADATA_KEY not in metadata:
        return True
    payload = metadata.get(ASSISTANT_RUNTIME_METADATA_KEY)
    if not isinstance(payload, Mapping):
        return False
    try:
        facts = AssistantRuntimeFacts.model_validate(dict(payload))
    except ValueError:
        return False
    return (
        facts.entry_profile != "async_worker"
        and facts.repository_snapshot_sha is None
    )


def _is_internal_worker(ctx: Auth.types.AuthContext) -> bool:
    permissions = getattr(ctx.user, "permissions", ()) or ()
    return _INTERNAL_WORKER_PERMISSION in permissions


@auth.on.store
async def scope_store(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.store.value,
) -> None:
    identity = str(ctx.user.identity)
    namespace = tuple(value.get("namespace") or ())
    if not namespace or namespace[0] != identity:
        value["namespace"] = (identity, *namespace)


__all__ = [
    "auth",
    "allow_assistant_read",
    "allow_assistant_search",
    "authenticate",
    "authorize_assistant_create",
    "authorize_assistant_delete",
    "authorize_assistant_update",
    "authorize_run_create",
    "authorize_thread_create",
    "authorize_thread_delete",
    "authorize_thread_read",
    "authorize_thread_search",
    "authorize_thread_update",
    "deny_all",
    "scope_store",
]
