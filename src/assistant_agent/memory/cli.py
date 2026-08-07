"""Read-only diagnostics for Assistant Memory Plugin assembly."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from pydantic import SecretStr

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.plugins.assembly import (
    MemoryPluginAssemblyError,
    assemble_memory_plugins,
)
from assistant_agent.memory.plugins.builtin.mem0 import (
    default_memory_plugin_factories,
)
from assistant_agent.memory.plugins.config import (
    MemoryPluginConfigError,
    MemoryPluginsConfig,
    load_memory_plugins_config,
)
from assistant_agent.memory.plugins.contracts import (
    MemoryPluginBuildContext,
    MemoryPluginFactory,
    MemoryPluginIssue,
)
from assistant_agent.memory.plugins.media import ManagedMemoryMediaStore


_REPORT_SCHEMA_VERSION = "memory_plugin_assembly_v1"
_UNAVAILABLE = "unavailable"
_BUILTIN_MEM0_SOURCES = frozenset(
    {
        "builtin:mem0",
        "module:assistant_agent.memory.plugins.builtin.mem0",
    }
)


class _EnvironmentMemorySecretResolver:
    def __init__(self, source: Mapping[str, str]) -> None:
        self._source = source

    def resolve(self, reference: str) -> str:
        value = self._source.get(reference)
        if value is None:
            raise KeyError("memory_plugin_secret_missing")
        return value


def main(
    argv: list[str] | None = None,
    *,
    factory_overrides: Iterable[MemoryPluginFactory] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run one diagnostics command and return its process exit code."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "plugins":
        return _run_plugins(factory_overrides=factory_overrides, env=env)
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Assistant Memory Plugin assembly without runtime calls."
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "plugins",
        help="Parse configuration and inspect the selected Memory Plugin.",
    )
    return parser


def _run_plugins(
    *,
    factory_overrides: Iterable[MemoryPluginFactory] | None,
    env: Mapping[str, str] | None,
) -> int:
    source = os.environ if env is None else env
    active_slot: str | None = None
    try:
        provider_config = ProviderConfig.from_env(source)
        override_factories = (
            None if factory_overrides is None else tuple(factory_overrides)
        )
        plugin_config = _plugin_config(
            provider_config=provider_config,
            factory_overrides=override_factories,
            env=source,
        )
        active_slot = plugin_config.slot
        factories = _factories(
            provider_config=provider_config,
            plugin_config=plugin_config,
            factory_overrides=override_factories,
        )
        media_store = ManagedMemoryMediaStore(max_total_bytes=0)
        registry = assemble_memory_plugins(
            config=plugin_config,
            builtin_factories=factories,
            build_context=MemoryPluginBuildContext(
                provider_mode=provider_config.provider_mode,
                media_reader=media_store,
                artifact_writer=media_store,
                secret_resolver=_EnvironmentMemorySecretResolver(source),
                clock=lambda: datetime.now(timezone.utc),
            ),
        )
    except MemoryPluginAssemblyError as exc:
        _print_report(
            _failure_report(
                active_slot=exc.report.active_slot,
                issues=exc.report.issues,
            )
        )
        return 1
    except MemoryPluginConfigError as exc:
        _print_report(
            _failure_report(
                active_slot=active_slot,
                issues=(_issue(exc.code),),
            )
        )
        return 1
    except Exception:
        _print_report(
            _failure_report(
                active_slot=active_slot,
                issues=(_issue("memory_plugin_diagnostics_failed"),),
            )
        )
        return 1

    report = registry.assembly_report
    selected = next(record for record in report.records if record.active)
    readiness, readiness_issues = _readiness(
        provider_config=provider_config,
        plugin_config=plugin_config,
        selected_source=selected.source,
    )
    _print_report(
        {
            "schema_version": _REPORT_SCHEMA_VERSION,
            "active_slot": report.active_slot,
            "descriptor": _descriptor_payload(selected.descriptor),
            "source": selected.source,
            "selected": True,
            "readiness": readiness,
            "issues": [
                issue.model_dump(mode="json")
                for issue in (*report.issues, *readiness_issues)
            ],
            "generation": registry.generation,
            "sealed": True,
        }
    )
    return 0


def _plugin_config(
    *,
    provider_config: ProviderConfig,
    factory_overrides: tuple[MemoryPluginFactory, ...] | None,
    env: Mapping[str, str],
) -> MemoryPluginsConfig:
    if provider_config.memory_plugin_config_path:
        return load_memory_plugins_config(
            provider_config.memory_plugin_config_path,
            env=env,
        )
    if factory_overrides is not None:
        if len(factory_overrides) != 1:
            raise MemoryPluginConfigError(
                "memory_plugin_factory_override_invalid"
            ) from None
        try:
            slot = factory_overrides[0].descriptor.plugin_id
        except Exception:
            raise MemoryPluginConfigError(
                "memory_plugin_descriptor_invalid"
            ) from None
        return MemoryPluginsConfig(
            schema_version="assistant_memory_plugins_v1",
            slot=slot,
            plugins={},
        )
    return MemoryPluginsConfig(
        schema_version="assistant_memory_plugins_v1",
        slot="mem0",
        plugins={},
    )


def _factories(
    *,
    provider_config: ProviderConfig,
    plugin_config: MemoryPluginsConfig,
    factory_overrides: tuple[MemoryPluginFactory, ...] | None,
) -> tuple[MemoryPluginFactory, ...]:
    if factory_overrides is not None:
        return factory_overrides
    return tuple(
        factory
        for factory in default_memory_plugin_factories(provider_config)
        if factory.descriptor.plugin_id not in plugin_config.plugins
    )


def _readiness(
    *,
    provider_config: ProviderConfig,
    plugin_config: MemoryPluginsConfig,
    selected_source: str,
) -> tuple[str, tuple[MemoryPluginIssue, ...]]:
    if selected_source not in _BUILTIN_MEM0_SOURCES:
        return "ready", ()
    if provider_config.provider_mode != "real":
        return _UNAVAILABLE, (_issue("memory_plugin_offline"),)
    if not _mem0_base_url_configured(provider_config, plugin_config):
        return _UNAVAILABLE, (_issue("memory_plugin_unconfigured"),)
    return "ready", ()


def _mem0_base_url_configured(
    provider_config: ProviderConfig,
    plugin_config: MemoryPluginsConfig,
) -> bool:
    configured_entry = plugin_config.plugins.get("mem0")
    value: object = provider_config.mem0_base_url
    if configured_entry is not None and "base_url" in configured_entry.config:
        value = configured_entry.config["base_url"]
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return isinstance(value, str) and bool(value.strip())


def _descriptor_payload(descriptor: Any) -> dict[str, Any]:
    payload = descriptor.model_dump(mode="json")
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, dict):
        modalities = capabilities.get("modalities")
        if isinstance(modalities, list):
            capabilities["modalities"] = sorted(modalities)
    return payload


def _failure_report(
    *,
    active_slot: str | None,
    issues: Iterable[MemoryPluginIssue],
) -> dict[str, object]:
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "active_slot": active_slot,
        "descriptor": None,
        "source": None,
        "selected": False,
        "readiness": _UNAVAILABLE,
        "issues": [issue.model_dump(mode="json") for issue in issues],
        "generation": None,
        "sealed": False,
    }


def _issue(code: str) -> MemoryPluginIssue:
    return MemoryPluginIssue(
        code=code,
        message=code,
        recoverable=False,
    )


def _print_report(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
