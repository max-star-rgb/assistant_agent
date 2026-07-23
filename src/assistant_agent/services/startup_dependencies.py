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
from assistant_agent.services.langfuse_config import langfuse_host_from_env
from assistant_agent.services.otel_exporter import OtlpHttpTextExporterConfig


DEFAULT_STARTUP_DEPENDENCY_TIMEOUT_SECONDS = 0.75
LANGFUSE_HEALTH_PATH = "/api/public/health"
MEMO_READY_PATH = "/ready"

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
) -> tuple[StartupDependencyStatus, StartupDependencyStatus]:
    """Probe configured local dependencies concurrently without blocking startup."""

    values = os.environ if env is None else env
    health_probe = probe or _read_json_health
    memo_disabled = not (
        config.memory_backend == "framework"
        and config.memory_framework == "mem0"
    )
    otel_export_enabled = OtlpHttpTextExporterConfig.from_env(values).enabled

    memo_default = StartupDependencyStatus(
        name="Memo",
        state="disabled" if memo_disabled else "unavailable",
    )
    langfuse_default = StartupDependencyStatus(
        name="Langfuse",
        state="unavailable" if otel_export_enabled else "disabled",
        detail=_langfuse_export_detail(otel_export_enabled) if otel_export_enabled else None,
    )

    checks: dict[str, Callable[[], StartupDependencyStatus]] = {
        "langfuse": lambda: _probe_langfuse(
            values,
            timeout_seconds=timeout_seconds,
            probe=health_probe,
            export_enabled=otel_export_enabled,
        ),
    }
    if not memo_disabled:
        checks["memo"] = lambda: _probe_memo(
            config,
            timeout_seconds=timeout_seconds,
            probe=health_probe,
        )

    resolved = {
        "memo": memo_default,
        "langfuse": langfuse_default,
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
    return resolved["memo"], resolved["langfuse"]


def format_startup_dependency_statuses(
    statuses: tuple[StartupDependencyStatus, ...],
) -> list[str]:
    """Format a compact dependency block for the launcher console."""

    return ["Dependencies:", *(status.format() for status in statuses)]


def _probe_memo(
    config: ProviderConfig,
    *,
    timeout_seconds: float,
    probe: JsonHealthProbe,
) -> StartupDependencyStatus:
    if not config.memory_framework_base_url:
        return StartupDependencyStatus(name="Memo", state="unavailable")
    payload = probe(
        f"{config.memory_framework_base_url.rstrip('/')}{MEMO_READY_PATH}",
        timeout_seconds,
    )
    if str(payload.get("status") or "").lower() != "ok":
        return StartupDependencyStatus(name="Memo", state="unavailable")
    framework = str(payload.get("framework") or config.memory_framework)
    version = str(payload.get("version") or config.memory_framework_version)
    detail = " ".join(part for part in (framework, version) if part)
    return StartupDependencyStatus(
        name="Memo",
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


def _langfuse_export_detail(enabled: bool) -> str:
    return f"export {'enabled' if enabled else 'disabled'}"


def _read_json_health(url: str, timeout_seconds: float) -> Mapping[str, Any]:
    with urlopen(url, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, Mapping):
        raise ValueError("dependency health response must be an object")
    return payload
