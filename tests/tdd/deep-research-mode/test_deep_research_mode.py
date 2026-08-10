from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant_agent.api.routes_agent import _gateway_http_metadata
from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.context.models import AssistantContextPack
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
)
from assistant_agent.gateway.runtime_adapter import realtime_request_to_user_request
from assistant_agent.gateway.runtime_types import RealtimeAgentRequest
from assistant_agent.runtime.chat_adapter import (
    ChatRequest,
    OpenAICompatibleChatAdapter,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.skills.loading import SkillCatalog
from assistant_agent.tools.ids import WORKFLOW_SUBMIT_TOOL_NAME
from assistant_agent.tools.models import ToolSpec
from assistant_agent.workflows.agent_runtime import AgentWorkItemResult
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.execution import AgentRuntimeWorkItemExecutor
from assistant_agent.workflows.models import WorkflowSubmission
from assistant_agent.workflows.research.definition import DeepResearchWorkflowDefinition
from assistant_agent.workflows.runtime import WorkItemAssignment


class _CapturingCompletions:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def create(self, **payload):
        self.payloads.append(payload)
        return iter(
            [
                {
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {
                            "delta": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ]
        )


class _CapturingClient:
    def __init__(self) -> None:
        self.completions = _CapturingCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class _CapturingAgentRuntime:
    def __init__(self) -> None:
        self.requests = []

    def run_work_item(self, request) -> AgentWorkItemResult:
        self.requests.append(request)
        return AgentWorkItemResult(
            status="succeeded",
            run_id="run-deep-research-sentinel",
            summary="research-result-sentinel",
        )


def _qwen_adapter(client: _CapturingClient) -> OpenAICompatibleChatAdapter:
    return OpenAICompatibleChatAdapter(
        provider="qwen",
        api_key="test-key",
        base_url="https://example.invalid/compatible-mode/v1",
        model="deepseek-v4-flash",
        native_web_search=True,
        client=client,
    )


def test_deep_research_mode_reaches_runtime_as_structured_request_state() -> None:
    http_request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="research-sentinel",
        assistant_mode="deep_research",
    )

    metadata = _gateway_http_metadata(http_request, "capture-sentinel")
    runtime_request = realtime_request_to_user_request(
        RealtimeAgentRequest(
            user_id=http_request.user_id,
            session_id=http_request.session_id,
            text=http_request.text or "",
            metadata=metadata,
        )
    )

    assert metadata["assistant_mode"] == "deep_research"
    assert runtime_request.assistant_mode == "deep_research"


def test_deep_research_entry_exposes_only_workflow_submission() -> None:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="research-sentinel",
        assistant_mode="deep_research",
    )
    specs = [
        ToolSpec(name=WORKFLOW_SUBMIT_TOOL_NAME, category="write"),
        ToolSpec(name="email_search", category="read"),
        ToolSpec(name="maps_text_search", category="read"),
    ]

    selection = select_prompt_tool_specs(
        request,
        specs,
        skill_catalog=SkillCatalog(),
    )

    assert selection.run_tool_catalog.available_tool_names == [
        WORKFLOW_SUBMIT_TOOL_NAME
    ]
    assert selection.run_tool_catalog.excluded_reasons == {
        "email_search": ["assistant_mode_not_allowed"],
        "maps_text_search": ["assistant_mode_not_allowed"],
    }


def test_deep_research_entry_requires_the_workflow_submission_tool() -> None:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="research-sentinel",
        assistant_mode="deep_research",
    )
    workflow_spec = ToolSpec(name=WORKFLOW_SUBMIT_TOOL_NAME, category="write")
    pack = AssistantContextPack(
        request=request,
        prompt_tool_specs=[workflow_spec],
        iteration=0,
        max_iterations=5,
    )

    compiled = PromptCompiler().compile(
        PromptCompileRequest(
            user_id=request.user_id,
            session_id=request.session_id,
            mode=PromptCompileMode.NATIVE_TOOL,
            user_query_fallback="fallback-sentinel",
            context_pack=pack,
            observations=(),
            native_calls=(),
            tool_call_id_prefix="call_",
        )
    )

    assert compiled.chat_request.tool_choice == {
        "type": "function",
        "function": {"name": WORKFLOW_SUBMIT_TOOL_NAME},
    }
    assert compiled.chat_request.provider_search_profile == "standard"


def test_deep_research_entry_fails_closed_without_workflow_submission() -> None:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="research-sentinel",
        assistant_mode="deep_research",
    )
    pack = AssistantContextPack(
        request=request,
        iteration=0,
        max_iterations=5,
    )

    with pytest.raises(
        ValueError,
        match="deep_research_mode_requires_workflow_submit",
    ):
        PromptCompiler().compile(
            PromptCompileRequest(
                user_id=request.user_id,
                session_id=request.session_id,
                mode=PromptCompileMode.NATIVE_TOOL,
                user_query_fallback="fallback-sentinel",
                context_pack=pack,
                observations=(),
                native_calls=(),
                tool_call_id_prefix="call_",
            )
        )


