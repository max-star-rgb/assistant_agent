"""Application-owned Gateway runtime services."""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from assistant_agent.gateway import GatewayBridge, GatewayQueuePolicy, GatewaySessionManager
from assistant_agent.realtime import GatewayAgentAdapter, RealtimeAgentBackend
from assistant_agent.schemas.api import AgentRunResponse
from assistant_agent.services.gateway_turn_facade import GatewayTurnFacade

_GATEWAY_SESSION_MANAGER: GatewaySessionManager | None = None
_GATEWAY_BRIDGE: GatewayBridge | None = None
_GATEWAY_TURN_FACADE: GatewayTurnFacade | None = None
_GATEWAY_RUNTIME_LOOP_ID: int | None = None
_GATEWAY_HTTP_RESPONSES: dict[str, AgentRunResponse] = {}
_GATEWAY_HTTP_RESPONSES_LOCK = RLock()

GATEWAY_MAX_SESSIONS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_SESSIONS"
GATEWAY_IDLE_TIMEOUT_S_ENV = "MULTIMODAL_AGENT_GATEWAY_IDLE_TIMEOUT_S"
GATEWAY_HANGUP_GRACE_S_ENV = "MULTIMODAL_AGENT_GATEWAY_HANGUP_GRACE_S"
GATEWAY_REAPER_INTERVAL_S_ENV = "MULTIMODAL_AGENT_GATEWAY_REAPER_INTERVAL_S"
GATEWAY_START_REAPER_ENV = "MULTIMODAL_AGENT_GATEWAY_START_REAPER"
GATEWAY_MAX_ACTIVE_RUNS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_ACTIVE_RUNS"
GATEWAY_MAX_PENDING_PER_SESSION_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_PENDING_PER_SESSION"
GATEWAY_MAX_QUEUED_TURNS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_QUEUED_TURNS"
GATEWAY_QUEUE_WAIT_TIMEOUT_MS_ENV = "MULTIMODAL_AGENT_GATEWAY_QUEUE_WAIT_TIMEOUT_MS"
GATEWAY_DEDUPE_TTL_S_ENV = "MULTIMODAL_AGENT_GATEWAY_DEDUPE_TTL_S"
GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER_ENV = (
    "MULTIMODAL_AGENT_GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER"
)
GATEWAY_HTTP_RESPONSE_CAPTURE_ID = "http_response_capture_id"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def get_gateway_session_manager() -> GatewaySessionManager:
    """Return the process-local Gateway session manager."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE, _GATEWAY_TURN_FACADE, _GATEWAY_RUNTIME_LOOP_ID
    loop_id = _running_loop_id()
    if _GATEWAY_SESSION_MANAGER is None or _gateway_runtime_loop_changed(loop_id):
        _GATEWAY_SESSION_MANAGER = create_gateway_session_manager()
        _GATEWAY_RUNTIME_LOOP_ID = loop_id
        _GATEWAY_BRIDGE = None
        _GATEWAY_TURN_FACADE = None
    return _GATEWAY_SESSION_MANAGER


def get_gateway_bridge() -> GatewayBridge:
    """Return the process-local Gateway bridge."""

    global _GATEWAY_BRIDGE
    manager = get_gateway_session_manager()
    if _GATEWAY_BRIDGE is None:
        _GATEWAY_BRIDGE = GatewayBridge(session_manager=manager)
    return _GATEWAY_BRIDGE


def get_gateway_turn_facade() -> GatewayTurnFacade:
    """Return the process-local Gateway sync-turn facade."""

    global _GATEWAY_TURN_FACADE
    manager = get_gateway_session_manager()
    if _GATEWAY_TURN_FACADE is None:
        _GATEWAY_TURN_FACADE = create_gateway_turn_facade(manager=manager)
    return _GATEWAY_TURN_FACADE


def create_gateway_turn_facade(
    *,
    manager: GatewaySessionManager | None = None,
) -> GatewayTurnFacade:
    """Create a GatewayTurnFacade bound to the process-local session manager."""

    return GatewayTurnFacade(manager=manager or get_gateway_session_manager())


def create_gateway_session_manager(
    *,
    env: Mapping[str, str] | None = None,
    backend_factory: Callable[[], RealtimeAgentBackend] | None = None,
    start_reaper: bool | None = None,
) -> GatewaySessionManager:
    """Create a GatewaySessionManager from safe local defaults and env overrides."""

    source = os.environ if env is None else env
    resolved_backend_factory = backend_factory or _default_gateway_backend_factory
    defaults = GatewayQueuePolicy()
    queue_policy = GatewayQueuePolicy(
        max_active_runs=_positive_int_env(
            source,
            GATEWAY_MAX_ACTIVE_RUNS_ENV,
            default=defaults.max_active_runs,
        ),
        max_pending_per_session=_positive_int_env(
            source,
            GATEWAY_MAX_PENDING_PER_SESSION_ENV,
            default=defaults.max_pending_per_session,
        ),
        max_queued_turns_global=_positive_int_env(
            source,
            GATEWAY_MAX_QUEUED_TURNS_ENV,
            default=defaults.max_queued_turns_global,
        ),
        queue_wait_timeout_ms=_positive_int_env(
            source,
            GATEWAY_QUEUE_WAIT_TIMEOUT_MS_ENV,
            default=defaults.queue_wait_timeout_ms,
        ),
        dedupe_ttl_s=_positive_float_env(
            source,
            GATEWAY_DEDUPE_TTL_S_ENV,
            default=defaults.dedupe_ttl_s,
        ),
        dedupe_max_entries_per_user=_positive_int_env(
            source,
            GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER_ENV,
            default=defaults.dedupe_max_entries_per_user,
        ),
    )
    return GatewaySessionManager(
        max_sessions=_int_env(source, GATEWAY_MAX_SESSIONS_ENV, default=20),
        idle_timeout_s=_float_env(source, GATEWAY_IDLE_TIMEOUT_S_ENV, default=300.0),
        hangup_grace_s=_optional_float_env(source, GATEWAY_HANGUP_GRACE_S_ENV),
        reaper_interval_s=_float_env(source, GATEWAY_REAPER_INTERVAL_S_ENV, default=30.0),
        backend_factory=resolved_backend_factory,
        queue_policy=queue_policy,
        start_reaper=_bool_env(source, GATEWAY_START_REAPER_ENV, default=True)
        if start_reaper is None
        else start_reaper,
    )


def _default_gateway_backend_factory() -> RealtimeAgentBackend:
    return GatewayAgentAdapter(run_request=_run_assistant_request_with_http_runtime)


def _run_assistant_request_with_http_runtime(request: Any, **kwargs: Any) -> Any:
    from assistant_agent.api.routes_agent import get_assistant_runtime_app

    artifacts = get_assistant_runtime_app().run_request(request, **kwargs)
    capture_id = _gateway_http_response_capture_id(getattr(request, "metadata", {}))
    if capture_id is not None:
        _capture_gateway_http_response(capture_id, artifacts.api_response())
    return artifacts


def new_gateway_http_response_capture_id() -> str:
    """Create an opaque id for one in-process HTTP Gateway response capture."""

    return str(uuid.uuid4())


def gateway_http_capture_metadata(capture_id: str) -> dict[str, Any]:
    """Return internal Gateway metadata used to capture an HTTP response."""

    return {"gateway": {GATEWAY_HTTP_RESPONSE_CAPTURE_ID: capture_id}}


def pop_gateway_http_response(capture_id: str) -> AgentRunResponse | None:
    """Pop and return a captured HTTP response, if the backend produced one."""

    with _GATEWAY_HTTP_RESPONSES_LOCK:
        return _GATEWAY_HTTP_RESPONSES.pop(capture_id, None)


def _capture_gateway_http_response(capture_id: str, response: AgentRunResponse) -> None:
    with _GATEWAY_HTTP_RESPONSES_LOCK:
        _GATEWAY_HTTP_RESPONSES[capture_id] = response


def _gateway_http_response_capture_id(metadata: Any) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in ("gateway", "runtime"):
        value = metadata.get(key)
        if not isinstance(value, Mapping):
            continue
        capture_id = value.get(GATEWAY_HTTP_RESPONSE_CAPTURE_ID)
        if isinstance(capture_id, str) and capture_id:
            return capture_id
    return None


def set_gateway_runtime_for_tests(
    *,
    manager: GatewaySessionManager | None = None,
    bridge: GatewayBridge | None = None,
) -> None:
    """Install explicit Gateway runtime services for tests."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE, _GATEWAY_TURN_FACADE, _GATEWAY_RUNTIME_LOOP_ID
    _GATEWAY_SESSION_MANAGER = manager
    _GATEWAY_BRIDGE = bridge
    _GATEWAY_TURN_FACADE = None
    _GATEWAY_RUNTIME_LOOP_ID = _running_loop_id()
    _clear_gateway_http_responses()


