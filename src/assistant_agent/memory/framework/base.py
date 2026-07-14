"""Framework adapter protocol and opaque identity binding."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory_framework import (
    FrameworkHealthResult,
    FrameworkRecallRequest,
    FrameworkRecallResult,
    FrameworkRetainRequest,
    FrameworkRetainResult,
    MemoryEngineIdentity,
)


@dataclass(frozen=True)
class FrameworkHttpRequest:
    method: str
    path: str
    body: Mapping[str, Any] | None = None
    query: Mapping[str, str] | None = None
    headers: Mapping[str, str] | None = None
    timeout_seconds: float = 5.0


class MemoryEngineAdapter(Protocol):
    name: str

    def health(self) -> FrameworkHealthResult: ...
    def retain(self, request: FrameworkRetainRequest) -> FrameworkRetainResult: ...
    def recall(self, request: FrameworkRecallRequest) -> FrameworkRecallResult: ...
    def reflect(self, request: FrameworkRecallRequest) -> Mapping[str, Any]: ...
    def get(self, *, identity: MemoryEngineIdentity, engine_id: str) -> Mapping[str, Any] | None: ...
    def list(self, *, identity: MemoryEngineIdentity) -> list[Mapping[str, Any]]: ...
    def history(self, *, identity: MemoryEngineIdentity, engine_id: str) -> list[Mapping[str, Any]]: ...
    def delete(self, *, identity: MemoryEngineIdentity, engine_id: str, project_memory_id: str | None = None) -> bool: ...
    def clear(self, *, identity: MemoryEngineIdentity) -> int: ...
    def export(self, *, identity: MemoryEngineIdentity) -> list[Mapping[str, Any]]: ...


def bind_engine_identity(identity: RequestIdentity, *, namespace: str) -> MemoryEngineIdentity:
    """Derive engine identifiers without exposing raw governance identity."""

    tenant = identity.tenant_id or "local"
    project = identity.project_id or "global"
    session = identity.session_id or "no-session"

    def digest(kind: str, value: str, length: int) -> str:
        payload = f"assistant_agent:{namespace}:{kind}:{value}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:length]

    user_scope = f"{tenant}\x1f{identity.user_id}"
    project_scope = f"{user_scope}\x1f{project}"
    session_scope = f"{project_scope}\x1f{session}"
    return MemoryEngineIdentity(
        bank_id=f"bank_{digest('bank', user_scope, 32)}",
        user_id=f"usr_{digest('user', user_scope, 32)}",
        agent_id=f"agt_{digest('agent', project_scope, 32)}",
        run_id=f"run_{digest('run', session_scope, 32)}",
        tenant_tag=f"tenant_{digest('tenant-tag', tenant, 24)}",
        user_tag=f"user_{digest('user-tag', user_scope, 24)}",
        project_tag=f"project_{digest('project-tag', project_scope, 24)}",
        session_tag=f"session_{digest('session-tag', session_scope, 24)}",
    )
