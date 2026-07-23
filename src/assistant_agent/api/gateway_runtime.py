"""Application-owned Gateway runtime services."""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from assistant_agent.gateway import (
    GatewayBridge,
    GatewayConnectionPolicy,
    GatewayQueuePolicy,
    GatewayRuntimePool,
    GatewaySessionManager,
    GatewayTurnArbitrationController,
    GatewayTurnArbitrationPolicy,
    shared_gateway_runtime_factory,
)
from assistant_agent.realtime import GatewayAgentAdapter, RealtimeAgentBackend
from assistant_agent.schemas.api import AgentRunResponse
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.services.gateway_turn_facade import GatewayTurnFacade
from assistant_agent.services.identifiers import new_prefixed_uuid7
from assistant_agent.services.operational_logging import record_gateway_lifecycle
from assistant_agent.services.realtime_turn_arbiter import (
    RealtimeTurnArbiter,
    create_realtime_turn_arbiter,
)

_GATEWAY_SESSION_MANAGER: GatewaySessionManager | None = None
_GATEWAY_BRIDGE: GatewayBridge | None = None
_GATEWAY_TURN_FACADE: GatewayTurnFacade | None = None
_GATEWAY_RUNTIME_POOL: GatewayRuntimePool | None = None
_GATEWAY_RUNTIME_LOOP_ID: int | None = None
_GATEWAY_HTTP_RESPONSES: dict[str, AgentRunResponse] = {}
_GATEWAY_HTTP_RESPONSES_LOCK = RLock()

