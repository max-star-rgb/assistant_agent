from __future__ import annotations

from assistant_agent.config import ProviderConfig
from evals.agent.missions.meeting_logistics_tentative_calendar_commit.environment import (
    MeetingLogisticsEnvironment,
)
from assistant_agent.tools.plugins.contracts import ToolRegistrationRecord
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.registry_overlay import apply_tool_replacements
from evals.agent.travel_support import (
    controlled_amap_proxy_tool,
    maps_geo_definition,
    maps_text_search_definition,
    maps_transit_definition,
)


def test_meeting_mission_declares_only_exact_dependency_replacements() -> None:
    environment = MeetingLogisticsEnvironment(
        config=ProviderConfig(provider_mode="mock"),
    )

    assert not hasattr(type(environment), "build_registry")
    assert environment.runtime_assembly is None
    assert environment.validate().passed is True


def test_meeting_replacements_preserve_production_tool_specs() -> None:
    environment = MeetingLogisticsEnvironment(
        config=ProviderConfig(provider_mode="mock"),
    )
    base = create_default_registry(ProviderConfig(provider_mode="mock"))
    production = ToolRegistry()
    for name in base.list():
        production.register(base.get(name), base.registration_record(name))
    for definition in (
        maps_text_search_definition(),
        maps_geo_definition(),
        maps_transit_definition(),
    ):
        definition = definition.model_copy(
            update={"description": f"production discovered: {definition.name}"}
        )
        tool = controlled_amap_proxy_tool(
            definition,
            runner=environment._maps_runner,
        )
        production.register(
            tool,
            ToolRegistrationRecord(
                tool_name=tool.name,
                plugin_id="mcp.amap_maps",
                plugin_version="test",
                source_type="mcp",
                source_ref="test:production-amap",
            ),
        )
    production.seal()

    assembly = apply_tool_replacements(
        production,
        environment.tool_replacements(production),
    )

    replacements = environment.tool_replacements(production)
    assert assembly.registry.list() == production.list()
    assert {
        name
        for name, provenance in assembly.provenance.items()
        if provenance.dependency_mode == "controlled_replacement"
    } == {item.tool_name for item in replacements} == {
        "mcp.amap_maps.maps_text_search",
        "mcp.amap_maps.maps_geo",
        "mcp.amap_maps.maps_direction_transit_integrated",
        "lodging_search",
        "calendar_search",
        "calendar_create",
    }
    assert all(item.reason.strip() for item in replacements)
    assert all(item.source_ref.strip() for item in replacements)
    poi_replacement = next(
        item.tool for item in replacements if item.tool_name.endswith("maps_text_search")
    )
    result = poi_replacement.run(
        {"keywords": "上海青浦万达茂", "city": "上海"}
    )
    assert result.success is True
    assert result.output_ref == "eval://meeting/maps/maps_text_search"
