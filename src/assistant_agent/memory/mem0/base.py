"""Minimal Mem0 adapter protocol and opaque identity binding."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.mem0 import (
    Mem0HealthResult,
    Mem0RecallRequest,
    Mem0RecallResult,
    Mem0TurnCaptureRequest,
    Mem0TurnCaptureResult,
    Mem0Identity,
)
from assistant_agent.services.provider_errors import sanitize_error_message


class Mem0OperationError(RuntimeError):
    """Recoverable Mem0 dependency failure."""

    def __init__(self, operation: str, message: str) -> None:
        super().__init__(sanitize_error_message(message))
        self.operation = operation
        self.recoverable = True


@dataclass(frozen=True)
class Mem0HttpRequest:
    method: str
    path: str
    body: Mapping[str, Any] | None = None
    query: Mapping[str, str] | None = None
    headers: Mapping[str, str] | None = None
    timeout_seconds: float = 5.0


class Mem0Adapter(Protocol):
    name: str
    configured: bool

    def health(self) -> Mem0HealthResult: ...

    def capture_turn(
        self,
        request: Mem0TurnCaptureRequest,
    ) -> Mem0TurnCaptureResult: ...

    def recall(
        self,
        request: Mem0RecallRequest,
    ) -> Mem0RecallResult: ...


def bind_mem0_identity(
    identity: RequestIdentity,
    *,
    namespace: str,
) -> Mem0Identity:
    """Map trusted application identity to opaque Mem0 identity filters."""

    tenant = identity.tenant_id or "default"
    project = identity.project_id or "global"

    def digest(kind: str, value: str) -> str:
        payload = f"assistant_agent:{namespace}:{kind}:{value}".encode()
        return hashlib.sha256(payload).hexdigest()[:32]

    user_seed = f"{tenant}\x1f{identity.user_id}"
    agent_seed = f"{tenant}\x1f{project}"
    return Mem0Identity(
        user_id=f"usr_{digest('user', user_seed)}",
        agent_id=f"agt_{digest('agent', agent_seed)}",
    )
