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
    FrameworkTurnCaptureRequest,
    FrameworkTurnCaptureResult,
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
    def capture_turn(self, request: FrameworkTurnCaptureRequest) -> FrameworkTurnCaptureResult: ...
    def recall(self, request: FrameworkRecallRequest) -> FrameworkRecallResult: ...
    def reflect(self, request: FrameworkRecallRequest) -> Mapping[str, Any]: ...
    def get(self, *, identity: MemoryEngineIdentity, engine_id: str) -> Mapping[str, Any] | None: ...
    def list(self, *, identity: MemoryEngineIdentity) -> list[Mapping[str, Any]]: ...
    def history(self, *, identity: MemoryEngineIdentity, engine_id: str) -> list[Mapping[str, Any]]: ...
    def update(self, *, identity: MemoryEngineIdentity, engine_id: str, text: str) -> bool: ...
    def delete(self, *, identity: MemoryEngineIdentity, engine_id: str, project_memory_id: str | None = None) -> bool: ...
    def clear(self, *, identity: MemoryEngineIdentity) -> int: ...
    def export(self, *, identity: MemoryEngineIdentity) -> list[Mapping[str, Any]]: ...


def bind_engine_identity(identity: RequestIdentity, *, namespace: str) -> MemoryEngineIdentity:
    """Derive engine identifiers without exposing raw governance identity.

    assistant_agent keeps `project_id`/`session_id` as domain fields. At the
    framework boundary they are folded into opaque engine `agent_id`/`run_id`
    values; raw tenant, project, and session values never cross the adapter.
    """

    tenant_raw = identity.tenant_id or "local"
    project_raw = identity.project_id or "global"
    session_raw = identity.session_id or "no-session"

    def digest(kind: str, value: str, length: int) -> str:
        payload = f"assistant_agent:{namespace}:{kind}:{value}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:length]

    engine_user_seed = f"{tenant_raw}\x1f{identity.user_id}"
    engine_agent_seed = f"{engine_user_seed}\x1f{project_raw}"
    engine_run_seed = f"{engine_agent_seed}\x1f{session_raw}"
    return MemoryEngineIdentity(
        bank_id=f"bank_{digest('bank', engine_user_seed, 32)}",
        user_id=f"usr_{digest('user', engine_user_seed, 32)}",
        agent_id=f"agt_{digest('agent', engine_agent_seed, 32)}",
        run_id=f"run_{digest('run', engine_run_seed, 32)}",
        tenant_tag=f"tenant_{digest('tenant-tag', tenant_raw, 24)}",
        user_tag=f"user_{digest('user-tag', engine_user_seed, 24)}",
        project_tag=f"project_{digest('project-tag', engine_agent_seed, 24)}",
        session_tag=f"session_{digest('session-tag', engine_run_seed, 24)}",
    )
