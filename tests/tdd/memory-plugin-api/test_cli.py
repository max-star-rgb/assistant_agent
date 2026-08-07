from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from assistant_agent.memory import cli as memory_cli
from assistant_agent.memory.plugins.contracts import (
    MemoryContextContribution,
    MemoryContextRequest,
    MemoryPluginBuildContext,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    MemorySessionCloseRequest,
    MemorySessionCloseResult,
    MemorySessionOpenRequest,
    MemorySessionOpenResult,
    MemoryTurnIngestionRequest,
    MemoryTurnIngestionResult,
)


class _ProbeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _RecordingPlugin:
    descriptor = MemoryPluginDescriptor(
        plugin_id="probe.memory",
        plugin_version="1.2.3",
        capabilities=MemoryPluginCapabilities(
            modalities={"text", "image"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=True,
            supports_idempotent_ingestion=True,
        ),
    )

    def __init__(self) -> None:
        self.open_calls = 0
        self.prepare_calls = 0
        self.ingest_calls = 0
        self.close_calls = 0

    def open_session(
        self,
        request: MemorySessionOpenRequest,
    ) -> MemorySessionOpenResult:
        self.open_calls += 1
        return MemorySessionOpenResult(status="ready")

    def prepare_context(
        self,
        request: MemoryContextRequest,
    ) -> MemoryContextContribution:
        self.prepare_calls += 1
        return MemoryContextContribution(status="succeeded")

    def ingest_turn(
        self,
        request: MemoryTurnIngestionRequest,
    ) -> MemoryTurnIngestionResult:
        self.ingest_calls += 1
        return MemoryTurnIngestionResult(status="accepted")

    def close_session(
        self,
        request: MemorySessionCloseRequest,
    ) -> MemorySessionCloseResult:
        self.close_calls += 1
        return MemorySessionCloseResult(status="closed")


class _RecordingFactory:
    descriptor = _RecordingPlugin.descriptor
    config_model = _ProbeConfig

    def __init__(self) -> None:
        self.plugin = _RecordingPlugin()

    def build(
        self,
        context: MemoryPluginBuildContext,
        config: BaseModel,
    ) -> _RecordingPlugin:
        return self.plugin


class _FailingFactory(_RecordingFactory):
    def build(
        self,
        context: MemoryPluginBuildContext,
        config: BaseModel,
    ) -> _RecordingPlugin:
        raise RuntimeError("secret-build-value")


def _payload(capsys) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return json.loads(capsys.readouterr().out)


def _assert_fixed_schema(payload: Mapping[str, object]) -> None:
    assert set(payload) == {
        "schema_version",
        "active_slot",
        "descriptor",
        "source",
        "selected",
        "readiness",
        "issues",
        "generation",
        "sealed",
    }
    assert payload["schema_version"] == "memory_plugin_assembly_v1"


def test_plugins_cli_reports_selected_plugin_without_runtime_calls(
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    factory = _RecordingFactory()

    exit_code = memory_cli.main(
        ["plugins"],
        factory_overrides=[factory],
        env={"MULTIMODAL_AGENT_PROVIDER_MODE": "mock"},
    )
    payload = _payload(capsys)

    _assert_fixed_schema(payload)
    assert exit_code == 0
    assert payload["active_slot"] == "probe.memory"
    assert payload["descriptor"] == {
        "api_version": "assistant_memory_plugin_v1",
        "capabilities": {
            "modalities": ["image", "text"],
            "supports_context_refresh": True,
            "supports_idempotent_ingestion": True,
            "supports_session_recall": True,
            "supports_turn_ingestion": True,
        },
        "kind": "memory",
        "plugin_id": "probe.memory",
        "plugin_version": "1.2.3",
    }
    assert payload["source"] == "builtin:probe.memory"
    assert payload["selected"] is True
    assert payload["readiness"] == "ready"
    assert payload["issues"] == []
    assert isinstance(payload["generation"], str)
    assert payload["sealed"] is True
    assert factory.plugin.open_calls == 0
    assert factory.plugin.prepare_calls == 0
    assert factory.plugin.ingest_calls == 0
    assert factory.plugin.close_calls == 0


def test_plugins_cli_reports_default_mock_mem0_as_offline_without_real_client(
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    def _unexpected_real_client(**kwargs: object) -> object:
        raise AssertionError("mock diagnostics must not construct Mem0Client")

    monkeypatch.setattr(
        "assistant_agent.memory.plugins.builtin.mem0.Mem0Client",
        _unexpected_real_client,
    )

    exit_code = memory_cli.main(
        ["plugins"],
        env={"MULTIMODAL_AGENT_PROVIDER_MODE": "mock"},
    )
    payload = _payload(capsys)

    _assert_fixed_schema(payload)
    assert exit_code == 0
    assert payload["active_slot"] == "mem0"
    assert payload["descriptor"]["plugin_id"] == "mem0"  # type: ignore[index]
    assert payload["source"] == "builtin:mem0"
    assert payload["selected"] is True
    assert payload["readiness"] == "unavailable"
    assert [issue["code"] for issue in payload["issues"]] == [  # type: ignore[index]
        "memory_plugin_offline"
    ]
    assert isinstance(payload["generation"], str)
    assert payload["sealed"] is True


def test_plugins_cli_reports_explicit_builtin_mem0_module_as_offline(
    tmp_path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    config_path = tmp_path / "memory-plugins.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_memory_plugins_v1",
                "slot": "mem0",
                "plugins": {
                    "mem0": {
                        "enabled": True,
                        "module": (
                            "assistant_agent.memory.plugins.builtin.mem0"
                        ),
                        "config": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = memory_cli.main(
        ["plugins"],
        env={
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "MULTIMODAL_AGENT_MEMORY_PLUGIN_CONFIG_PATH": str(config_path),
        },
    )
    payload = _payload(capsys)

    assert exit_code == 0
    assert payload["source"] == (
        "module:assistant_agent.memory.plugins.builtin.mem0"
    )
    assert payload["readiness"] == "unavailable"
    assert [issue["code"] for issue in payload["issues"]] == [  # type: ignore[index]
        "memory_plugin_offline"
    ]


def test_plugins_cli_failure_uses_same_schema_and_redacts_plugin_exception(
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    exit_code = memory_cli.main(
        ["plugins"],
        factory_overrides=[_FailingFactory()],
        env={"MULTIMODAL_AGENT_PROVIDER_MODE": "mock"},
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    _assert_fixed_schema(payload)
    assert exit_code == 1
    assert payload == {
        "schema_version": "memory_plugin_assembly_v1",
        "active_slot": "probe.memory",
        "descriptor": None,
        "source": None,
        "selected": False,
        "readiness": "unavailable",
        "issues": [
            {
                "code": "memory_plugin_build_failed",
                "message": "memory_plugin_build_failed",
                "recoverable": False,
                "retry_after_seconds": None,
            }
        ],
        "generation": None,
        "sealed": False,
    }
    assert "secret-build-value" not in output
    assert "config" not in payload
