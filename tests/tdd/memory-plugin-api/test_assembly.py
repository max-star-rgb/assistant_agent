import json
import sys
from datetime import datetime, timezone
from types import ModuleType

import pytest
from pydantic import BaseModel, SecretStr

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.plugins.assembly import (
    MemoryPluginAssemblyError,
    assemble_memory_plugins,
)
from assistant_agent.memory.plugins.config import (
    MemoryPluginConfigError,
    MemoryPluginsConfig,
    load_memory_plugins_config,
)
from assistant_agent.memory.plugins.contracts import (
    MemoryContextContribution,
    MemoryPluginBuildContext,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    MemorySessionCloseResult,
    MemorySessionOpenResult,
    MemoryTurnIngestionResult,
)


class _FactoryConfig(BaseModel):
    api_key: SecretStr | None = None


class _Plugin:
    def __init__(self, descriptor: MemoryPluginDescriptor) -> None:
        self.descriptor = descriptor

    def open_session(self, request) -> MemorySessionOpenResult:  # type: ignore[no-untyped-def]
        return MemorySessionOpenResult(status="unavailable")

    def prepare_context(self, request) -> MemoryContextContribution:  # type: ignore[no-untyped-def]
        return MemoryContextContribution(status="unavailable")

    def ingest_turn(self, request) -> MemoryTurnIngestionResult:  # type: ignore[no-untyped-def]
        return MemoryTurnIngestionResult(status="failed")

    def close_session(self, request) -> MemorySessionCloseResult:  # type: ignore[no-untyped-def]
        return MemorySessionCloseResult(status="closed")


class _Factory:
    config_model = _FactoryConfig

    def __init__(
        self,
        descriptor: MemoryPluginDescriptor,
        *,
        built_descriptor: MemoryPluginDescriptor | None = None,
        build_error: Exception | None = None,
    ) -> None:
        self.descriptor = descriptor
        self._built_descriptor = built_descriptor or descriptor
        self._build_error = build_error

    def build(self, context: MemoryPluginBuildContext, config: BaseModel) -> _Plugin:
        if self._build_error is not None:
            raise self._build_error
        return _Plugin(self._built_descriptor)


