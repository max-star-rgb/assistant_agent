"""Tokenless Agent Server identity and resource-authorization entry point."""

from __future__ import annotations

from langgraph_sdk import Auth

from assistant_agent.agent_server.config import (
    ASSISTANT_EXECUTION_MODE_CONTEXT_KEY,
    ASSISTANT_GRAPH_ID,
    PLANNING_ASSISTANT_ID,
    PLANNING_ASSISTANT_NAME,
)


auth = Auth()


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
    return {
        "identity": identity or "local-developer",
        "permissions": ["assistant:developer"],
        "is_authenticated": True,
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
async def authorize_planning_assistant_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.create.value,
) -> bool | None:
    """Allow only the repository-owned planning preset assistant definition."""

    del ctx
    if str(value.get("assistant_id")) != PLANNING_ASSISTANT_ID:
        return False
    value["graph_id"] = ASSISTANT_GRAPH_ID
    value["name"] = PLANNING_ASSISTANT_NAME
    value["config"] = {}
    value["context"] = {ASSISTANT_EXECUTION_MODE_CONTEXT_KEY: "planning"}
    value["metadata"] = {
        "assistant_agent_preset": "planning",
        "managed_by": "assistant_agent",
    }
    value["if_exists"] = "do_nothing"
    return None


@auth.on.assistants.update
async def authorize_planning_assistant_update(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.update.value,
) -> bool | None:
    """Keep updates confined to the repository-owned planning preset."""

    del ctx
    if str(value.get("assistant_id")) != PLANNING_ASSISTANT_ID:
        return False
    value["graph_id"] = ASSISTANT_GRAPH_ID
    value["name"] = PLANNING_ASSISTANT_NAME
    value["config"] = {}
    value["context"] = {ASSISTANT_EXECUTION_MODE_CONTEXT_KEY: "planning"}
    value["metadata"] = {
        "assistant_agent_preset": "planning",
        "managed_by": "assistant_agent",
    }
    return None


@auth.on.threads.create
async def authorize_thread_create(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create.value,
) -> None:
    metadata = value.setdefault("metadata", {})
    metadata["owner"] = str(ctx.user.identity)


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
) -> Auth.types.FilterType:
    _ = value
    return {"owner": str(ctx.user.identity)}


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
) -> Auth.types.FilterType:
    metadata = value.setdefault("metadata", {})
    metadata["owner"] = str(ctx.user.identity)
    return {"owner": str(ctx.user.identity)}


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
    "authorize_planning_assistant_create",
    "authorize_planning_assistant_update",
    "authorize_run_create",
    "authorize_thread_create",
    "authorize_thread_delete",
    "authorize_thread_read",
    "authorize_thread_search",
    "authorize_thread_update",
    "deny_all",
    "scope_store",
]
