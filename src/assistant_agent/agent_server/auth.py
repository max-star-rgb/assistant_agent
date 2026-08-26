"""Tokenless Agent Server identity and resource-authorization entry point."""

from __future__ import annotations

from langgraph_sdk import Auth
from langgraph_sdk.auth import is_studio_user

from assistant_agent.agent_server.config import (
    ASSISTANT_GRAPH_ID,
    MEMORY_GRAPH_ID,
)
from assistant_agent.agent_server.client import THREAD_GRAPH_METADATA_KEY
from assistant_agent.agent_server.attestation import (
    execution_attestation_digest,
    issue_evaluation_context_token,
    verify_evaluation_context_token,
)
from assistant_agent.agent_server.graph import get_native_assistant_execution_attestation
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    AssistantRuntimeFacts,
    assistant_runtime_metadata,
)


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
) -> Auth.types.FilterType | bool:
    metadata = value.setdefault("metadata", {})
    metadata["owner"] = str(ctx.user.identity)
    if str(value.get("assistant_id")) == MEMORY_GRAPH_ID:
        return {"owner": str(ctx.user.identity)}
    context = value.setdefault("context", {})
    if not isinstance(context, dict):
        return False
    evaluation_fields = {
        _EVALUATION_TOKEN_KEY,
        "evaluation_repository_id",
        "evaluation_case_id",
        "evaluation_execution_attestation_digest",
    }
    requested_evaluation = bool(evaluation_fields.intersection(metadata))
    if requested_evaluation:
        if not evaluation_fields.issuperset(
            key for key in metadata if key.startswith("evaluation_")
        ):
            return False
        token = metadata.get(_EVALUATION_TOKEN_KEY)
        repository_id = metadata.get("evaluation_repository_id")
        case_id = metadata.get("evaluation_case_id")
        if not (
            isinstance(token, str)
            and isinstance(repository_id, str)
            and isinstance(case_id, str)
        ):
            return False
        try:
            digest = execution_attestation_digest(
                get_native_assistant_execution_attestation()
            )
        except RuntimeError:
            return False
        if not verify_evaluation_context_token(
            token,
            identity=str(ctx.user.identity),
            repository_id=repository_id,
            case_id=case_id,
            attestation_digest=digest,
        ):
            return False
        for key in evaluation_fields:
            metadata.pop(key, None)
        metadata.update(
            assistant_runtime_metadata(
                AssistantRuntimeFacts(entry_profile="evaluation")
            )
        )
    try:
        normalized_context = AssistantRunContext.model_validate(context)
    except ValueError:
        return False
    context.clear()
    context.update(normalized_context.model_dump())
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
