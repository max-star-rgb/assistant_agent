"""Bounded, fail-open dependency checks for the server startup summary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal
from urllib.request import urlopen

from assistant_agent.config import ProviderConfig


DEFAULT_STARTUP_DEPENDENCY_TIMEOUT_SECONDS = 0.75
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
    timeout_seconds: float = DEFAULT_STARTUP_DEPENDENCY_TIMEOUT_SECONDS,
    probe: JsonHealthProbe | None = None,
) -> tuple[
    StartupDependencyStatus,
    StartupDependencyStatus,
]:
    """Probe configured local dependencies concurrently without blocking startup."""

    health_probe = probe or _read_json_health
    mem0_disabled = not (
        config.provider_mode == "real" and config.mem0_base_url
    )
    mem0_default = StartupDependencyStatus(
        name="Mem0",
        state="disabled" if mem0_disabled else "unavailable",
    )
    web_search_default = _web_search_status(config)

    checks: dict[str, Callable[[], StartupDependencyStatus]] = {}
    if not mem0_disabled:
        checks["mem0"] = lambda: _probe_mem0(
            config,
            timeout_seconds=timeout_seconds,
            probe=health_probe,
        )
    resolved = {
        "mem0": mem0_default,
        "web_search": web_search_default,
    }
    if checks:
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


def _web_search_status(config: ProviderConfig) -> StartupDependencyStatus:
    if config.provider_mode == "mock":
        return StartupDependencyStatus(name="Web search", state="disabled")
    if config.chat_provider != "qwen":
        return StartupDependencyStatus(name="Web search", state="disabled")
    ready = not config.resolved_chat_provider().missing_required_env()
    return StartupDependencyStatus(
        name="Web search",
        state="ready" if ready else "unavailable",
        detail="bailian native turbo",
    )


def _read_json_health(url: str, timeout_seconds: float) -> Mapping[str, Any]:
    with urlopen(url, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, Mapping):
        raise ValueError("dependency health response must be an object")
    return payload
