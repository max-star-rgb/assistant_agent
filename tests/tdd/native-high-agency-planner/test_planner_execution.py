"""Temporary RED/GREEN coverage for planner execution through the shared agent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent import planning_graph
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.skills.loading import SkillCatalog, load_repo_skill_descriptors
from assistant_agent.tools.native_boundary import configure_builtin_tool
from assistant_agent.tools.plugins.builtin.skill_loading import create_load_skill_tool


class _PlannerExecutionModel(MockAssistantChatModel):
    scenario: Literal["default-tool", "skill-tool"]

    def _response_message(self, messages, **kwargs):
        visible_names = _visible_tool_names(kwargs.get("tools"))
        if "NativePlanProposal" not in visible_names:
            return AIMessage(content="worker-or-finalizer-sentinel")
        if self.scenario == "default-tool":
            if "weather_probe" in visible_names and not _has_tool_result(
                messages,
                "weather_probe",
                tool_call_id="weather-call-1",
            ):
                return _tool_call("weather_probe", "weather-call-1")
            return _proposal_call(
                evidence_id="weather-call-1",
                producer_node_id="weather-worker",
            )
        if not _has_tool_result(messages, "load_skill"):
            if "load_skill" not in visible_names:
                return _proposal_call(
                    evidence_id="route-call-1",
                    producer_node_id="route-worker",
                )
            return _tool_call(
                "load_skill",
                "load-skill-call-1",
                args={"skill_id": "travel-sentinel"},
            )
        if not _has_tool_result(messages, "route_probe"):
            if "route_probe" not in visible_names:
                return AIMessage(content="governed-tool-not-visible")
            return _tool_call("route_probe", "route-call-1")
        return _proposal_call(
            evidence_id="route-call-1",
            producer_node_id="route-worker",
        )


class _CompactingFastAgent:
    """Delegate execution to a real agent, then emulate history replacement."""

    name = "AssistantFastAgent"

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def get_graph(self):
        return self._delegate.get_graph()

    async def ainvoke(self, input: dict[str, Any], *, context: Any):
        result = await self._delegate.ainvoke(input, context=context)
        if input.get("agent_phase") != "planner":
            return result
        input_count = len(input.get("messages", ()))
        generated = list(result.get("messages", ()))[input_count:]
        replaced_historical_tool = next(
            (
                message.model_copy(update={"id": "replacement-tool-message"})
                for message in input.get("messages", ())
                if isinstance(message, ToolMessage)
            ),
            None,
        )
        return {
            **result,
            "messages": [
                HumanMessage(content="compacted-history-sentinel", id="summary-1"),
                *(
                    [replaced_historical_tool]
                    if replaced_historical_tool is not None
                    else []
                ),
                ToolMessage(
                    content="orphan-weather-sentinel",
                    name="weather_probe",
                    tool_call_id="orphan-weather-call",
                    id="orphan-tool-message",
                ),
                *generated,
            ],
        }


def test_planner_calls_default_tool_and_captures_real_evidence() -> None:
    model = _PlannerExecutionModel(scenario="default-tool")
    weather_probe = _create_probe_tool(
        "weather_probe",
        content="weather-sentinel",
        artifact={
            "temperature": 23,
            "output_ref": "artifact://weather-sentinel",
        },
    )
    shared_agent = build_fast_agent(
        model,
        [weather_probe],
        skill_catalog=SkillCatalog(),
    )
    graph = build_planning_graph(
        model,
        shared_agent,
        tools=[weather_probe],
        skill_catalog=SkillCatalog(),
    )

    result = asyncio.run(
        graph.ainvoke(
            _planning_input(),
            context=AssistantRunContext(),
        )
    )

    assert [(item.tool_name, item.content) for item in result["planner_evidence"]] == [
        ("weather_probe", "weather-sentinel")
    ]
    evidence = result["planner_evidence"][0]
    assert evidence.evidence_id == "weather-call-1"
    assert evidence.structured_content == {
        "temperature": 23,
        "output_ref": "artifact://weather-sentinel",
    }
    assert evidence.artifact_ref == "artifact://weather-sentinel"
    assert result["plan_candidate"].deliverables[0].evidence_refs == (
        evidence.evidence_id,
    )


def test_planner_loads_skill_then_calls_governed_tool(tmp_path: Path) -> None:
    _write_travel_skill(tmp_path)
    catalog = load_repo_skill_descriptors(tmp_path)
    model = _PlannerExecutionModel(scenario="skill-tool")
    route_probe = _create_probe_tool(
        "route_probe",
        content="route-sentinel",
        artifact={"route": "north", "artifact_ref": "artifact://route-sentinel"},
    )
    load_skill_tool = create_load_skill_tool(root=tmp_path)
    tools = [load_skill_tool, route_probe]
    shared_agent = build_fast_agent(
        model,
        tools,
        skill_catalog=catalog,
    )
    graph = build_planning_graph(
        model,
        shared_agent,
        tools=tools,
        skill_catalog=catalog,
    )

    result = asyncio.run(
        graph.ainvoke(
            _planning_input(),
            context=AssistantRunContext(),
        )
    )

    assert result["planner_active_skill_ids"] == ["travel-sentinel"]
    assert result["planner_skill_reference_grants"] == {
        "travel-sentinel": ["route-guide"]
    }
    assert [item.tool_name for item in result["planner_evidence"]] == ["route_probe"]
    assert result["planner_evidence"][0].content == "route-sentinel"


def test_planner_captures_new_evidence_after_history_replacement() -> None:
    model = _PlannerExecutionModel(scenario="default-tool")
    weather_probe = _create_probe_tool(
        "weather_probe",
        content="weather-sentinel",
        artifact={"temperature": 23},
    )
    compiled_agent = build_fast_agent(
        model,
        [weather_probe],
        skill_catalog=SkillCatalog(),
    )
    graph = build_planning_graph(
        model,
        _CompactingFastAgent(compiled_agent),
        tools=[weather_probe],
        skill_catalog=SkillCatalog(),
    )
    history = [
        HumanMessage(content="history-0-sentinel"),
        _tool_call("weather_probe", "historical-weather-call"),
        ToolMessage(
            content="historical-weather-sentinel",
            name="weather_probe",
            tool_call_id="historical-weather-call",
        ),
        HumanMessage(content="history-1-sentinel"),
        HumanMessage(content="history-2-sentinel"),
    ]

    result = asyncio.run(
        graph.ainvoke(
            {
                **_planning_input(),
                "messages": [*history, HumanMessage(content="request-sentinel")],
            },
            context=AssistantRunContext(),
        )
    )

    assert [
        (item.evidence_id, item.content) for item in result["planner_evidence"]
    ] == [("weather-call-1", "weather-sentinel")]


def test_evidence_capture_rejects_unreferenceable_ids_and_bounds_artifacts() -> None:
    oversized_artifact = {
        "provider_raw_response": {"secret": "must-not-be-retained"},
        "payload": "x" * 60_000,
        "nested": {"output_ref": "artifact://bounded-sentinel"},
    }
    messages = [
        ToolMessage(
            content="kept-sentinel",
            name="weather_probe",
            tool_call_id="valid-call-1",
            artifact=oversized_artifact,
        ),
        ToolMessage(
            content="invalid-id-sentinel",
            name="weather_probe",
            tool_call_id="invalid id",
        ),
        ToolMessage(
            content="long-id-sentinel",
            name="weather_probe",
            tool_call_id="a" * 161,
        ),
        ToolMessage(
            content="control-sentinel",
            name="load_skill",
            tool_call_id="load-call-1",
        ),
        ToolMessage(
            content="synthetic-sentinel",
            name="NativePlanProposal",
            tool_call_id="proposal-call-1",
        ),
    ]

    evidence = planning_graph.capture_planner_evidence(
        messages,
        inventory_names={
            "weather_probe",
            "load_skill",
            "NativePlanProposal",
        },
    )

    assert len(evidence) == 1
    assert evidence[0].evidence_id == "valid-call-1"
    assert evidence[0].structured_content == {
        "payload": {"_truncated": True, "reason": "string_byte_limit"},
        "nested": {"output_ref": "artifact://bounded-sentinel"},
    }
    assert evidence[0].artifact_ref == "artifact://bounded-sentinel"


def test_evidence_capture_keeps_json_safe_data_without_provider_raw_payload() -> None:
    messages = [
        ToolMessage(
            content="x" * 20_001,
            name="weather_probe",
            tool_call_id="safe-call-1",
            status="error",
            artifact={
                "value": 7,
                "provider_raw_response": {"secret": "must-not-be-retained"},
            },
        ),
        ToolMessage(
            content="opaque-sentinel",
            name="weather_probe",
            tool_call_id="safe-call-2",
            artifact={
                "opaque": object(),
                "output_ref": "artifact://opaque-sentinel",
            },
        ),
    ]

    evidence = planning_graph.capture_planner_evidence(
        messages,
        inventory_names={"weather_probe"},
    )

    assert evidence[0].status == "failed"
    assert len(evidence[0].content) == 20_000
    assert evidence[0].structured_content == {"value": 7}
    assert evidence[1].structured_content == {
        "opaque": {"_truncated": True, "reason": "unsupported_type"},
        "output_ref": "artifact://opaque-sentinel",
    }
    assert evidence[1].artifact_ref == "artifact://opaque-sentinel"


def test_evidence_capture_removes_all_raw_provider_shapes() -> None:
    raw_fields = {
        "provider_response": {"secret": "provider-response"},
        "provider_raw_payload": {"secret": "provider-raw-payload"},
        "provider_payload": {"secret": "provider-payload"},
        "raw_payload": {"secret": "raw-payload"},
        "raw_provider_payload": {"secret": "raw-provider-payload"},
        "raw_provider": {"secret": "raw-provider"},
        "raw_result": {"secret": "raw-result"},
        "providerResponse": {"secret": "camel-provider-response"},
        "providerRawPayload": {"secret": "camel-provider-raw-payload"},
        "rawProviderPayload": {"secret": "camel-raw-provider-payload"},
        "rawResult": {"secret": "camel-raw-result"},
        "providerSearchResponseEnvelope": {
            "secret": "camel-provider-response-structure"
        },
        "rawBinaryOutput": {"secret": "camel-raw-output-structure"},
    }
    messages = [
        ToolMessage(
            content=[
                {
                    "type": "json",
                    "safe": "content-sentinel",
                    **raw_fields,
                }
            ],
            name="weather_probe",
            tool_call_id="raw-filter-call-1",
            artifact={
                "safe": "artifact-sentinel",
                "nested": {
                    "keep": 7,
                    "Provider Response": {"secret": "normalized-key-sentinel"},
                },
                **raw_fields,
            },
        )
    ]

    evidence = planning_graph.capture_planner_evidence(
        messages,
        inventory_names={"weather_probe"},
    )

    assert json.loads(evidence[0].content) == [
        {"safe": "content-sentinel", "type": "json"}
    ]
    assert evidence[0].structured_content == {
        "safe": "artifact-sentinel",
        "nested": {"keep": 7},
    }


def _planning_input() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="request-sentinel")],
        "memory_context": (),
        "memory_status": "empty",
    }


def _create_probe_tool(
    name: str,
    *,
    content: str,
    artifact: dict[str, Any],
) -> BaseTool:
    @tool(name, response_format="content_and_artifact")
    def probe(
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[str, dict[str, Any]]:
        """Return one deterministic offline planner probe result."""

        del runtime
        return content, artifact

    return configure_builtin_tool(probe, "read")


def _visible_tool_names(raw_tools: object) -> set[str]:
    if not isinstance(raw_tools, list):
        return set()
    names: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


def _has_tool_result(
    messages: list[Any],
    name: str,
    *,
    tool_call_id: str | None = None,
) -> bool:
    return any(
        isinstance(message, ToolMessage)
        and message.name == name
        and (tool_call_id is None or message.tool_call_id == tool_call_id)
        for message in messages
    )


def _tool_call(
    name: str,
    tool_call_id: str,
    *,
    args: dict[str, Any] | None = None,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args or {},
                "id": tool_call_id,
                "type": "tool_call",
            }
        ],
    )


def _proposal_call(*, evidence_id: str, producer_node_id: str) -> AIMessage:
    return _tool_call(
        "NativePlanProposal",
        "proposal-call-1",
        args={
            "schema_version": "native_plan_v2",
            "nodes": [
                {
                    "node_id": producer_node_id,
                    "objective": "worker-sentinel",
                    "depends_on": [],
                }
            ],
            "deliverables": [
                {
                    "deliverable_id": "answer",
                    "description": "return the sentinel",
                    "producer_node_ids": [producer_node_id],
                    "evidence_refs": [evidence_id],
                }
            ],
        },
    )


def _write_travel_skill(root: Path) -> None:
    skill_dir = root / "skills" / "travel-sentinel"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "Call route_probe after loading this workflow.\n",
        encoding="utf-8",
    )
    (skill_dir / "skill.toml").write_text(
        "schema_version = 1\n"
        'skill_id = "travel-sentinel"\n'
        "version = 1\n"
        'description = "Route workflow"\n'
        'governed_tools = ["route_probe"]\n'
        "[references]\n"
        'route-guide = "references/route-guide.md"\n',
        encoding="utf-8",
    )
    (references_dir / "route-guide.md").write_text(
        "route-reference-sentinel\n",
        encoding="utf-8",
    )