GATEWAY_MAX_SESSIONS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_SESSIONS"
GATEWAY_IDLE_TIMEOUT_S_ENV = "MULTIMODAL_AGENT_GATEWAY_IDLE_TIMEOUT_S"
GATEWAY_REAPER_INTERVAL_S_ENV = "MULTIMODAL_AGENT_GATEWAY_REAPER_INTERVAL_S"
GATEWAY_START_REAPER_ENV = "MULTIMODAL_AGENT_GATEWAY_START_REAPER"
GATEWAY_DETACH_GRACE_S_ENV = "MULTIMODAL_AGENT_GATEWAY_DETACH_GRACE_S"
GATEWAY_OUTBOX_MAX_FRAMES_ENV = "MULTIMODAL_AGENT_GATEWAY_OUTBOX_MAX_FRAMES"
GATEWAY_MAX_ACTIVE_RUNS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_ACTIVE_RUNS"
GATEWAY_MAX_RUNTIME_INSTANCES_ENV = (
    "MULTIMODAL_AGENT_GATEWAY_MAX_RUNTIME_INSTANCES"
)
GATEWAY_MAX_PENDING_PER_SESSION_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_PENDING_PER_SESSION"
GATEWAY_MAX_QUEUED_TURNS_ENV = "MULTIMODAL_AGENT_GATEWAY_MAX_QUEUED_TURNS"
GATEWAY_QUEUE_WAIT_TIMEOUT_MS_ENV = "MULTIMODAL_AGENT_GATEWAY_QUEUE_WAIT_TIMEOUT_MS"
GATEWAY_DEDUPE_TTL_S_ENV = "MULTIMODAL_AGENT_GATEWAY_DEDUPE_TTL_S"
GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER_ENV = (
    "MULTIMODAL_AGENT_GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER"
)
REALTIME_SEMANTIC_INTERRUPT_ENABLED_ENV = (
    "MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_ENABLED"
)
REALTIME_SEMANTIC_INTERRUPT_TIMEOUT_MS_ENV = (
    "MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_TIMEOUT_MS"
)
REALTIME_SEMANTIC_INTERRUPT_MAX_CONCURRENCY_ENV = (
    "MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MAX_CONCURRENCY"
)
REALTIME_SEMANTIC_INTERRUPT_MIN_CONFIDENCE_ENV = (
    "MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MIN_CONFIDENCE"
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
        _GATEWAY_BRIDGE = GatewayBridge(
            session_manager=manager,
            connection_policy=_gateway_connection_policy(os.environ),
        )
    return _GATEWAY_BRIDGE


def get_gateway_turn_facade() -> GatewayTurnFacade:
    """Return the process-local Gateway sync-turn facade."""

    global _GATEWAY_TURN_FACADE
    manager = get_gateway_session_manager()
    if _GATEWAY_TURN_FACADE is None:
        _GATEWAY_TURN_FACADE = create_gateway_turn_facade(manager=manager)
    return _GATEWAY_TURN_FACADE


def get_gateway_runtime_pool_for_tests() -> GatewayRuntimePool | None:
    """Return the process-local Gateway runtime pool for focused tests."""

    return _GATEWAY_RUNTIME_POOL


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
    turn_arbitration_controller: GatewayTurnArbitrationController | None = None,
    start_reaper: bool | None = None,
) -> GatewaySessionManager:
    """Create a GatewaySessionManager from safe local defaults and env overrides."""

    global _GATEWAY_RUNTIME_POOL
    source = os.environ if env is None else env
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
    if backend_factory is None:
        max_runtime_instances = _positive_int_env(
            source,
            GATEWAY_MAX_RUNTIME_INSTANCES_ENV,
            default=queue_policy.max_active_runs,
        )
        if max_runtime_instances < queue_policy.max_active_runs:
            raise ValueError(
                "max_runtime_instances must be greater than or equal to max_active_runs"
            )
        runtime_pool = GatewayRuntimePool(
            max_runtime_instances=max_runtime_instances,
            runtime_factory=_default_gateway_runtime_factory(),
            run_request=_run_assistant_request_with_http_runtime,
            runtime_cleanup=lambda runtime: runtime.close(),
        )
        _GATEWAY_RUNTIME_POOL = runtime_pool
        resolved_backend_factory = _default_gateway_backend_factory(runtime_pool)
    else:
        resolved_backend_factory = backend_factory
    arbitration_policy = GatewayTurnArbitrationPolicy(
        enabled=_bool_env(
            source,
            REALTIME_SEMANTIC_INTERRUPT_ENABLED_ENV,
            default=False,
        ),
        timeout_ms=_positive_int_env(
            source,
            REALTIME_SEMANTIC_INTERRUPT_TIMEOUT_MS_ENV,
            default=1000,
        ),
        max_concurrency=_positive_int_env(
            source,
            REALTIME_SEMANTIC_INTERRUPT_MAX_CONCURRENCY_ENV,
            default=2,
        ),
        min_confidence=_unit_interval_env(
            source,
            REALTIME_SEMANTIC_INTERRUPT_MIN_CONFIDENCE_ENV,
            default=0.80,
        ),
    )
    resolved_turn_arbitration_controller = turn_arbitration_controller
    if resolved_turn_arbitration_controller is None:
        resolved_turn_arbitration_controller = GatewayTurnArbitrationController(
            policy=arbitration_policy,
            arbiter_factory=lambda: _default_realtime_turn_arbiter(
                min_confidence=arbitration_policy.min_confidence,
            ),
        )
    return GatewaySessionManager(
        max_sessions=_int_env(source, GATEWAY_MAX_SESSIONS_ENV, default=20),
        idle_timeout_s=_float_env(source, GATEWAY_IDLE_TIMEOUT_S_ENV, default=300.0),
        reaper_interval_s=_float_env(source, GATEWAY_REAPER_INTERVAL_S_ENV, default=30.0),
        backend_factory=resolved_backend_factory,
        queue_policy=queue_policy,
        turn_arbitration_controller=resolved_turn_arbitration_controller,
        session_initializer=lambda user_id, session_id, config: (
            _initialize_gateway_session_memory(
                runtime_pool,
                user_id=user_id,
                session_id=session_id,
                config=config,
            )
            if backend_factory is None
            else _noop_session_initializer()
        ),
        lifecycle_sink=record_gateway_lifecycle,
        start_reaper=_bool_env(source, GATEWAY_START_REAPER_ENV, default=True)
        if start_reaper is None
        else start_reaper,
    )


async def _initialize_gateway_session_memory(
    runtime_pool: GatewayRuntimePool,
    *,
    user_id: str,
    session_id: str,
    config: Mapping[str, Any],
) -> None:
    identity = RequestIdentity.for_user(
        user_id=user_id,
        session_id=session_id,
        tenant_id=_optional_config_string(config, "tenant_id"),
        project_id=_optional_config_string(config, "project_id"),
    )
    await asyncio.to_thread(runtime_pool.initialize_session_memory, identity)


async def _noop_session_initializer() -> None:
    return None


def _optional_config_string(config: Mapping[str, Any], key: str) -> str | None:
    value = config.get(key)
    text = str(value).strip() if value is not None else ""
    return text or None


def _gateway_connection_policy(source: Mapping[str, str]) -> GatewayConnectionPolicy:
    defaults = GatewayConnectionPolicy()
    return GatewayConnectionPolicy(
        detach_grace_s=_non_negative_float_env(
            source,
            GATEWAY_DETACH_GRACE_S_ENV,
            default=defaults.detach_grace_s,
        ),
        outbox_max_frames=_positive_int_env(
            source,
            GATEWAY_OUTBOX_MAX_FRAMES_ENV,
            default=defaults.outbox_max_frames,
        ),
    )


