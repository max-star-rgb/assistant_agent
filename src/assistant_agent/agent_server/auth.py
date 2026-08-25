"""Tokenless Agent Server identity and resource-authorization entry point."""

from __future__ import annotations

from langgraph_sdk import Auth

from assistant_agent.agent_server.config import (
    ASSISTANT_EXECUTION_MODE_CONTEXT_KEY,
    ASSISTANT_GRAPH_ID,
    MEMORY_GRAPH_ID,
    PLANNING_ASSISTANT_ID,
    PLANNING_ASSISTANT_NAME,
)
from assistant_agent.agent_server.client import THREAD_GRAPH_METADATA_KEY
from assistant_agent.agent_server.attestation import (
    execution_attestation_digest,
    issue_evaluation_context_token,
    verify_evaluation_context_token,
)
from assistant_agent.agent_server.graph import get_native_assistant_execution_attestation


_EVALUATION_TOKEN_KEY = "coding_eval_context_token"


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
) -> bool | None:
    metadata = value.setdefault("metadata", {})
    requested_graph_id = value.get("graph_id") or metadata.get(
        THREAD_GRAPH_METADATA_KEY
    )
    graph_id = str(requested_graph_id or ASSISTANT_GRAPH_ID)
    if graph_id not in {ASSISTANT_GRAPH_ID, MEMORY_GRAPH_ID}:
        return False
    metadata["owner"] = str(ctx.user.identity)
    metadata[THREAD_GRAPH_METADATA_KEY] = graph_id
    eval_identity = metadata.get("coding_eval_identity")
    eval_repository = metadata.get("coding_eval_repo_id")
    eval_case = metadata.get("coding_eval_case_id")
    if any(value is not None for value in (eval_identity, eval_repository, eval_case)):
        if not all(
            isinstance(value, str) and bool(value) and len(value) <= 160
            for value in (eval_identity, eval_repository, eval_case)
        ) or eval_identity != str(ctx.user.identity):
            return False
        try:
            digest = execution_attestation_digest(
                get_native_assistant_execution_attestation()
            )
        except RuntimeError:
            return False
        metadata[_EVALUATION_TOKEN_KEY] = issue_evaluation_context_token(
            identity=eval_identity,
            repository_id=eval_repository,
            case_id=eval_case,
            attestation_digest=digest,
        )
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
    if not isinstance(metadata, dict) or THREAD_GRAPH_METADATA_KEY not in metadata:
        return {"owner": str(ctx.user.identity)}
    graph_id = str(metadata[THREAD_GRAPH_METADATA_KEY])
    if graph_id not in {ASSISTANT_GRAPH_ID, MEMORY_GRAPH_ID}:
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
) -> Auth.types.FilterType:
    metadata = value.setdefault("metadata", {})
    metadata["owner"] = str(ctx.user.identity)
    if str(value.get("assistant_id")) == MEMORY_GRAPH_ID:
        return {"owner": str(ctx.user.identity)}
    context = value.setdefault("context", {})
    token = context.pop(_EVALUATION_TOKEN_KEY, None)
    repository_id = context.pop("evaluation_repository_id", None)
    case_id = context.pop("evaluation_case_id", None)
    context.pop("evaluation_execution_attestation_digest", None)
    requested_evaluation = context.get("entry_profile") == "evaluation"
    trusted = False
    if requested_evaluation and isinstance(token, str) and isinstance(repository_id, str) and isinstance(case_id, str):
        try:
            digest = execution_attestation_digest(
                get_native_assistant_execution_attestation()
            )
        except RuntimeError:
            digest = ""
        trusted = verify_evaluation_context_token(
            token,
            identity=str(ctx.user.identity),
            repository_id=repository_id,
            case_id=case_id,
            attestation_digest=digest,
        )
        if trusted:
            context["entry_profile"] = "evaluation"
    if requested_evaluation and not trusted:
        context["entry_profile"] = "agent_server"
    return {
        "owner": str(ctx.user.identity),
        THREAD_GRAPH_METADATA_KEY: ASSISTANT_GRAPH_ID,
    }


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