def test_deep_research_chat_uses_required_max_search_without_freshness() -> None:
    client = _CapturingClient()
    adapter = _qwen_adapter(client)

    result = adapter.chat(
        ChatRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            user_query="research-sentinel",
            assistant_mode="deep_research",
            provider_search_profile="deep_research",
        )
    )

    assert result.success is True
    assert client.completions.payloads[0]["extra_body"] == {
        "enable_thinking": True,
        "enable_search": True,
        "search_options": {
            "search_strategy": "max",
            "forced_search": True,
            "enable_search_extension": True,
        },
    }


def test_deep_research_ingress_does_not_run_research_search_before_submission() -> None:
    client = _CapturingClient()
    adapter = _qwen_adapter(client)

    result = adapter.chat(
        ChatRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            user_query="research-sentinel",
            assistant_mode="deep_research",
        )
    )

    assert result.success is True
    assert client.completions.payloads[0]["extra_body"] == {
        "enable_thinking": False,
        "enable_search": True,
        "search_options": {
            "search_strategy": "turbo",
            "forced_search": False,
            "enable_search_extension": True,
            "freshness": 7,
        },
    }


def test_deep_research_work_item_compiles_the_research_search_profile() -> None:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="collect-sources-sentinel",
        assistant_mode="deep_research",
        metadata={
            "_trusted_workflow_assignment": {
                "workflow_id": "workflow-sentinel",
                "work_item_id": "collect-sentinel",
                "attempt_id": "attempt-sentinel",
            }
        },
    )
    pack = AssistantContextPack(
        request=request,
        iteration=0,
        max_iterations=5,
    )

    compiled = PromptCompiler().compile(
        PromptCompileRequest(
            user_id=request.user_id,
            session_id=request.session_id,
            mode=PromptCompileMode.NATIVE_TOOL,
            user_query_fallback="fallback-sentinel",
            context_pack=pack,
            observations=(),
            native_calls=(),
            tool_call_id_prefix="call_",
        )
    )

    assert compiled.chat_request.provider_search_profile == "deep_research"


def test_deep_research_work_items_use_native_search_and_no_local_web_tools(
    tmp_path,
) -> None:
    agent_runtime = _CapturingAgentRuntime()
    artifact_store = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    executor = AgentRuntimeWorkItemExecutor(
        agent_runtime=agent_runtime,
        artifact_store=artifact_store,
        context_compiler=WorkflowContextCompiler(artifact_store=artifact_store),
    )
    assignment = WorkItemAssignment.model_validate(
        {
            "workflow_id": "workflow-sentinel",
            "workflow_type": "deep_research",
            "definition_version": "2",
            "user_id": "user-sentinel",
            "agent_id": "agent-sentinel",
            "session_id": "session-sentinel",
            "attempt_id": "attempt-sentinel",
            "objective": "research-objective-sentinel",
            "inputs": {},
            "model_calls_remaining": 5,
            "tool_calls_remaining": 5,
            "work_item": {
                "work_item_id": "collect-sentinel",
                "kind": "collect_sources",
                "objective": "collect-sources-sentinel",
            },
        }
    )

    result = executor.execute(assignment)

    assert result.status == "succeeded"
    assert agent_runtime.requests[0].assistant_mode == "deep_research"
    assert agent_runtime.requests[0].allowed_tool_names == []
    artifact_store.close()


def test_chat_compatible_research_plan_marks_sources_as_best_effort() -> None:
    definition = DeepResearchWorkflowDefinition()
    plan = definition.build_initial_plan(
        workflow_id="workflow-sentinel",
        submission=WorkflowSubmission(
            workflow_type="deep_research",
            objective="research-objective-sentinel",
            deliverables=["report-sentinel"],
            inputs={"source_target": 12},
            durability_reasons=["multi_stage", "many_sources"],
            idempotency_key="submission-sentinel",
        ),
    )
    contracts = {
        item.work_item_id: item.acceptance_contract for item in plan.work_items
    }

    assert definition.descriptor.definition_version == "2"
    assert contracts["collect_sources"] == {
        "target_sources": 12,
        "source_verification": "best_effort",
    }
    assert contracts["extract_evidence"] == {"source_refs": "best_effort"}
    assert contracts["draft"] == {"citations": "best_effort"}
    assert contracts["verify"] == {
        "unresolved_claims_target": 0,
        "verification": "best_effort",
    }
