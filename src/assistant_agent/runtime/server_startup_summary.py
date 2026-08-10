"""Operator-visible startup report built from the finalized application state."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant_agent.api.auth import require_auth_bound_identity
from assistant_agent.api.trial_access import trial_access_gate_from_env
from assistant_agent.observability.agent_service_delivery import DEFAULT_AUDIT_PATH
from assistant_agent.observability.operational_logging import (
    GATEWAY_EVENT_PATH_ENV,
    OPERATIONAL_CONSOLE_LEVEL_ENV,
    OPERATIONAL_FILE_LEVEL_ENV,
    OPERATIONAL_LOG_DIR_ENV,
)
from assistant_agent.observability.trace_content_policy import (
    local_memory_trace_content_enabled,
    local_provider_protocol_capture_enabled,
    local_trace_content_enabled,
)
from assistant_agent.runtime.startup_dependencies import (
    StartupDependencyStatus,
    collect_startup_dependency_statuses,
)
from assistant_agent.tools.registry import ToolRegistry


STARTUP_BIND_HOST_ENV = "MULTIMODAL_AGENT_STARTUP_BIND_HOST"
STARTUP_BIND_PORT_ENV = "MULTIMODAL_AGENT_STARTUP_BIND_PORT"
STARTUP_PUBLIC_URL_ENV = "MULTIMODAL_AGENT_STARTUP_PUBLIC_URL"
STARTUP_DETAILS_ENV = "MULTIMODAL_AGENT_STARTUP_DETAILS"

_TOOL_CATEGORY_ORDER = ("read", "generate", "write", "dangerous")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_PREPARED_STARTUP_LINES: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ToolInventory:
    total: int
    sealed: bool
    category_counts: tuple[tuple[str, int], ...]
    source_counts: tuple[tuple[str, int], ...]
    plugin_count: int


@dataclass(frozen=True)
class RouteInventory:
    http_count: int
    websocket_count: int
    health_path: str | None


@dataclass(frozen=True)
class WorkerStatus:
    name: str
    state: str


@dataclass(frozen=True)
class ServerStartupReport:
    state: str
    bind_host: str
    bind_port: int
    public_url: str | None
    provider_mode: str
    chat_provider: str
    chat_model: str
    tools: ToolInventory
    routes: RouteInventory
    dependencies: tuple[StartupDependencyStatus, ...]
    workers: tuple[WorkerStatus, ...]
    auth_bound_identity_required: bool
    trial_user_count: int
    trace_content_enabled: bool
    provider_protocol_capture_enabled: bool
    memory_trace_content_enabled: bool
    console_level: str
    file_level: str
    log_dir: str
    gateway_event_path: str
    runtime_trace_path: str | None
    delivery_audit_path: str
    details: bool


def build_server_startup_report(
    app: Any,
    runtime: Any,
    *,
    env: Mapping[str, str] | None = None,
) -> ServerStartupReport:
    """Collect one prompt-safe report from the fully assembled app and runtime."""

    values = os.environ if env is None else env
    config = runtime.config
    dependencies = collect_startup_dependency_statuses(config, env=values)
    tools = _tool_inventory(runtime.registry)
    workers = _worker_statuses(app, config)
    unavailable = any(item.state == "unavailable" for item in dependencies)
    unavailable = unavailable or any(item.state == "unavailable" for item in workers)
    if not tools.sealed:
        state = "NOT READY"
    elif unavailable:
        state = "READY (degraded)"
    else:
        state = "READY"

    gate = trial_access_gate_from_env(values)
    return ServerStartupReport(
        state=state,
        bind_host=str(values.get(STARTUP_BIND_HOST_ENV) or "127.0.0.1"),
        bind_port=_safe_port(values.get(STARTUP_BIND_PORT_ENV), default=8000),
        public_url=_clean_optional(values.get(STARTUP_PUBLIC_URL_ENV)),
        provider_mode=str(config.provider_mode),
        chat_provider=str(config.chat_provider),
        chat_model=str(config.chat_model or "default"),
        tools=tools,
        routes=_route_inventory(app),
        dependencies=dependencies,
        workers=workers,
        auth_bound_identity_required=require_auth_bound_identity(values),
        trial_user_count=gate.allowed_user_count,
        trace_content_enabled=local_trace_content_enabled(values),
        provider_protocol_capture_enabled=local_provider_protocol_capture_enabled(values),
        memory_trace_content_enabled=local_memory_trace_content_enabled(values),
        console_level=str(values.get(OPERATIONAL_CONSOLE_LEVEL_ENV) or "INFO"),
        file_level=str(values.get(OPERATIONAL_FILE_LEVEL_ENV) or "DEBUG"),
        log_dir=str(values.get(OPERATIONAL_LOG_DIR_ENV) or ".data/logs"),
        gateway_event_path=str(
            values.get(GATEWAY_EVENT_PATH_ENV) or ".data/gateway_events.jsonl"
        ),
        runtime_trace_path=_runtime_trace_path(runtime),
        delivery_audit_path=str(DEFAULT_AUDIT_PATH.resolve()),
        details=_env_enabled(values.get(STARTUP_DETAILS_ENV)),
    )


def format_server_startup_report(
    report: ServerStartupReport,
    *,
    registry: ToolRegistry | None = None,
) -> list[str]:
    """Render the compact default view and optional ownership details."""

    bind = _format_host_port(report.bind_host, report.bind_port)
    local_base = report.public_url or _local_base_url(report.bind_host, report.bind_port)
    route_parts = [f"HTTP {report.routes.http_count}", f"WebSocket {report.routes.websocket_count}"]
    category_summary = _format_counts(report.tools.category_counts)
    source_summary = _format_counts(report.tools.source_counts)
    sealed = "sealed" if report.tools.sealed else "unsealed"

    lines = [
        f"assistant_agent  {report.state}",
        "",
        "Listening:",
        f"  Bind:       {bind}{' (network-accessible)' if _network_accessible(report.bind_host) else ''}",
        f"  Routes:     {', '.join(route_parts)}",
    ]
    if report.routes.health_path:
        lines.append(f"  Health:     GET {local_base.rstrip('/')}{report.routes.health_path}")
    lines.extend(
        [
            "",
            "Runtime:",
            f"  Mode:       {report.provider_mode}",
            f"  Main LLM:   {report.chat_provider} / {report.chat_model}",
            f"  Tools:      {report.tools.total} registered, registry {sealed} ({category_summary})",
            f"  Sources:    {report.tools.plugin_count} plugins ({source_summary})",
            f"  Workers:    {_format_workers(report.workers)}",
        ]
    )

    visible_dependencies = tuple(
        item for item in report.dependencies if item.state != "disabled"
    )
    if visible_dependencies:
        lines.extend(["", "Integrations:"])
        lines.extend(f"  {item.name}: {item.state}{_detail_suffix(item.detail)}" for item in visible_dependencies)

    lines.extend(
        [
            "",
            "Safety:",
            "  Auth-bound identity:       "
            + ("required" if report.auth_bound_identity_required else "not required"),
            f"  Trial allowlist:           {report.trial_user_count} users",
            f"  Local trace content:       {_enabled(report.trace_content_enabled)}",
            f"  Provider protocol capture: {_enabled(report.provider_protocol_capture_enabled)}",
            f"  Memory trace content:      {_enabled(report.memory_trace_content_enabled)}",
            "",
            "Observability:",
            f"  Runtime trace:   {report.runtime_trace_path or 'in-memory only'}",
            f"  Runtime export:  Langfuse {_langfuse_export_state(report.dependencies)}",
            f"  Gateway events:  {report.gateway_event_path}",
            f"  Delivery audit:  {report.delivery_audit_path}",
            f"  Gateway log:     {report.file_level} -> {report.log_dir.rstrip('/')}/gateway.log",
            f"  Console:         {report.console_level} "
            "(Gateway lifecycle + application warnings; no Runtime timeline)",
        ]
    )

    warnings = _report_warnings(report)
    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in warnings)

    if report.details and registry is not None:
        lines.extend(["", "Tool sources (ownership, not capability categories):"])
        lines.extend(format_tool_registry_summary(registry))
    return lines


def prepare_server_startup_report(app: Any, runtime: Any) -> None:
    """Prepare report lines during ASGI startup without claiming the socket is bound."""

    global _PREPARED_STARTUP_LINES
    try:
        report = build_server_startup_report(app, runtime)
        lines = format_server_startup_report(report, registry=runtime.registry)
    except Exception as exc:  # Startup diagnostics must not prevent serving traffic.
        lines = [
            "assistant_agent  STATUS UNKNOWN (startup summary unavailable)",
            f"Warning: startup report failed with {type(exc).__name__}",
        ]
    _PREPARED_STARTUP_LINES = tuple(lines)


def print_prepared_server_startup_report() -> None:
    """Print the prepared report after Uvicorn has successfully bound its socket."""

    lines = _PREPARED_STARTUP_LINES or (
        "assistant_agent  STATUS UNKNOWN (startup summary was not prepared)",
    )
    for line in lines:
        print(line, flush=True)


def print_server_startup_report(app: Any, runtime: Any) -> None:
    """Compatibility helper for non-Uvicorn callers that own their ready boundary."""

    prepare_server_startup_report(app, runtime)
    print_prepared_server_startup_report()


def format_tool_registry_summary(registry: ToolRegistry) -> list[str]:
    """List registered tool names grouped by owning plugin for details mode."""

    grouped_names: dict[str, list[str]] = defaultdict(list)
    for record in registry.list_registration_records():
        grouped_names[record.plugin_id].append(record.tool_name)

    lines: list[str] = []
    for plugin_id in sorted(grouped_names):
        lines.append(f"  [{plugin_id}]")
        lines.extend(f"    {name}" for name in sorted(grouped_names[plugin_id]))
    return lines


def print_tool_registry_summary(registry: ToolRegistry) -> None:
    """Compatibility helper for callers that explicitly request tool details."""

    for line in format_tool_registry_summary(registry):
        print(line, flush=True)


def _tool_inventory(registry: ToolRegistry) -> ToolInventory:
    specs = registry.list_specs()
    records = registry.list_registration_records()
    categories = Counter(spec.category for spec in specs)
    source_types = Counter(
        source_type for source_type, _ in {
            (record.source_type, record.plugin_id) for record in records
        }
    )
    plugins = {record.plugin_id for record in records}
    return ToolInventory(
        total=len(specs),
        sealed=registry.sealed,
        category_counts=tuple(
            (name, categories.get(name, 0)) for name in _TOOL_CATEGORY_ORDER
        ),
        source_counts=tuple(sorted(source_types.items())),
        plugin_count=len(plugins),
    )


def _runtime_trace_path(runtime: Any) -> str | None:
    trace_store = getattr(runtime, "trace_store", None)
    candidates = [trace_store, *getattr(trace_store, "secondaries", ())]
    for candidate in candidates:
        sink = getattr(candidate, "sink", candidate)
        path = getattr(sink, "path", None)
        if path is not None:
            return str(Path(path).resolve())
    return None


def _route_inventory(app: Any) -> RouteInventory:
    http_count = 0
    websocket_count = 0
    health_path: str | None = None
    for route in getattr(app, "routes", ()):
        path = str(getattr(route, "path", ""))
        if hasattr(route, "methods"):
            http_count += 1
            if getattr(route, "name", None) == "health":
                health_path = path
        elif route.__class__.__name__.endswith("WebSocketRoute"):
            websocket_count += 1
    return RouteInventory(
        http_count=http_count,
        websocket_count=websocket_count,
        health_path=health_path,
    )


def _worker_statuses(app: Any, config: Any) -> tuple[WorkerStatus, ...]:
    return (
        _worker_status(
            app,
            name="durable-task",
            enabled=bool(getattr(config, "durable_task_worker_enabled", False)),
            task_attribute="durable_task_worker_task",
        ),
        _worker_status(
            app,
            name="workflow",
            enabled=bool(getattr(config, "durable_workflow_worker_enabled", False)),
            task_attribute="durable_workflow_worker_task",
        ),
    )


def _worker_status(
    app: Any,
    *,
    name: str,
    enabled: bool,
    task_attribute: str,
) -> WorkerStatus:
    if not enabled:
        return WorkerStatus(name=name, state="disabled")
    task = getattr(getattr(app, "state", object()), task_attribute, None)
    if task is None or task.done():
        return WorkerStatus(name=name, state="unavailable")
    return WorkerStatus(name=name, state="running")


def _report_warnings(report: ServerStartupReport) -> list[str]:
    warnings = [
        f"{item.name} is configured but unavailable."
        for item in report.dependencies
        if item.state == "unavailable"
    ]
    warnings.extend(
        f"{item.name} worker is enabled but not running."
        for item in report.workers
        if item.state == "unavailable"
    )
    if _network_accessible(report.bind_host) and not report.auth_bound_identity_required:
        warnings.append("Service is network-accessible without requiring auth-bound identity.")
    if report.provider_protocol_capture_enabled:
        warnings.append("Provider protocol capture is enabled for local diagnostics.")
    if report.memory_trace_content_enabled:
        warnings.append("Memory trace content is enabled for local diagnostics.")
    return warnings


def _format_counts(counts: tuple[tuple[str, int], ...]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts)


def _format_workers(workers: tuple[WorkerStatus, ...]) -> str:
    return ", ".join(f"{item.name} {item.state}" for item in workers)


def _detail_suffix(detail: str | None) -> str:
    return f" ({detail})" if detail else ""


def _langfuse_export_state(
    dependencies: tuple[StartupDependencyStatus, ...],
) -> str:
    for dependency in dependencies:
        if dependency.name == "Langfuse" and dependency.detail == "export enabled":
            return "enabled"
    return "disabled"


def _enabled(value: bool) -> str:
    return "enabled" if value else "disabled"


def _env_enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def _clean_optional(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _safe_port(value: str | None, *, default: int) -> int:
    try:
        port = int(str(value or default))
    except ValueError:
        return default
    return port if 1 <= port <= 65535 else default


def _format_host_port(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{rendered_host}:{port}"


def _local_base_url(host: str, port: int) -> str:
    connect_host = "127.0.0.1" if _network_accessible(host) else host
    return f"http://{_format_host_port(connect_host, port)}"


def _network_accessible(host: str) -> bool:
    return host.strip().lower() in {"0.0.0.0", "::", "[::]"}
