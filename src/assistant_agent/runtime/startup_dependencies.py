"""Bounded, fail-open dependency checks for the server startup summary."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal
from urllib.request import urlopen

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.langfuse_config import langfuse_host_from_env
from assistant_agent.observability.otel_exporter import OtlpHttpTextExporterConfig
from assistant_agent.providers.provider_config_validation import (
    qwen_native_web_search_issues,
)


DEFAULT_STARTUP_DEPENDENCY_TIMEOUT_SECONDS = 0.75
LANGFUSE_HEALTH_PATH = "/api/public/health"
MEM0_READY_PATH = "/ready"

DependencyState = Literal["disabled", "ready", "unavailable"]
JsonHealthProbe = Callable[[str, float], Mapping[str, Any]]


@dataclass(frozen=True)
class StartupDependencyStatus:
    """One prompt-safe, operator-visible dependency status."""

    name: str
    state: DependencyState
    detail: str | None = None

    def format(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"  {self.name}: {self.state}{suffix}"


def collect_startup_dependency_statuses(
    config: ProviderConfig,
    *,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = DEFAULT_STARTUP_DEPENDENCY_TIMEOUT_SECONDS,
    probe: JsonHealthProbe | None = None,
) -> tuple[
    StartupDependencyStatus,
    StartupDependencyStatus,
    StartupDependencyStatus,
]:
    """Probe configured local dependencies concurrently without blocking startup."""

    values = os.environ if env is None else env
    health_probe = probe or _read_json_health
    mem0_disabled = not (
        config.provider_mode == "real" and config.mem0_base_url
    )
    otel_export_enabled = OtlpHttpTextExporterConfig.from_env(values).enabled

    mem0_default = StartupDependencyStatus(
        name="Mem0",
        state="disabled" if mem0_disabled else "unavailable",
    )
    langfuse_default = StartupDependencyStatus(
        name="Langfuse",
        state="unavailable" if otel_export_enabled else "disabled",
        detail=_langfuse_export_detail(otel_export_enabled) if otel_export_enabled else None,
    )
    web_search_default = _web_search_status(config)

    checks: dict[str, Callable[[], StartupDependencyStatus]] = {
        "langfuse": lambda: _probe_langfuse(
            values,
            timeout_seconds=timeout_seconds,
            probe=health_probe,
            export_enabled=otel_export_enabled,
        ),
    }
    if not mem0_disabled:
        checks["mem0"] = lambda: _probe_mem0(
            config,
            timeout_seconds=timeout_seconds,
            probe=health_probe,
        )
    resolved = {
        "mem0": mem0_default,
        "langfuse": langfuse_default,
        "web_search": web_search_default,
    }
    with ThreadPoolExecutor(
        max_workers=len(checks),
        thread_name_prefix="startup-dependency",
    ) as executor:
        futures = {
            name: executor.submit(check)
            for name, check in checks.items()
        }
        for name, future in futures.items():
            try:
                resolved[name] = future.result()
            except Exception:
                # Startup dependency reporting is diagnostic and must stay fail-open.
                continue
    return (
        resolved["mem0"],
        resolved["langfuse"],
        resolved["web_search"],
    )


def format_startup_dependency_statuses(
    statuses: tuple[StartupDependencyStatus, ...],
) -> list[str]:
    """Format a compact dependency block for the launcher console."""

    return ["Dependencies:", *(status.format() for status in statuses)]


def _probe_mem0(
    config: ProviderConfig,
    *,
    timeout_seconds: float,
    probe: JsonHealthProbe,
) -> StartupDependencyStatus:
    if not config.mem0_base_url:
        return StartupDependencyStatus(name="Mem0", state="unavailable")
    payload = probe(
        f"{config.mem0_base_url.rstrip('/')}{MEM0_READY_PATH}",
        timeout_seconds,
    )
    if str(payload.get("status") or "").lower() != "ok":
        return StartupDependencyStatus(name="Mem0", state="unavailable")
    framework = str(payload.get("framework") or "mem0")
    version = str(payload.get("version") or "")
    detail = " ".join(part for part in (framework, version) if part)
    return StartupDependencyStatus(
        name="Mem0",
        state="ready",
        detail=detail or None,
    )


def _probe_langfuse(
    env: Mapping[str, str],
    *,
    timeout_seconds: float,
    probe: JsonHealthProbe,
    export_enabled: bool,
) -> StartupDependencyStatus:
    host = langfuse_host_from_env(env).rstrip("/")
    payload = probe(f"{host}{LANGFUSE_HEALTH_PATH}", timeout_seconds)
    if str(payload.get("status") or "").lower() != "ok":
        state: DependencyState = "unavailable" if export_enabled else "disabled"
    else:
        state = "ready"
    return StartupDependencyStatus(
        name="Langfuse",
        state=state,
        detail=_langfuse_export_detail(export_enabled) if state != "disabled" else None,
    )


def _web_search_status(config: ProviderConfig) -> StartupDependencyStatus:
    if config.provider_mode == "mock":
        return StartupDependencyStatus(
            name="Web search",
            state="ready",
            detail="mock",
        )
    if config.chat_provider != "qwen":
        return StartupDependencyStatus(name="Web search", state="disabled")
    ready = not (
        config.resolved_chat_provider().missing_required_env()
        or qwen_native_web_search_issues(config)
    )
    return StartupDependencyStatus(
        name="Web search",
        state="ready" if ready else "unavailable",
        detail="qwen native agent_max",
    )


def _langfuse_export_detail(enabled: bool) -> str:
    return f"export {'enabled' if enabled else 'disabled'}"


def _read_json_health(url: str, timeout_seconds: float) -> Mapping[str, Any]:
    with urlopen(url, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, Mapping):
        raise ValueError("dependency health response must be an object")
    return payload
