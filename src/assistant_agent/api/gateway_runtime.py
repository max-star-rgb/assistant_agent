"""Application-owned Gateway runtime services."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from assistant_agent.gateway import GatewayBridge, GatewaySessionManager
from assistant_agent.realtime import GatewayAgentAdapter, RealtimeAgentBackend

_GATEWAY_SESSION_MANAGER: GatewaySessionManager | None = None
_GATEWAY_BRIDGE: GatewayBridge | None = None

GATEWAY_MAX_SESSIONS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_SESSIONS"
GATEWAY_IDLE_TIMEOUT_S_ENV = "MULTIMODAL_AGENT_GATEWAY_IDLE_TIMEOUT_S"
GATEWAY_HANGUP_GRACE_S_ENV = "MULTIMODAL_AGENT_GATEWAY_HANGUP_GRACE_S"
GATEWAY_REAPER_INTERVAL_S_ENV = "MULTIMODAL_AGENT_GATEWAY_REAPER_INTERVAL_S"
GATEWAY_START_REAPER_ENV = "MULTIMODAL_AGENT_GATEWAY_START_REAPER"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def get_gateway_session_manager() -> GatewaySessionManager:
    """Return the process-local Gateway session manager."""

    global _GATEWAY_SESSION_MANAGER
    if _GATEWAY_SESSION_MANAGER is None:
        _GATEWAY_SESSION_MANAGER = create_gateway_session_manager()
    return _GATEWAY_SESSION_MANAGER


def get_gateway_bridge() -> GatewayBridge:
    """Return the process-local Gateway bridge."""

    global _GATEWAY_BRIDGE
    if _GATEWAY_BRIDGE is None:
        _GATEWAY_BRIDGE = GatewayBridge(session_manager=get_gateway_session_manager())
    return _GATEWAY_BRIDGE


def create_gateway_session_manager(
    *,
    env: Mapping[str, str] | None = None,
    backend_factory: Callable[[], RealtimeAgentBackend] | None = None,
    start_reaper: bool | None = None,
) -> GatewaySessionManager:
    """Create a GatewaySessionManager from safe local defaults and env overrides."""

    source = os.environ if env is None else env
    resolved_backend_factory = backend_factory or _default_gateway_backend_factory
    return GatewaySessionManager(
        max_sessions=_int_env(source, GATEWAY_MAX_SESSIONS_ENV, default=20),
        idle_timeout_s=_float_env(source, GATEWAY_IDLE_TIMEOUT_S_ENV, default=300.0),
        hangup_grace_s=_optional_float_env(source, GATEWAY_HANGUP_GRACE_S_ENV),
        reaper_interval_s=_float_env(source, GATEWAY_REAPER_INTERVAL_S_ENV, default=30.0),
        backend_factory=resolved_backend_factory,
        start_reaper=_bool_env(source, GATEWAY_START_REAPER_ENV, default=True)
        if start_reaper is None
        else start_reaper,
    )


def _default_gateway_backend_factory() -> RealtimeAgentBackend:
    return GatewayAgentAdapter(run_request=_run_assistant_request_with_http_runtime)


def _run_assistant_request_with_http_runtime(request: Any, **kwargs: Any) -> Any:
    from assistant_agent.api.routes_agent import get_agent_runtime
    from assistant_agent.services.assistant_run_service import run_assistant_request

    return run_assistant_request(request, runtime=get_agent_runtime(), **kwargs)


def set_gateway_runtime_for_tests(
    *,
    manager: GatewaySessionManager | None = None,
    bridge: GatewayBridge | None = None,
) -> None:
    """Install explicit Gateway runtime services for tests."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE
    _GATEWAY_SESSION_MANAGER = manager
    _GATEWAY_BRIDGE = bridge


async def shutdown_gateway_runtime() -> None:
    """Close application-owned Gateway sessions and reset process globals."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE
    manager = _GATEWAY_SESSION_MANAGER
    _GATEWAY_SESSION_MANAGER = None
    _GATEWAY_BRIDGE = None
    if manager is not None:
        await manager.close()


def reset_gateway_runtime_for_tests() -> None:
    """Best-effort synchronous reset for tests without an active event loop."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE
    manager = _GATEWAY_SESSION_MANAGER
    _GATEWAY_SESSION_MANAGER = None
    _GATEWAY_BRIDGE = None
    if manager is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(manager.close())
        return
    loop.create_task(manager.close())


def _int_env(env: Mapping[str, str], name: str, *, default: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _float_env(env: Mapping[str, str], name: str, *, default: float) -> float:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _optional_float_env(env: Mapping[str, str], name: str) -> float | None:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _bool_env(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = str(env.get(name, "")).strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def repo_root() -> Path:
    """Return the repository root for shared local API services."""

    return Path(__file__).resolve().parents[3]