def _default_gateway_backend_factory(
    runtime_pool: GatewayRuntimePool,
) -> Callable[[], RealtimeAgentBackend]:
    return lambda: GatewayAgentAdapter(run_request=runtime_pool.run_request)


def _default_gateway_runtime_factory() -> Callable[[], Any]:
    from assistant_agent.api import routes_agent

    return shared_gateway_runtime_factory(routes_agent.get_agent_runtime)


def _default_realtime_turn_arbiter(*, min_confidence: float) -> RealtimeTurnArbiter:
    from assistant_agent.api.routes_agent import get_assistant_runtime_app

    runtime = get_assistant_runtime_app().runtime
    return create_realtime_turn_arbiter(
        runtime.config,
        runtime.chat_adapter,
        min_confidence=min_confidence,
    )


def _run_assistant_request_with_http_runtime(request: Any, **kwargs: Any) -> Any:
    if kwargs.get("runtime") is None:
        from assistant_agent.api.routes_agent import get_assistant_runtime_app

        artifacts = get_assistant_runtime_app().run_request(request, **kwargs)
    else:
        from assistant_agent.services.assistant_run_service import run_assistant_request

        artifacts = run_assistant_request(request, **kwargs)
    capture_id = _gateway_http_response_capture_id(getattr(request, "metadata", {}))
    if capture_id is not None:
        _capture_gateway_http_response(capture_id, artifacts.api_response())
    return artifacts


def new_gateway_http_response_capture_id() -> str:
    """Create an opaque id for one in-process HTTP Gateway response capture."""

    return new_prefixed_uuid7("gateway_response")


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

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE, _GATEWAY_TURN_FACADE, _GATEWAY_RUNTIME_POOL, _GATEWAY_RUNTIME_LOOP_ID
    _GATEWAY_SESSION_MANAGER = manager
    _GATEWAY_BRIDGE = bridge
    _GATEWAY_TURN_FACADE = None
    _GATEWAY_RUNTIME_POOL = None
    _GATEWAY_RUNTIME_LOOP_ID = _running_loop_id()
    _clear_gateway_http_responses()


async def shutdown_gateway_runtime() -> None:
    """Close application-owned Gateway sessions and reset process globals."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE, _GATEWAY_TURN_FACADE, _GATEWAY_RUNTIME_POOL, _GATEWAY_RUNTIME_LOOP_ID
    manager = _GATEWAY_SESSION_MANAGER
    facade = _GATEWAY_TURN_FACADE
    runtime_pool = _GATEWAY_RUNTIME_POOL
    _GATEWAY_SESSION_MANAGER = None
    _GATEWAY_BRIDGE = None
    _GATEWAY_TURN_FACADE = None
    _GATEWAY_RUNTIME_POOL = None
    _GATEWAY_RUNTIME_LOOP_ID = None
    _clear_gateway_http_responses()
    if facade is not None:
        await facade.close()
    if manager is not None:
        await manager.close()
    if runtime_pool is not None:
        runtime_pool.close()


def reset_gateway_runtime_for_tests() -> None:
    """Best-effort synchronous reset for tests without an active event loop."""

    global _GATEWAY_SESSION_MANAGER, _GATEWAY_BRIDGE, _GATEWAY_TURN_FACADE, _GATEWAY_RUNTIME_POOL, _GATEWAY_RUNTIME_LOOP_ID
    manager = _GATEWAY_SESSION_MANAGER
    facade = _GATEWAY_TURN_FACADE
    runtime_pool = _GATEWAY_RUNTIME_POOL
    _GATEWAY_SESSION_MANAGER = None
    _GATEWAY_BRIDGE = None
    _GATEWAY_TURN_FACADE = None
    _GATEWAY_RUNTIME_POOL = None
    _GATEWAY_RUNTIME_LOOP_ID = None
    _clear_gateway_http_responses()
    if manager is None and facade is None and runtime_pool is None:
        return

    async def close_runtime() -> None:
        if facade is not None:
            await facade.close()
        if manager is not None:
            await manager.close()
        if runtime_pool is not None:
            runtime_pool.close()

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


def _non_negative_float_env(
    env: Mapping[str, str],
    name: str,
    *,
    default: float,
) -> float:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be finite and non-negative") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


def _unit_interval_env(env: Mapping[str, str], name: str, *, default: float) -> float:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be finite and between 0 and 1") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")
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
