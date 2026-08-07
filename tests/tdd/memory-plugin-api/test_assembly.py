import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from types import ModuleType
from typing import ClassVar

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
from assistant_agent.memory.plugins.registry import (
    MemoryPluginRegistrationRecord,
    MemoryPluginRegistry,
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


class _DescriptorGetterFactory:
    config_model = _FactoryConfig

    def __init__(
        self,
        *,
        descriptor: MemoryPluginDescriptor | None = None,
        descriptor_error: Exception | None = None,
        plugin: object | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._descriptor_error = descriptor_error
        self._plugin = plugin

    @property
    def descriptor(self) -> MemoryPluginDescriptor:
        if self._descriptor_error is not None:
            raise self._descriptor_error
        assert self._descriptor is not None
        return self._descriptor

    def build(self, context: MemoryPluginBuildContext, config: BaseModel) -> object:
        return self._plugin or _Plugin(self.descriptor)


class _OneShotDescriptorPlugin:
    def __init__(
        self,
        descriptor: MemoryPluginDescriptor,
        *,
        first_error: Exception | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._first_error = first_error
        self._descriptor_reads = 0

    @property
    def descriptor(self) -> MemoryPluginDescriptor:
        self._descriptor_reads += 1
        if self._first_error is not None:
            raise self._first_error
        if self._descriptor_reads > 1:
            raise RuntimeError("repeated-descriptor-secret-sentinel")
        return self._descriptor


class _GuardedExportModule(ModuleType):
    def __init__(
        self,
        name: str,
        factory: object,
        *,
        getter_error: Exception | None = None,
    ) -> None:
        super().__init__(name)
        self._factory = factory
        self._getter_error = getter_error
        self.export_reads = 0

    @property
    def __assistant_memory_plugin_factory__(self) -> object:
        self.export_reads += 1
        if self._getter_error is not None:
            raise self._getter_error
        if self.export_reads > 1:
            raise RuntimeError("repeated-export-secret-sentinel")
        return self._factory


class _ConfigModelGetterFactory:
    def __init__(
        self,
        descriptor: MemoryPluginDescriptor,
        *,
        config_model: type[BaseModel] = _FactoryConfig,
        getter_error: Exception | None = None,
    ) -> None:
        self.descriptor = descriptor
        self._config_model = config_model
        self._getter_error = getter_error
        self.config_model_reads = 0

    @property
    def config_model(self) -> type[BaseModel]:
        self.config_model_reads += 1
        if self._getter_error is not None:
            raise self._getter_error
        if self.config_model_reads > 1:
            raise RuntimeError("repeated-config-model-secret-sentinel")
        return self._config_model

    def build(self, context: MemoryPluginBuildContext, config: BaseModel) -> _Plugin:
        return _Plugin(self.descriptor)


class _BuildGetterFactory:
    config_model = _FactoryConfig

    def __init__(
        self,
        descriptor: MemoryPluginDescriptor,
        *,
        getter_error: Exception | None = None,
    ) -> None:
        self.descriptor = descriptor
        self._getter_error = getter_error
        self.build_reads = 0

    @property
    def build(self):  # type: ignore[no-untyped-def]
        self.build_reads += 1
        if self._getter_error is not None:
            raise self._getter_error
        if self.build_reads > 1:
            raise RuntimeError("repeated-build-getter-secret-sentinel")
        return self._build_plugin

    def _build_plugin(
        self,
        context: MemoryPluginBuildContext,
        config: BaseModel,
    ) -> _Plugin:
        return _Plugin(self.descriptor)


class _OneShotValidationConfig(BaseModel):
    validation_calls: ClassVar[int] = 0

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):  # type: ignore[no-untyped-def]
        cls.validation_calls += 1
        if cls.validation_calls > 1:
            raise RuntimeError("repeated-config-validation-secret-sentinel")
        return super().model_validate(obj, *args, **kwargs)


class _ExplodingValidationConfig(BaseModel):
    @classmethod
    def model_validate(cls, obj, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("config-validation-secret-sentinel")


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


def _capture_assembly_failure(
    *,
    config: MemoryPluginsConfig,
    builtin_factories: tuple[object, ...] = (),
) -> tuple[MemoryPluginAssemblyError, str]:
    try:
        assemble_memory_plugins(
            config=config,
            builtin_factories=builtin_factories,
            build_context=_build_context(),
        )
    except MemoryPluginAssemblyError as error:
        return error, traceback.format_exc()
    pytest.fail("assembly unexpectedly succeeded")


def _assert_sanitized_assembly_failure(
    error: MemoryPluginAssemblyError,
    rendered_traceback: str,
    *,
    code: str,
    secret: str,
) -> None:
    assert error.report.issues[0].code == code
    assert error.__suppress_context__ is True
    assert secret not in str(error.report)
    assert secret not in str(error)
    assert secret not in rendered_traceback


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


def test_config_resolves_nested_dict_and_list_secret_references(tmp_path) -> None:
    path = tmp_path / "memory_plugins.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": "probe.memory",
                "plugins": {
                    "probe.memory": _configured_plugin(
                        "probe_memory_plugin",
                        config={
                            "credentials": {"api_key": "${NESTED_MEMORY_API_KEY}"},
                            "tokens": ["plain-token", "${LIST_MEMORY_API_KEY}"],
                        },
                    )
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_memory_plugins_config(
        path,
        env={
            "NESTED_MEMORY_API_KEY": "nested-secret-sentinel",
            "LIST_MEMORY_API_KEY": "list-secret-sentinel",
        },
    )

    plugin_config = config.plugins["probe.memory"].config
    assert plugin_config["credentials"]["api_key"].get_secret_value() == "nested-secret-sentinel"
    assert plugin_config["tokens"] == ["plain-token", SecretStr("list-secret-sentinel")]


def test_config_missing_nested_secret_fails_without_echoing_reference(tmp_path) -> None:
    path = tmp_path / "memory_plugins.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": "probe.memory",
                "plugins": {
                    "probe.memory": _configured_plugin(
                        "probe_memory_plugin",
                        config={"credentials": {"api_key": "${MISSING_NESTED_SECRET}"}},
                    )
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MemoryPluginConfigError) as exc_info:
        load_memory_plugins_config(path, env={})

    assert exc_info.value.code == "memory_plugin_secret_missing"
    assert "MISSING_NESTED_SECRET" not in str(exc_info.value)


def test_config_leaves_non_reference_interpolation_string_literal(tmp_path) -> None:
    path = tmp_path / "memory_plugins.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": "probe.memory",
                "plugins": {
                    "probe.memory": _configured_plugin(
                        "probe_memory_plugin",
                        config={"template": "prefix ${NOT_A_SECRET} suffix"},
                    )
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_memory_plugins_config(path, env={"NOT_A_SECRET": "secret-sentinel"})

    assert config.plugins["probe.memory"].config["template"] == "prefix ${NOT_A_SECRET} suffix"


def test_config_rejects_duplicate_json_plugin_keys_without_echoing_values(tmp_path) -> None:
    path = tmp_path / "memory_plugins.json"
    path.write_text(
        """{
            "schema_version": "assistant_memory_plugins_v1",
            "slot": "probe.memory",
            "plugins": {
                "probe.memory": {"module": "first", "config": {"api_key": "first-secret"}},
                "probe.memory": {"module": "second", "config": {"api_key": "second-secret"}}
            }
        }""",
        encoding="utf-8",
    )

    with pytest.raises(MemoryPluginConfigError) as exc_info:
        load_memory_plugins_config(path, env={})

    assert exc_info.value.code == "memory_plugin_config_duplicate_key"
    assert "first-secret" not in str(exc_info.value)
    assert "second-secret" not in str(exc_info.value)


@pytest.mark.parametrize("slot", ["", "x" * 129], ids=["empty", "too-long"])
def test_config_rejects_invalid_slot_as_a_safe_domain_error(tmp_path, slot: str) -> None:
    path = tmp_path / "memory_plugins.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": slot,
                "plugins": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MemoryPluginConfigError) as exc_info:
        load_memory_plugins_config(path, env={})

    assert exc_info.value.code == "memory_plugin_slot_invalid"
    assert "ValidationError" not in str(exc_info.value)


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


@pytest.mark.parametrize("slot", ["", "x" * 129], ids=["empty", "too-long"])
def test_assembly_rejects_bypassed_invalid_slot_as_a_domain_error(slot: str) -> None:
    config = MemoryPluginsConfig.model_construct(
        schema_version="assistant_memory_plugins_v1",
        slot=slot,
        plugins={},
    )

    with pytest.raises(MemoryPluginAssemblyError) as exc_info:
        assemble_memory_plugins(
            config=config,
            builtin_factories=(),
            build_context=_build_context(),
        )

    assert exc_info.value.report.issues[0].code == "memory_plugin_slot_invalid"
    assert "ValidationError" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("api_version", "unsupported_api"), ("kind", "not_memory")],
    ids=["api-version", "kind"],
)
def test_factory_rejects_model_constructed_invalid_descriptor(
    field: str,
    value: str,
) -> None:
    valid_descriptor = _descriptor()
    descriptor = MemoryPluginDescriptor.model_construct(
        plugin_id=valid_descriptor.plugin_id,
        plugin_version=valid_descriptor.plugin_version,
        api_version=value if field == "api_version" else valid_descriptor.api_version,
        kind=value if field == "kind" else valid_descriptor.kind,
        capabilities=valid_descriptor.capabilities,
    )
    module_name = f"test_memory_plugin_invalid_factory_{field}"
    sys.modules[module_name] = _module(module_name, _Factory(descriptor))
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

    assert exc_info.value.report.issues[0].code == "memory_plugin_descriptor_invalid"


def test_factory_rejects_descriptor_with_mutated_invalid_modality() -> None:
    descriptor = _descriptor()
    descriptor.capabilities.modalities.add("untrusted_modality")
    module_name = "test_memory_plugin_invalid_modality"
    sys.modules[module_name] = _module(module_name, _Factory(descriptor))
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

    assert exc_info.value.report.issues[0].code == "memory_plugin_descriptor_invalid"


def test_assembly_rejects_invalid_descriptor_returned_by_plugin_build() -> None:
    valid_descriptor = _descriptor()
    invalid_built_descriptor = MemoryPluginDescriptor.model_construct(
        plugin_id=valid_descriptor.plugin_id,
        plugin_version=valid_descriptor.plugin_version,
        api_version=valid_descriptor.api_version,
        kind="not_memory",
        capabilities=valid_descriptor.capabilities,
    )
    module_name = "test_memory_plugin_invalid_built_descriptor"
    sys.modules[module_name] = _module(
        module_name,
        _Factory(valid_descriptor, built_descriptor=invalid_built_descriptor),
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

    assert exc_info.value.report.issues[0].code == "memory_plugin_descriptor_invalid"


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


def test_module_factory_export_is_read_once_and_snapshotted(monkeypatch) -> None:
    module_name = "test_memory_plugin_one_shot_export"
    module = _GuardedExportModule(module_name, _Factory(_descriptor()))
    monkeypatch.setitem(sys.modules, module_name, module)

    registry = assemble_memory_plugins(
        config=_config(
            slot="probe.memory",
            plugins={"probe.memory": _configured_plugin(module_name)},
        ),
        builtin_factories=(),
        build_context=_build_context(),
    )

    assert registry.assembly_report.active_slot == "probe.memory"
    assert module.export_reads == 1


def test_module_export_getter_failure_is_domainized_and_sanitized(monkeypatch) -> None:
    module_name = "test_memory_plugin_export_getter_failure"
    secret = "module-export-secret-sentinel"
    monkeypatch.setitem(
        sys.modules,
        module_name,
        _GuardedExportModule(
            module_name,
            _Factory(_descriptor()),
            getter_error=RuntimeError(secret),
        ),
    )

    error, rendered_traceback = _capture_assembly_failure(
        config=_config(
            slot="probe.memory",
            plugins={"probe.memory": _configured_plugin(module_name)},
        )
    )

    _assert_sanitized_assembly_failure(
        error,
        rendered_traceback,
        code="memory_plugin_export_missing",
        secret=secret,
    )


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


def test_plugin_build_failure_suppresses_secret_exception_traceback() -> None:
    module_name = "test_memory_plugin_build_failure_traceback"
    sys.modules[module_name] = _module(
        module_name,
        _Factory(_descriptor(), build_error=RuntimeError("build-secret-sentinel")),
    )
    try:
        try:
            assemble_memory_plugins(
                config=_config(
                    slot="probe.memory",
                    plugins={"probe.memory": _configured_plugin(module_name)},
                ),
                builtin_factories=(),
                build_context=_build_context(),
            )
        except MemoryPluginAssemblyError as error:
            assembly_error = error
            rendered_traceback = traceback.format_exc()
        else:
            pytest.fail("assembly unexpectedly succeeded")
    finally:
        sys.modules.pop(module_name, None)

    assert assembly_error.report.issues[0].code == "memory_plugin_build_failed"
    assert assembly_error.__suppress_context__ is True
    assert "build-secret-sentinel" not in rendered_traceback


def test_factory_descriptor_getter_failure_is_domainized_and_sanitized() -> None:
    module_name = "test_memory_plugin_factory_descriptor_getter_failure"
    sys.modules[module_name] = _module(
        module_name,
        _DescriptorGetterFactory(
            descriptor_error=RuntimeError("factory-descriptor-secret-sentinel")
        ),
    )
    try:
        try:
            assemble_memory_plugins(
                config=_config(
                    slot="probe.memory",
                    plugins={"probe.memory": _configured_plugin(module_name)},
                ),
                builtin_factories=(),
                build_context=_build_context(),
            )
        except MemoryPluginAssemblyError as error:
            assembly_error = error
            rendered_traceback = traceback.format_exc()
        else:
            pytest.fail("assembly unexpectedly succeeded")
    finally:
        sys.modules.pop(module_name, None)

    assert assembly_error.report.issues[0].code == "memory_plugin_descriptor_invalid"
    assert "factory-descriptor-secret-sentinel" not in str(assembly_error.report)
    assert "factory-descriptor-secret-sentinel" not in rendered_traceback


def test_factory_config_model_is_read_once_and_snapshotted(monkeypatch) -> None:
    module_name = "test_memory_plugin_one_shot_config_model"
    factory = _ConfigModelGetterFactory(_descriptor())
    monkeypatch.setitem(sys.modules, module_name, _module(module_name, factory))

    registry = assemble_memory_plugins(
        config=_config(
            slot="probe.memory",
            plugins={"probe.memory": _configured_plugin(module_name)},
        ),
        builtin_factories=(),
        build_context=_build_context(),
    )

    assert registry.assembly_report.active_slot == "probe.memory"
    assert factory.config_model_reads == 1


def test_factory_config_model_getter_failure_is_domainized_and_sanitized(
    monkeypatch,
) -> None:
    module_name = "test_memory_plugin_config_model_getter_failure"
    secret = "config-model-getter-secret-sentinel"
    factory = _ConfigModelGetterFactory(
        _descriptor(),
        getter_error=RuntimeError(secret),
    )
    monkeypatch.setitem(sys.modules, module_name, _module(module_name, factory))

    error, rendered_traceback = _capture_assembly_failure(
        config=_config(
            slot="probe.memory",
            plugins={"probe.memory": _configured_plugin(module_name)},
        )
    )

    _assert_sanitized_assembly_failure(
        error,
        rendered_traceback,
        code="memory_plugin_factory_invalid",
        secret=secret,
    )


def test_factory_build_callable_is_read_once_and_snapshotted(monkeypatch) -> None:
    module_name = "test_memory_plugin_one_shot_build_getter"
    factory = _BuildGetterFactory(_descriptor())
    monkeypatch.setitem(sys.modules, module_name, _module(module_name, factory))

    registry = assemble_memory_plugins(
        config=_config(
            slot="probe.memory",
            plugins={"probe.memory": _configured_plugin(module_name)},
        ),
        builtin_factories=(),
        build_context=_build_context(),
    )

    assert registry.assembly_report.active_slot == "probe.memory"
    assert factory.build_reads == 1


def test_factory_build_getter_failure_is_domainized_and_sanitized(monkeypatch) -> None:
    module_name = "test_memory_plugin_build_getter_failure"
    secret = "build-getter-secret-sentinel"
    factory = _BuildGetterFactory(
        _descriptor(),
        getter_error=RuntimeError(secret),
    )
    monkeypatch.setitem(sys.modules, module_name, _module(module_name, factory))

    error, rendered_traceback = _capture_assembly_failure(
        config=_config(
            slot="probe.memory",
            plugins={"probe.memory": _configured_plugin(module_name)},
        )
    )

    _assert_sanitized_assembly_failure(
        error,
        rendered_traceback,
        code="memory_plugin_factory_invalid",
        secret=secret,
    )


def test_factory_config_validation_is_called_once_and_snapshotted(monkeypatch) -> None:
    module_name = "test_memory_plugin_one_shot_config_validation"
    factory = _Factory(_descriptor())
    factory.config_model = _OneShotValidationConfig
    _OneShotValidationConfig.validation_calls = 0
    monkeypatch.setitem(sys.modules, module_name, _module(module_name, factory))

    registry = assemble_memory_plugins(
        config=_config(
            slot="probe.memory",
            plugins={"probe.memory": _configured_plugin(module_name)},
        ),
        builtin_factories=(),
        build_context=_build_context(),
    )

    assert registry.assembly_report.active_slot == "probe.memory"
    assert _OneShotValidationConfig.validation_calls == 1


def test_factory_config_validation_failure_is_domainized_and_sanitized(
    monkeypatch,
) -> None:
    module_name = "test_memory_plugin_config_validation_failure"
    secret = "config-validation-secret-sentinel"
    factory = _Factory(_descriptor())
    factory.config_model = _ExplodingValidationConfig
    monkeypatch.setitem(sys.modules, module_name, _module(module_name, factory))

    error, rendered_traceback = _capture_assembly_failure(
        config=_config(
            slot="probe.memory",
            plugins={"probe.memory": _configured_plugin(module_name)},
        )
    )

    _assert_sanitized_assembly_failure(
        error,
        rendered_traceback,
        code="memory_plugin_config_invalid",
        secret=secret,
    )


def test_plugin_descriptor_getter_failure_is_domainized_and_sanitized() -> None:
    module_name = "test_memory_plugin_descriptor_getter_failure"
    descriptor = _descriptor()
    plugin = _OneShotDescriptorPlugin(
        descriptor,
        first_error=RuntimeError("plugin-descriptor-secret-sentinel"),
    )
    sys.modules[module_name] = _module(
        module_name,
        _DescriptorGetterFactory(descriptor=descriptor, plugin=plugin),
    )
    try:
        try:
            assemble_memory_plugins(
                config=_config(
                    slot="probe.memory",
                    plugins={"probe.memory": _configured_plugin(module_name)},
                ),
                builtin_factories=(),
                build_context=_build_context(),
            )
        except MemoryPluginAssemblyError as error:
            assembly_error = error
            rendered_traceback = traceback.format_exc()
        else:
            pytest.fail("assembly unexpectedly succeeded")
    finally:
        sys.modules.pop(module_name, None)

    assert assembly_error.report.issues[0].code == "memory_plugin_descriptor_invalid"
    assert "plugin-descriptor-secret-sentinel" not in str(assembly_error.report)
    assert "plugin-descriptor-secret-sentinel" not in rendered_traceback


def test_registry_seals_with_validated_descriptor_snapshot_without_rereading_getter() -> None:
    module_name = "test_memory_plugin_descriptor_snapshot"
    descriptor = _descriptor()
    plugin = _OneShotDescriptorPlugin(descriptor)
    sys.modules[module_name] = _module(
        module_name,
        _DescriptorGetterFactory(descriptor=descriptor, plugin=plugin),
    )
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

    assert registry.active_plugin is plugin
    assert registry.assembly_report.active_slot == "probe.memory"


def test_registry_compatibility_path_rejects_mismatched_plugin_descriptor() -> None:
    descriptor = _descriptor()

    with pytest.raises(ValueError, match="memory_plugin_registry_invalid"):
        MemoryPluginRegistry(
            [
                MemoryPluginRegistrationRecord(
                    descriptor=descriptor,
                    source="builtin:probe.memory",
                    enabled=True,
                    active=True,
                )
            ],
            _Plugin(_descriptor("other.memory")),
        )


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


def test_registry_report_is_a_defensive_snapshot_of_mutable_capabilities() -> None:
    module_name = "test_memory_plugin_registry_snapshot"
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

    generation = registry.generation
    report = registry.assembly_report
    report.records[0].descriptor.capabilities.modalities.add("image")
    descriptor.capabilities.modalities.add("audio")

    sealed_report = registry.assembly_report
    assert sealed_report.records[0].descriptor.capabilities.modalities == {"text"}
    assert registry.generation == generation


def test_registry_generation_is_stable_for_differently_ordered_modalities() -> None:
    first_modalities: set[str] = set()
    first_modalities.update(["image", "text", "audio"])
    second_modalities: set[str] = set()
    second_modalities.update(["audio", "image", "text"])
    first_descriptor = MemoryPluginDescriptor(
        plugin_id="probe.memory",
        plugin_version="1",
        capabilities=MemoryPluginCapabilities(
            modalities=first_modalities,
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=True,
            supports_idempotent_ingestion=True,
        ),
    )
    second_descriptor = MemoryPluginDescriptor(
        plugin_id="probe.memory",
        plugin_version="1",
        capabilities=MemoryPluginCapabilities(
            modalities=second_modalities,
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=True,
            supports_idempotent_ingestion=True,
        ),
    )

    first_registry = assemble_memory_plugins(
        config=_config(slot="probe.memory", plugins={}),
        builtin_factories=(_Factory(first_descriptor),),
        build_context=_build_context(),
    )
    second_registry = assemble_memory_plugins(
        config=_config(slot="probe.memory", plugins={}),
        builtin_factories=(_Factory(second_descriptor),),
        build_context=_build_context(),
    )

    assert first_registry.generation == second_registry.generation


def test_registry_generation_is_stable_across_python_hash_seeds() -> None:
    script = """
from assistant_agent.memory.plugins.contracts import (
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
)
from assistant_agent.memory.plugins.registry import (
    MemoryPluginRegistrationRecord,
    MemoryPluginRegistry,
)

class Plugin:
    def __init__(self, descriptor):
        self.descriptor = descriptor

descriptor = MemoryPluginDescriptor(
    plugin_id="probe.memory",
    plugin_version="1",
    capabilities=MemoryPluginCapabilities(
        modalities={"text", "image", "audio"},
        supports_session_recall=True,
        supports_turn_ingestion=True,
        supports_context_refresh=True,
        supports_idempotent_ingestion=True,
    ),
)
registry = MemoryPluginRegistry(
    [
        MemoryPluginRegistrationRecord(
            descriptor=descriptor,
            source="builtin:probe.memory",
            enabled=True,
            active=True,
        )
    ],
    Plugin(descriptor),
)
print(registry.generation)
"""
    root = Path(__file__).parents[3]
    generations = []
    for seed in ("1", "2"):
        output = subprocess.check_output(
            [sys.executable, "-c", script],
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(root / "src")},
        )
        generations.append(output.strip())

    assert generations[0] == generations[1]
