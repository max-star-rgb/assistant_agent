"""Agent Server authentication entry point.

Mock mode is intentionally local/developer scoped. Real mode accepts only an
explicitly configured service bearer token; end-user ownership is added in the
resource-authorization migration rather than inferred from vendor fields.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException
from langgraph_sdk import Auth


_PROVIDER_MODE_ENV = "MULTIMODAL_AGENT_PROVIDER_MODE"
_SERVICE_TOKEN_ENV = "ASSISTANT_AGENT_SERVER_SERVICE_TOKEN"

auth = Auth()


@auth.authenticate
async def authenticate(authorization: str | None) -> Auth.types.MinimalUserDict:
    if os.environ.get(_PROVIDER_MODE_ENV, "mock") == "mock":
        return {
            "identity": "local-developer",
            "permissions": ["assistant:developer"],
            "is_authenticated": True,
        }

    expected = os.environ.get(_SERVICE_TOKEN_ENV)
    received = (authorization or "").removeprefix("Bearer ")
    if not expected or not received or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Agent Server authentication failed")
    return {
        "identity": "media-service",
        "permissions": ["assistant:invoke"],
        "is_authenticated": True,
    }


__all__ = ["auth"]
