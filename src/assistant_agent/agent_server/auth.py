"""Agent Server authentication entry point.

Mock mode is intentionally local/developer scoped. Real mode accepts only an
explicitly configured service bearer token; end-user ownership is added in the
resource-authorization migration rather than inferred from vendor fields.
"""

from __future__ import annotations

import hmac
import hashlib
import os

from fastapi import HTTPException
from langgraph_sdk import Auth


_PROVIDER_MODE_ENV = "MULTIMODAL_AGENT_PROVIDER_MODE"
_SERVICE_TOKEN_ENV = "ASSISTANT_AGENT_SERVER_SERVICE_TOKEN"

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
    if os.environ.get(_PROVIDER_MODE_ENV, "mock") == "mock":
        identity = _header_text(headers, b"x-assistant-user") or "local-developer"
        tenant_id = _header_text(headers, b"x-assistant-tenant") or "local"
        return {
            "identity": identity,
            "permissions": ["assistant:developer"],
            "is_authenticated": True,
            "tenant_id": tenant_id,
        }

    expected = os.environ.get(_SERVICE_TOKEN_ENV)
    received = (authorization or "").removeprefix("Bearer ")
    if not expected or not received or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Agent Server authentication failed")
    identity = _header_text(headers, b"x-assistant-user")
    tenant_id = _header_text(headers, b"x-assistant-tenant")
    signature = _header_text(headers, b"x-assistant-signature")
    if not identity or not tenant_id or not signature:
        raise HTTPException(status_code=401, detail="Signed user delegation is required")
    expected_signature = delegated_identity_signature(
        secret=expected,
        identity=identity,
        tenant_id=tenant_id,
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="User delegation signature is invalid")
    return {
        "identity": identity,
        "permissions": [],
        "is_authenticated": True,
        "tenant_id": tenant_id,
    }


def _header_text(headers: dict[bytes, bytes], name: bytes) -> str | None:
    raw = headers.get(name)
    if raw is None:
        return None
    value = raw.decode("utf-8", errors="strict").strip()
    return value or None


def delegated_identity_signature(
    *, secret: str, identity: str, tenant_id: str
) -> str:
    payload = f"{identity}\n{tenant_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


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
    "authorize_run_create",
    "authorize_thread_create",
    "authorize_thread_delete",
    "authorize_thread_read",
    "authorize_thread_search",
    "authorize_thread_update",
    "deny_all",
    "delegated_identity_signature",
    "scope_store",
]
