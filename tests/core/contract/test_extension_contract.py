from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import ModuleType

import pytest
from pydantic import BaseModel

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.plugins.assembly import assemble_memory_plugins
from assistant_agent.memory.plugins.config import MemoryPluginsConfig
from assistant_agent.memory.plugins.contracts import (
    MemoryContextContribution,
    MemoryContextItem,
    MemoryPluginBuildContext,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    MemorySessionCloseResult,
    MemorySessionOpenResult,
    MemoryTurnIngestionResult,
)
from assistant_agent.memory.plugins.host import MemoryPluginHost
from assistant_agent.memory.plugins.media import ManagedMemoryMediaStore
from assistant_agent.memory.plugins.registry import MemoryPluginRegistry
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.models import RunToolCatalog
from assistant_agent.tools.plugins.assembly import assemble_tool_plugins
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)
from assistant_agent.tools.registry import ToolRegistry
from tests.core.support import ProbeTool, offline_config


class ProbePlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="tests.probe",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[ProbeTool]:
        return [ProbeTool()]


class ProbeMemoryPluginConfig(BaseModel):
    pass


class ProbeMemoryPlugin:
    descriptor = MemoryPluginDescriptor(
        plugin_id="tests.memory_probe",
        plugin_version="1",
        capabilities=MemoryPluginCapabilities(
            modalities={"text"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=True,
            supports_idempotent_ingestion=True,
        ),
    )

    def open_session(self, request: object) -> MemorySessionOpenResult:
        return MemorySessionOpenResult(
            status="ready",
            initial_contribution=MemoryContextContribution(
                status="succeeded",
            ),
        )

    def prepare_context(self, request: object) -> MemoryContextContribution:
        return MemoryContextContribution(
            items=[
                MemoryContextItem(
                    memory_id="memory-sentinel",
                    text="value-sentinel",
                    source="semantic",
                )
            ],
            status="succeeded",
        )

    def ingest_turn(self, request: object) -> MemoryTurnIngestionResult:
        return MemoryTurnIngestionResult(status="accepted")

    def close_session(self, request: object) -> MemorySessionCloseResult:
        return MemorySessionCloseResult(status="closed")


class ProbeMemoryPluginFactory:
    descriptor = ProbeMemoryPlugin.descriptor
    config_model = ProbeMemoryPluginConfig

    def build(
        self,
        context: MemoryPluginBuildContext,
        config: BaseModel,
    ) -> ProbeMemoryPlugin:
        return ProbeMemoryPlugin()


class ProbeMemorySecretResolver:
    def resolve(self, reference: str) -> str:
        raise AssertionError("secret-resolver-called")


def _probe_memory_config() -> MemoryPluginsConfig:
    return MemoryPluginsConfig(
        schema_version="assistant_memory_plugins_v1",
        slot="tests.memory_probe",
        plugins={},
    )


def _offline_memory_build_context() -> MemoryPluginBuildContext:
    media_store = ManagedMemoryMediaStore(max_total_bytes=1024)
    return MemoryPluginBuildContext(
        provider_mode="mock",
        media_reader=media_store,
        artifact_writer=media_store,
        secret_resolver=ProbeMemorySecretResolver(),
        clock=lambda: datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


def _probe_memory_host(registry: MemoryPluginRegistry) -> MemoryPluginHost:
    return MemoryPluginHost(
        registry=registry,
        media_store=ManagedMemoryMediaStore(max_total_bytes=1024),
        clock=lambda: datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


def _probe_identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="user-sentinel",
        session_id="session-sentinel",
    )


def _probe_request() -> UserRequest:
    return UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="request-sentinel",
    )


@pytest.mark.core_invariant("EXT-001")
def test_probe_plugin_assembles_schema_and_executes_through_governed_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "tests_core_probe_plugin"
    module = ModuleType(module_name)
    module.__assistant_tool_plugin__ = ProbePlugin()
    monkeypatch.setitem(sys.modules, module_name, module)
    assembly = assemble_tool_plugins(
        ToolPluginContext(
            config=offline_config(),
            mcp_server_configs=[],
        ),
        builtin_plugins=(),
        configured_module_names=(module_name,),
    )
    registry = ToolRegistry()
    registry.register_many(assembly.contributions)
    registry.seal(assembly_report=assembly.report)

    spec = registry.get_spec(ProbeTool.name)
    registration = registry.registration_record(ProbeTool.name)
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="request-sentinel",
    )
    state = AgentState.from_request(request)
    state.run_tool_catalog = RunToolCatalog(
        available_tool_names=[ProbeTool.name]
    )
    decision = AssistantToolCall(
        tool_name=ProbeTool.name,
        tool_input={"value": "value-sentinel"},
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    assert validation.accepted is True
    assert validation.validated_input is not None
    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-sentinel",
        decision.tool_name,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert set(spec.input_schema["properties"]) == {"value"}
    assert spec.input_schema["required"] == ["value"]
    assert registration.plugin_id == "tests.probe"
    assert registration.plugin_version == "1"
    assert registration.source_type == "configured_module"
    assert result.success is True
    assert result.data == {"value": "value-sentinel"}


@pytest.mark.core_invariant("EXT-001")
def test_probe_memory_plugin_assembles_single_slot_and_runs_through_host() -> None:
    registry = assemble_memory_plugins(
        config=_probe_memory_config(),
        builtin_factories=(ProbeMemoryPluginFactory(),),
        build_context=_offline_memory_build_context(),
    )
    host = _probe_memory_host(registry)
    state = AgentState.from_request(
        _probe_request(),
        run_id="run-sentinel",
    )
    try:
        host.open_session(
            identity=_probe_identity(),
            state=state,
            trace_store=None,
        )
        snapshot = host.prepare_context(
            state=state,
            trace_store=None,
            cancel_token=None,
        )

        assert registry.active_plugin.descriptor.plugin_id == "tests.memory_probe"
        assert snapshot.memories[0].memory_id == "memory-sentinel"
        assert snapshot.memories[0].text == "value-sentinel"
    finally:
        host.close(timeout=1.0)