def _descriptor(plugin_id: str = "probe.memory") -> MemoryPluginDescriptor:
    return MemoryPluginDescriptor(
        plugin_id=plugin_id,
        plugin_version="1",
        capabilities=MemoryPluginCapabilities(
            modalities={"text"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=True,
            supports_idempotent_ingestion=True,
        ),
    )


def _config(*, slot: str, plugins: dict) -> MemoryPluginsConfig:
    return MemoryPluginsConfig(
        schema_version="assistant_memory_plugins_v1",
        slot=slot,
        plugins=plugins,
    )


def _build_context() -> MemoryPluginBuildContext:
    return MemoryPluginBuildContext(
        provider_mode="mock",
        media_reader=object(),
        artifact_writer=object(),
        secret_resolver=object(),
        clock=lambda: datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


def _module(name: str, factory: object | None) -> ModuleType:
    module = ModuleType(name)
    if factory is not None:
        module.__assistant_memory_plugin_factory__ = factory
    return module


def _configured_plugin(module: str, *, enabled: bool = True, config: dict | None = None) -> dict:
    return {
        "enabled": enabled,
        "module": module,
        "config": config or {},
    }


def test_config_resolves_declared_env_reference_only(tmp_path) -> None:
    path = tmp_path / "memory_plugins.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": "probe.memory",
                "plugins": {
                    "probe.memory": _configured_plugin(
                        "probe_memory_plugin",
                        config={"api_key": "${PROBE_MEMORY_API_KEY}"},
                    )
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_memory_plugins_config(
        path,
        env={"PROBE_MEMORY_API_KEY": "secret-sentinel"},
    )

    assert config.slot == "probe.memory"
    assert config.plugins["probe.memory"].config["api_key"].get_secret_value() == "secret-sentinel"


def test_config_missing_secret_fails_without_echoing_value(tmp_path) -> None:
    path = tmp_path / "memory_plugins.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": "probe.memory",
                "plugins": {
                    "probe.memory": _configured_plugin(
                        "probe_memory_plugin",
                        config={"api_key": "${MISSING_MEMORY_SECRET}"},
                    )
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MemoryPluginConfigError) as exc_info:
        load_memory_plugins_config(path, env={})

    assert exc_info.value.code == "memory_plugin_secret_missing"
    assert "MISSING_MEMORY_SECRET" not in str(exc_info.value)


def test_provider_config_reads_only_explicit_memory_plugin_settings() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "MULTIMODAL_AGENT_MEMORY_PLUGIN_CONFIG_PATH": "/safe/plugins.json",
            "MULTIMODAL_AGENT_MEMORY_PLUGIN_OPEN_TIMEOUT_SECONDS": "2.5",
            "MULTIMODAL_AGENT_MEMORY_PLUGIN_PREPARE_TIMEOUT_SECONDS": "3.5",
            "MULTIMODAL_AGENT_MEMORY_PLUGIN_INGEST_TIMEOUT_SECONDS": "4.5",
            "MULTIMODAL_AGENT_MEMORY_PLUGIN_CLOSE_TIMEOUT_SECONDS": "5.5",
            "PROBE_MEMORY_API_KEY": "secret-sentinel",
        }
    )

    assert config.memory_plugin_config_path == "/safe/plugins.json"
    assert config.memory_plugin_open_timeout_seconds == 2.5
    assert config.memory_plugin_prepare_timeout_seconds == 3.5
    assert config.memory_plugin_ingest_timeout_seconds == 4.5
    assert config.memory_plugin_close_timeout_seconds == 5.5


def test_unknown_active_slot_fails_closed() -> None:
    with pytest.raises(MemoryPluginAssemblyError) as exc_info:
        assemble_memory_plugins(
            config=_config(slot="missing.memory", plugins={}),
            builtin_factories=(),
            build_context=_build_context(),
        )

    assert exc_info.value.report.issues[0].code == "memory_plugin_slot_unknown"


def test_duplicate_plugin_id_fails_before_active_plugin_is_constructed() -> None:
    descriptor = _descriptor()
    module_name = "test_memory_plugin_duplicate"
    module = _module(module_name, _Factory(descriptor))
    sys.modules[module_name] = module
    try:
        with pytest.raises(MemoryPluginAssemblyError) as exc_info:
            assemble_memory_plugins(
                config=_config(
                    slot="probe.memory",
                    plugins={"probe.memory": _configured_plugin(module_name)},
                ),
                builtin_factories=(_Factory(descriptor),),
                build_context=_build_context(),
            )
    finally:
        sys.modules.pop(module_name, None)

    assert exc_info.value.report.issues[0].code == "memory_plugin_duplicate_id"


def test_disabled_active_slot_fails_closed() -> None:
    descriptor = _descriptor()
    module_name = "test_memory_plugin_disabled"
    module = _module(module_name, _Factory(descriptor))
    sys.modules[module_name] = module
    try:
        with pytest.raises(MemoryPluginAssemblyError) as exc_info:
            assemble_memory_plugins(
                config=_config(
                    slot="probe.memory",
                    plugins={
                        "probe.memory": _configured_plugin(module_name, enabled=False)
                    },
                ),
                builtin_factories=(),
                build_context=_build_context(),
            )
    finally:
        sys.modules.pop(module_name, None)

    assert exc_info.value.report.issues[0].code == "memory_plugin_slot_disabled"


def test_module_without_declared_factory_export_fails_closed() -> None:
    module_name = "test_memory_plugin_missing_export"
    sys.modules[module_name] = _module(module_name, None)
    try:
        with pytest.raises(MemoryPluginAssemblyError) as exc_info:
            assemble_memory_plugins(
                config=_config(
                    slot="probe.memory",
                    plugins={"probe.memory": _configured_plugin(module_name)},
                ),
                builtin_factories=(),
                build_context=_build_context(),
            )
    finally:
        sys.modules.pop(module_name, None)

    assert exc_info.value.report.issues[0].code == "memory_plugin_export_missing"


def test_plugin_descriptor_must_match_its_factory_descriptor() -> None:
    module_name = "test_memory_plugin_descriptor_mismatch"
    factory = _Factory(_descriptor(), built_descriptor=_descriptor("other.memory"))
    sys.modules[module_name] = _module(module_name, factory)
    try:
        with pytest.raises(MemoryPluginAssemblyError) as exc_info:
            assemble_memory_plugins(
                config=_config(
                    slot="probe.memory",
                    plugins={"probe.memory": _configured_plugin(module_name)},
                ),
                builtin_factories=(),
                build_context=_build_context(),
            )
    finally:
        sys.modules.pop(module_name, None)

    assert exc_info.value.report.issues[0].code == "memory_plugin_descriptor_mismatch"


def test_plugin_build_failure_does_not_return_partial_registry() -> None:
    module_name = "test_memory_plugin_build_failure"
    sys.modules[module_name] = _module(
        module_name,
        _Factory(_descriptor(), build_error=RuntimeError("secret-sentinel")),
    )
    try:
        with pytest.raises(MemoryPluginAssemblyError) as exc_info:
            assemble_memory_plugins(
                config=_config(
                    slot="probe.memory",
                    plugins={"probe.memory": _configured_plugin(module_name)},
                ),
                builtin_factories=(),
                build_context=_build_context(),
            )
    finally:
        sys.modules.pop(module_name, None)

    assert exc_info.value.report.issues[0].code == "memory_plugin_build_failed"
    assert "secret-sentinel" not in str(exc_info.value.report)


def test_registry_exposes_only_the_selected_plugin_and_safe_generation() -> None:
    module_name = "test_memory_plugin_registry"
    descriptor = _descriptor()
    sys.modules[module_name] = _module(module_name, _Factory(descriptor))
    try:
        registry = assemble_memory_plugins(
            config=_config(
                slot="probe.memory",
                plugins={"probe.memory": _configured_plugin(module_name)},
            ),
            builtin_factories=(),
            build_context=_build_context(),
        )
    finally:
        sys.modules.pop(module_name, None)

    assert registry.active_plugin.descriptor == descriptor
    assert len(registry.assembly_report.records) == 1
    assert registry.assembly_report.active_slot == "probe.memory"
    assert len(registry.generation) == 64
    assert "secret-sentinel" not in registry.assembly_report.model_dump_json()