async def shutdown_gateway_runtime() -> None:
    """Close application-owned Gateway sessions and reset process globals."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE, _GATEWAY_TURN_FACADE, _GATEWAY_RUNTIME_LOOP_ID
    manager = _GATEWAY_SESSION_MANAGER
    facade = _GATEWAY_TURN_FACADE
    _GATEWAY_SESSION_MANAGER = None
    _GATEWAY_BRIDGE = None
    _GATEWAY_TURN_FACADE = None
    _GATEWAY_RUNTIME_LOOP_ID = None
    _clear_gateway_http_responses()
    if facade is not None:
        await facade.close()
    if manager is not None:
        await manager.close()


def reset_gateway_runtime_for_tests() -> None:
    """Best-effort synchronous reset for tests without an active event loop."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE, _GATEWAY_TURN_FACADE, _GATEWAY_RUNTIME_LOOP_ID
    manager = _GATEWAY_SESSION_MANAGER
    facade = _GATEWAY_TURN_FACADE
    _GATEWAY_SESSION_MANAGER = None
    _GATEWAY_BRIDGE = None
    _GATEWAY_TURN_FACADE = None
    _GATEWAY_RUNTIME_LOOP_ID = None
    _clear_gateway_http_responses()
    if manager is None and facade is None:
        return

    async def close_runtime() -> None:
        if facade is not None:
            await facade.close()
        if manager is not None:
            await manager.close()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(close_runtime())
        return
    loop.create_task(close_runtime())


def _clear_gateway_http_responses() -> None:
    with _GATEWAY_HTTP_RESPONSES_LOCK:
        _GATEWAY_HTTP_RESPONSES.clear()


def _running_loop_id() -> int | None:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return None


def _gateway_runtime_loop_changed(loop_id: int | None) -> bool:
    if _GATEWAY_RUNTIME_LOOP_ID is None or loop_id is None:
        return False
    return loop_id != _GATEWAY_RUNTIME_LOOP_ID


def _int_env(env: Mapping[str, str], name: str, *, default: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _positive_int_env(env: Mapping[str, str], name: str, *, default: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float_env(env: Mapping[str, str], name: str, *, default: float) -> float:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be finite and positive") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


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
