from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import hashlib
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel

from assistant_agent.context.service import ContextService
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
from assistant_agent.runtime.assistant_graph_state import AssistantTurnState
from assistant_agent.runtime.assistant_graph_profiles import (
    ASSISTANT_GRAPH_PROFILES,
    AssistantGraphProfile,
    GraphProfileMismatchError,
    GraphProfilePolicyError,
    ProfileInvocationInput,
    profile_input_adapter,
    profile_output_adapter,
    resolve_resume_profile,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_runtime import (
    GraphRuntimeContext,
    bind_checkpointed_runtime_node,
)
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import RuntimeTaskUpdate, UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult, ToolSpec
from tests.core.support import ProbeTool, ScriptedChatAdapter, sealed_registry


class _WriteProbeInput(BaseModel):
    value: str


class _WriteProbe(ToolBase):
    name = "write_probe"
    description = "write probe"
    input_schema = _WriteProbeInput
    output_schema = _WriteProbeInput
    category = "write"

    def _run(self, input: _WriteProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name, success=True, data={"value": input.value}
        )


class _SecondReadProbe(ProbeTool):
    name = "second_read_probe"


class _LoadSkillProbe(ProbeTool):
    name = LOAD_SKILL_TOOL_NAME


class _LoadSkillReferenceProbe(ProbeTool):
    name = LOAD_SKILL_REFERENCE_TOOL_NAME


class _ParentProbeState(AssistantTurnState, total=False):
    workflow_private_secret: str
    dynamic_task_uuid: str


def test_canonical_profiles_are_structured_and_immutable() -> None:
    profiles = tuple(ASSISTANT_GRAPH_PROFILES.values())

    assert tuple(ASSISTANT_GRAPH_PROFILES) == (
        "standard",
        "planner",
        "worker",
        "verifier",
    )
    assert all(isinstance(profile, AssistantGraphProfile) for profile in profiles)
    assert {profile.name for profile in profiles} == set(ASSISTANT_GRAPH_PROFILES)
    assert len({id(profile) for profile in profiles}) == 4

    with pytest.raises(FrozenInstanceError):
        profiles[0].max_tool_iterations = 999  # type: ignore[misc]


def test_profile_adapters_are_explicit_and_narrow_parent_capabilities() -> None:
    parent_state = {
        "user_id": "user-profile",
        "session_id": "session-profile",
        "run_id": "run-profile",
        "trace_id": "trace-profile",
        "agent_id": "agent-profile",
        "available_tool_names": ("read_probe", "generate_probe", "write_probe"),
        "registered_tool_specs": (
            ToolSpec(name="read_probe", category="read"),
            ToolSpec(name="generate_probe", category="generate"),
            ToolSpec(name="write_probe", category="write"),
        ),
        "workflow_private_secret": "must-not-cross-child-boundary",
        "dynamic_task_uuid": "task-2f96e7b7-1337-4f2c-a7d2-bf8d3be89f6a",
    }
    assignment = ProfileInvocationInput(
        profile="worker",
        assignment_ref="workflow-item:research-1",
        objective="Collect bounded evidence.",
        constraints=("Use cited evidence.",),
        capability_refs=("grant:research",),
        explicit_tool_allowlist=("read_probe", "write_probe"),
    )

    child_state = profile_input_adapter(parent_state, assignment)

    assert child_state["profile"] == "worker"
    assert child_state["request"]["text"] == "Collect bounded evidence."
    assert child_state["request"]["runtime_task_facts"] == {
        "action": "continue",
        "objective": "Collect bounded evidence.",
        "constraints": ["Use cited evidence."],
    }
    assert child_state["capability_refs"] == ["grant:research"]
    assert child_state["catalog"]["available_tool_names"] == ["read_probe"]
    assert child_state["max_tool_calls_per_run"] == 5
    assert child_state["max_control_tool_calls_per_run"] == 0
    assert child_state["context_refs"] == [
        {
            "kind": "context_section",
            "ref": "workflow-item:research-1",
            "source": "profile_assignment",
            "version": None,
            "status_code": None,
        }
    ]
    encoded = json.dumps(child_state)
    assert "must-not-cross-child-boundary" not in encoded
    assert "task-2f96e7b7-1337-4f2c-a7d2-bf8d3be89f6a" not in encoded

    child_state["run"]["status"] = "completed"
    child_state["tool_observations"] = [
        {
            "tool_name": "read_probe",
            "status": "succeeded",
            "summary": "evidence",
            "outcome": "success",
            "warnings": [],
            "is_complete": True,
            "output_ref": "output:1",
            "artifact_refs": ["artifact:1"],
            "provider_call_id": "call:1",
            "safe_details": [],
            "error": None,
        }
    ]
    child_state["final_response"] = {
        "message": "bounded result",
        "followup_question": None,
        "output_refs": ["artifact:2"],
        "citations": [],
    }

    result = profile_output_adapter(child_state)

    assert result.model_dump(mode="json") == {
        "profile": "worker",
        "status": "completed",
        "response": {
            "message": "bounded result",
            "followup_question": None,
            "output_refs": ["artifact:2"],
        },
        "tool_trajectory": [
            {
                "tool_name": "read_probe",
                "status": "succeeded",
                "summary": "evidence",
                "provider_call_id": "call:1",
                "output_ref": "output:1",
                "artifact_refs": ["artifact:1"],
            }
        ],
        "artifact_refs": ["artifact:2", "artifact:1"],
    }


def test_resume_profile_is_inherited_or_must_match_checkpoint() -> None:
    checkpoint = profile_input_adapter(
        {
            "user_id": "u",
            "session_id": "s",
            "run_id": "r",
            "trace_id": "t",
            "agent_id": "a",
            "available_tool_names": (),
            "registered_tool_specs": (),
        },
        ProfileInvocationInput(
            profile="verifier",
            assignment_ref="assignment:verify",
            objective="Verify evidence.",
        ),
    )

    assert resolve_resume_profile(checkpoint).name == "verifier"
    assert resolve_resume_profile(checkpoint, "verifier").name == "verifier"
    with pytest.raises(GraphProfileMismatchError) as captured:
        resolve_resume_profile(checkpoint, "worker")

    assert captured.value.code == "graph_profile_mismatch"


def test_profile_graphs_have_stable_names_and_inherit_parent_saver() -> None:
    saver = InMemorySaver()
    app = AssistantTurnGraphApp(checkpointer=saver)

    assert app.graph.checkpointer is saver
    for name in ASSISTANT_GRAPH_PROFILES:
        child = app.graph_for_profile(name)

        assert child is app.graph_for_profile(name)
        assert child.name == f"AssistantTurnGraph.{name}"
        assert child.checkpointer is None
        assert child.config == {
            "metadata": {"graph_profile": name},
            "tags": ["assistant_turn_graph", f"assistant_profile:{name}"],
        }


def test_profile_scope_filters_provider_specs_and_validator_catalog_together() -> None:
    registry = sealed_registry(
        ProbeTool(),
        _SecondReadProbe(),
        _WriteProbe(),
        _LoadSkillProbe(),
        _LoadSkillReferenceProbe(),
    )
    child_state = profile_input_adapter(
        {
            "user_id": "u",
            "session_id": "s",
            "run_id": "r",
            "trace_id": "t",
            "agent_id": "a",
            "available_tool_names": tuple(registry.list()),
            "registered_tool_specs": tuple(registry.list_specs()),
        },
        ProfileInvocationInput(
            profile="worker",
            assignment_ref="assignment:worker",
            objective="Perform assigned work.",
            explicit_tool_allowlist=(
                ProbeTool.name,
                _WriteProbe.name,
                LOAD_SKILL_TOOL_NAME,
                LOAD_SKILL_REFERENCE_TOOL_NAME,
            ),
        ),
    )
    request = UserRequest(
        user_id="u",
        session_id="s",
        text="Perform assigned work.",
        response_style="structured",
        task_execution_mode="foreground",
        runtime_task_update=RuntimeTaskUpdate(
            action="continue",
            objective="Perform assigned work.",
        ),
    )
    runtime_state = AgentState.from_request(
        request,
        run_id="r",
        trace_id="t",
        agent_id="a",
    )
    captured: dict[str, object] = {}

    def inspect_scope(graph_state: dict[str, object]) -> dict[str, object]:
        executor = graph_state["tool_executor"]
        assert isinstance(executor, ToolExecutor)
        captured["provider_names"] = [
            spec.name for spec in executor.registry.list_specs()
        ]
        captured["validator"] = ActionValidator().validate(
            decision=AssistantToolCall(
                tool_name=_WriteProbe.name,
                tool_input={"value": "blocked"},
                reason="probe",
            ),
            registry=executor.registry,
            request=graph_state["request"],
            state=graph_state["state"],
        )
        return graph_state

    wrapped = bind_checkpointed_runtime_node(
        "profile_scope_probe",
        inspect_scope,
        trace=False,
        expected_profile="worker",
    )
    result = wrapped(
        child_state,
        Runtime(
            context=GraphRuntimeContext(
                tool_executor=ToolExecutor(registry=registry),
                chat_adapter=ScriptedChatAdapter(
                    [
                        ChatResult(
                            provider="scripted",
                            model="scripted",
                            finish_reason="stop",
                            response_text="unused",
                        )
                    ]
                ),
                agent_state=runtime_state,
                state_ref_resolver=lambda _persisted, _runtime: None,
                profile_allowed_tool_names=frozenset(
                    child_state["catalog"]["available_tool_names"]
                ),
            )
        ),
    )

    assert captured["provider_names"] == [ProbeTool.name]
    assert result["catalog"]["available_tool_names"] == [ProbeTool.name]
    validation = captured["validator"]
    assert validation.code == "tool_not_allowed_for_run"

    escalated = json.loads(json.dumps(child_state))
    escalated["catalog"]["available_tool_names"].append(_WriteProbe.name)
    with pytest.raises(GraphProfilePolicyError) as captured_error:
        wrapped(
            escalated,
            Runtime(
                context=GraphRuntimeContext(
                    tool_executor=ToolExecutor(registry=registry),
                    chat_adapter=ScriptedChatAdapter([]),
                    agent_state=runtime_state,
                    state_ref_resolver=lambda _persisted, _runtime: None,
                    profile_allowed_tool_names=frozenset(
                        child_state["catalog"]["available_tool_names"]
                    ),
                )
            ),
        )
    assert captured_error.value.code == "graph_profile_policy_invalid"

    same_category_escalation = json.loads(json.dumps(child_state))
    same_category_escalation["catalog"]["available_tool_names"].append(
        _SecondReadProbe.name
    )
    forged_payload = json.dumps(
        ["worker", sorted(same_category_escalation["catalog"]["available_tool_names"])],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    same_category_escalation["catalog"]["selection_reason_codes"] = [
        reason
        for reason in same_category_escalation["catalog"]["selection_reason_codes"]
        if not reason.startswith("graph_profile_scope_sha256:")
    ] + [f"graph_profile_scope_sha256:{hashlib.sha256(forged_payload).hexdigest()}"]
    with pytest.raises(GraphProfilePolicyError):
        wrapped(
            same_category_escalation,
            Runtime(
                context=GraphRuntimeContext(
                    tool_executor=ToolExecutor(registry=registry),
                    chat_adapter=ScriptedChatAdapter([]),
                    agent_state=runtime_state,
                    state_ref_resolver=lambda _persisted, _runtime: None,
                    profile_allowed_tool_names=frozenset(
                        child_state["catalog"]["available_tool_names"]
                    ),
                )
            ),
        )

    assert LOAD_SKILL_TOOL_NAME not in child_state["catalog"]["available_tool_names"]
    assert (
        LOAD_SKILL_REFERENCE_TOOL_NAME
        not in child_state["catalog"]["available_tool_names"]
    )


def test_standard_child_scope_is_bound_to_trusted_runtime_assignment() -> None:
    """Standard is also a reusable child; its explicit allowlist cannot escalate."""

    registry = sealed_registry(ProbeTool(), _WriteProbe())
    child_state = profile_input_adapter(
        {
            "user_id": "u",
            "session_id": "s",
            "run_id": "r-standard",
            "trace_id": "t-standard",
            "agent_id": "a",
            "available_tool_names": tuple(registry.list()),
            "registered_tool_specs": tuple(registry.list_specs()),
        },
        ProfileInvocationInput(
            profile="standard",
            assignment_ref="assignment:standard",
            objective="Perform bounded work.",
            explicit_tool_allowlist=(ProbeTool.name,),
        ),
    )
    trusted_names = frozenset(child_state["catalog"]["available_tool_names"])
    escalated = json.loads(json.dumps(child_state))
    escalated["catalog"]["available_tool_names"].append(_WriteProbe.name)
    forged_payload = json.dumps(
        ["standard", sorted(escalated["catalog"]["available_tool_names"])],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    escalated["catalog"]["selection_reason_codes"] = [
        reason
        for reason in escalated["catalog"]["selection_reason_codes"]
        if not reason.startswith("graph_profile_scope_sha256:")
    ] + [f"graph_profile_scope_sha256:{hashlib.sha256(forged_payload).hexdigest()}"]
    runtime_state = AgentState.from_request(
        UserRequest(
            user_id="u",
            session_id="s",
            text="Perform bounded work.",
            response_style="structured",
            task_execution_mode="foreground",
            runtime_task_update=RuntimeTaskUpdate(
                action="continue",
                objective="Perform bounded work.",
            ),
        ),
        run_id="r-standard",
        trace_id="t-standard",
        agent_id="a",
    )
    wrapped = bind_checkpointed_runtime_node(
        "standard_scope_probe",
        lambda graph_state: graph_state,
        trace=False,
        expected_profile="standard",
    )

    with pytest.raises(GraphProfilePolicyError):
        wrapped(
            escalated,
            Runtime(
                context=GraphRuntimeContext(
                    tool_executor=ToolExecutor(registry=registry),
                    chat_adapter=ScriptedChatAdapter([]),
                    agent_state=runtime_state,
                    state_ref_resolver=lambda _persisted, _runtime: None,
                    profile_allowed_tool_names=trusted_names,
                )
            ),
        )


def test_native_parent_subgraph_namespace_does_not_become_child_business_state() -> (
    None
):
    registry = sealed_registry()
    assignment = ProfileInvocationInput(
        profile="worker",
        assignment_ref="assignment:worker-native",
        objective="Produce a bounded child result.",
        explicit_tool_allowlist=(ProbeTool.name,),
    )
    child_input = profile_input_adapter(
        {
            "user_id": "u-native",
            "session_id": "s-native",
            "run_id": "r-native",
            "trace_id": "t-native",
            "agent_id": "a-native",
            "available_tool_names": tuple(registry.list()),
            "registered_tool_specs": tuple(registry.list_specs()),
        },
        assignment,
    )
    child_input["workflow_private_secret"] = "parent-only-secret"
    child_input["dynamic_task_uuid"] = "parent-business-id-must-not-use-task-uuid"
    request = UserRequest(
        user_id="u-native",
        session_id="s-native",
        text="Produce a bounded child result.",
        response_style="structured",
        task_execution_mode="foreground",
        runtime_task_update=RuntimeTaskUpdate(
            action="continue",
            objective="Produce a bounded child result.",
        ),
    )
    runtime_state = AgentState.from_request(
        request,
        run_id="r-native",
        trace_id="t-native",
        agent_id="a-native",
    )
    context = GraphRuntimeContext(
        tool_executor=ToolExecutor(registry=registry),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="child-complete",
                )
            ]
        ),
        context_service=ContextService(),
        agent_state=runtime_state,
        state_ref_resolver=lambda _persisted, _runtime: None,
        profile_allowed_tool_names=frozenset(
            child_input["catalog"]["available_tool_names"]
        ),
    )
    child = AssistantTurnGraphApp().graph_for_profile("worker")
    parent_builder = StateGraph(
        _ParentProbeState,
        context_schema=GraphRuntimeContext,
    )
    parent_builder.add_node("worker", child)
    parent_builder.add_edge(START, "worker")
    parent_builder.add_edge("worker", END)
    parent = parent_builder.compile(
        checkpointer=InMemorySaver(),
        name="ProfileParentProbe",
    )

    async def collect() -> list[dict[str, object]]:
        return [
            part
            async for part in parent.astream(
                child_input,
                config={"configurable": {"thread_id": "thread-native"}},
                context=context,
                stream_mode=["values", "updates", "checkpoints"],
                subgraphs=True,
                version="v2",
            )
        ]

    parts = asyncio.run(collect())
    child_namespaces = {tuple(part.get("ns") or ()) for part in parts if part.get("ns")}
    child_checkpoints = [
        part["data"]["values"]
        for part in parts
        if part["type"] == "checkpoints" and part.get("ns")
    ]

    assert len(child_namespaces) == 1
    namespace = next(iter(child_namespaces))
    assert namespace[0].startswith("worker:")
    assert all(
        "parent-only-secret" not in json.dumps(value) for value in child_checkpoints
    )
    assert all(
        "parent-business-id-must-not-use-task-uuid" not in json.dumps(value)
        for value in child_checkpoints
    )
    final_child = child_checkpoints[-1]
    result = profile_output_adapter(final_child)
    assert result.response is not None
    assert result.response.message == "child-complete"
    assert namespace[0] not in json.dumps(result.model_dump(mode="json"))
