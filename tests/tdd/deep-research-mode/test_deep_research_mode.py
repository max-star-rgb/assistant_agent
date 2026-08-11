from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from assistant_agent.api.agent_service_websocket import (
    AgentServiceConnectionState,
    ChatHandler,
    PreparedChat,
    _prepared_chat_response,
)
from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.config import ProviderConfig
from assistant_agent.context.models import AssistantContextPack
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
)
from assistant_agent.gateway.runtime_adapter import realtime_request_to_user_request
from assistant_agent.gateway.runtime_types import RealtimeAgentRequest, RealtimeAgentResult
from assistant_agent.gateway.session import GatewaySessionManager
from assistant_agent.gateway.turn_facade import GatewayTurnFacade, GatewayTurnRequest
from assistant_agent.observability.agent_service_delivery import AgentServiceDelivery
from assistant_agent.runtime.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
    OpenAICompatibleChatAdapter,
)
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.skills.loading import SkillCatalog
from assistant_agent.tools.ids import WORKFLOW_SUBMIT_TOOL_NAME
from assistant_agent.tools.models import ToolSpec
from assistant_agent.workflows.agent_runtime import (
    AgentWorkItemRequest,
    AgentWorkItemResult,
    parse_work_item_response,
)
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.execution import AgentRuntimeWorkItemExecutor
from assistant_agent.workflows.models import WorkflowSubmission
from assistant_agent.workflows.research.definition import DeepResearchWorkflowDefinition
from assistant_agent.workflows.runtime import WorkItemAssignment
from scripts.media_simulator import chat_body, parse_console_command


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
    runtime_request = realtime_request_to_user_request(
        RealtimeAgentRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="research-sentinel",
            assistant_mode="deep_research",
            metadata={"assistant_mode": "standard"},
        )
    )

    assert runtime_request.assistant_mode == "deep_research"


def test_deepseek_v4_flash_uses_its_declared_million_token_input_window() -> None:
    config = ProviderConfig.from_env({
        "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
        "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
        "QWEN_API_KEY": "key-sentinel",
        "QWEN_CHAT_MODEL": "deepseek-v4-flash",
    })

    assert config.context_input_token_limit == 1_000_000


def test_media_simulator_deep_command_sets_structured_chat_mode() -> None:
    assert parse_console_command("/deep research") == ("mode", "deep_research")
    assert parse_console_command("/standard") == ("mode", "standard")

    body = chat_body(
        text="research-sentinel",
        chat_index="chat-sentinel",
        user_number="user-sentinel",
        speaker_number="user-sentinel",
        stream=False,
        assistant_mode="deep_research",
        now=lambda: "2026-08-10T00:00:00+08:00",
    )

    assert body["assistantMode"] == "deep_research"


def test_agent_service_converts_chat_mode_to_gateway_turn_field() -> None:
    class CapturingFacade:
        def __init__(self) -> None:
            self.request = None

        async def run_turn(self, request, **_kwargs):
            self.request = request
            return SimpleNamespace(
                status="completed",
                response_text="ok-sentinel",
                payload={},
                reason="completed",
            )

    facade = CapturingFacade()
    state = AgentServiceConnectionState(
        session_id="vendor-session-sentinel",
        query_params={},
        runtime_session_id="runtime-session-sentinel",
        response_session_id="vendor-session-sentinel",
        media_protocol=True,
        gateway_facade=facade,
    )
    body = {
        "chatIndex": "chat-sentinel",
        "userNumber": "user-sentinel",
        "assistantMode": "deep_research",
        "contents": [
            {
                "speakerNumber": "user-sentinel",
                "speechContent": "research-sentinel",
                "time": "2026-08-10T00:00:00+08:00",
            }
        ],
        "stream": False,
    }

    asyncio.run(
        ChatHandler().handle(
            session_id="runtime-session-sentinel",
            body=body,
            state=state,
        )
    )

    assert facade.request.assistant_mode == "deep_research"


def test_agent_service_projects_workflow_output_ref_structurally() -> None:
    response = _prepared_chat_response(
        PreparedChat(
            session_id="runtime-session-sentinel",
            response_session_id="vendor-session-sentinel",
            body={"stream": True},
            chat_index="chat-sentinel",
            user_number="user-sentinel",
            latest_speech="research-sentinel",
            contents=[],
            assistant_mode="deep_research",
            video_ids=[],
            received_ns=1,
            accepted_ns=2,
            session_turn=1,
        ),
        state=AgentServiceConnectionState(
            session_id="vendor-session-sentinel",
            query_params={},
            media_protocol=True,
        ),
        turn=SimpleNamespace(
            status="completed",
            response_text="accepted-sentinel",
            payload={
                "output_refs": [
                    "workflow://workflow-sentinel",
                    "provider://chat/provider-sentinel",
                ]
            },
        ),
        delivery=AgentServiceDelivery(
            delivery_id="delivery-sentinel",
            session_digest="session-digest-sentinel",
            chat_index_digest="chat-digest-sentinel",
            chat_index="chat-sentinel",
            expects_ack=False,
        ),
        sequence=1,
    )

    body = json.loads(response["body"])
    assert body["outputRefs"] == ["workflow://workflow-sentinel"]


def test_gateway_snapshots_assistant_mode_on_realtime_request() -> None:
    class CapturingBackend:
        def __init__(self) -> None:
            self.requests = []

        async def run_turn(self, request, **_kwargs):
            self.requests.append(request)
            return RealtimeAgentResult(
                status="completed",
                response_text="ok-sentinel",
                run_id=request.run_id,
            )

    async def run() -> None:
        backend = CapturingBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        facade = GatewayTurnFacade(manager=manager)
        try:
            await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-sentinel",
                    session_id="session-sentinel",
                    text="research-sentinel",
                    assistant_mode="deep_research",
                )
            )
        finally:
            await facade.close()
            await manager.close()

        assert backend.requests[0].assistant_mode == "deep_research"

    asyncio.run(run())


def test_standard_mode_remains_the_gateway_default() -> None:
    request = realtime_request_to_user_request(RealtimeAgentRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="research-sentinel",
    ))

    assert request.assistant_mode == "standard"


def test_deep_research_entry_does_not_expose_llm_submission_tools() -> None:
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

    assert selection.run_tool_catalog.available_tool_names == []
    assert selection.run_tool_catalog.excluded_reasons == {
        WORKFLOW_SUBMIT_TOOL_NAME: ["assistant_mode_runtime_managed"],
        "email_search": ["assistant_mode_not_allowed"],
        "maps_text_search": ["assistant_mode_not_allowed"],
    }


def test_deep_research_entry_prompt_has_no_submission_tool_choice() -> None:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="research-sentinel",
        assistant_mode="deep_research",
    )
    pack = AssistantContextPack(
        request=request,
        prompt_tool_specs=[],
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

    assert compiled.chat_request.tools == []
    assert compiled.chat_request.tool_choice is None


def test_deep_research_without_workflow_runs_inline_with_native_search() -> None:
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

    assert compiled.chat_request.tools == []
    assert compiled.chat_request.tool_choice is None
    assert compiled.chat_request.provider_search_profile == "deep_research"


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
            "workflow_trace_id": "fedcba9876543210fedcba9876543210",
            "definition_version": "2",
            "user_id": "user-sentinel",
            "agent_id": "agent-sentinel",
            "session_id": "session-sentinel",
            "attempt_id": "attempt-sentinel",
            "objective": "research-objective-sentinel",
            "inputs": {
                "user_inputs": [{
                    "resume_token": "resume-sentinel",
                    "values": {"response": "clarification-sentinel"},
                }]
            },
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
    assert agent_runtime.requests[0].workflow_trace_id == (
        "fedcba9876543210fedcba9876543210"
    )
    assert agent_runtime.requests[0].assistant_mode == "deep_research"
    assert agent_runtime.requests[0].allowed_tool_names == []
    assert agent_runtime.requests[0].workflow_inputs == {
        "user_inputs": [{
            "resume_token": "resume-sentinel",
            "values": {"response": "clarification-sentinel"},
        }]
    }
    artifact_store.close()


class _ScriptedWorkItemChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, result: ChatResult) -> None:
        self.result = result
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return self.result


def _deep_research_work_item_request() -> AgentWorkItemRequest:
    return AgentWorkItemRequest(
        workflow_id="workflow-sentinel",
        workflow_type="deep_research",
        work_item_id="draft-sentinel",
        attempt_id="attempt-sentinel",
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
        objective="draft-objective-sentinel",
        work_item_kind="draft",
        acceptance_contract={"min_sources": 4},
        assigned_constraints=[
            {
                "constraint_id": "source-count",
                "statement": "最终报告至少引用 15 个来源",
                "owner_work_item_ids": ["draft-sentinel"],
                "verifier_work_item_id": "verify-sentinel",
                "severity": "required",
            }
        ],
        assistant_mode="deep_research",
        context_manifest={
            "workflow_id": "workflow-sentinel",
            "objective": "research-objective-sentinel",
            "constraints": [],
            "artifacts": [],
            "total_excerpt_chars": 0,
            "trimmed": False,
        },
    )


def test_long_work_item_text_is_preserved_as_content_with_a_bounded_summary() -> None:
    text = "研究正文" * 2_000

    result = parse_work_item_response(
        text,
        run_id="run-sentinel",
        artifact_refs=[],
        model_calls_used=1,
        tool_calls_used=0,
    )

    assert result.status == "succeeded"
    assert result.content == text
    assert len(result.summary) <= 4_000


def test_verifier_requires_a_structured_complete_constraint_result() -> None:
    missing = parse_work_item_response(
        "plain-verifier-text-sentinel",
        run_id="run-sentinel",
        artifact_refs=[],
        model_calls_used=1,
        tool_calls_used=0,
        required_verification_ids=["source-count"],
    )
    verified = parse_work_item_response(
        json.dumps({
            "workflow_control": {
                "status": "verified",
                "summary": "verification-summary-sentinel",
                "content": "full-report-sentinel",
                "verified_constraint_ids": ["source-count"],
            }
        }),
        run_id="run-sentinel",
        artifact_refs=[],
        model_calls_used=1,
        tool_calls_used=0,
        required_verification_ids=["source-count"],
    )

    assert missing.status == "failed"
    assert missing.error_code == "verification_result_missing"
    assert verified.status == "succeeded"
    assert verified.content == "full-report-sentinel"


def test_deep_research_work_item_uses_a_separate_response_budget() -> None:
    adapter = _ScriptedWorkItemChatAdapter(ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="stop",
        response_text="complete-result-sentinel",
    ))
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
    )

    result = runtime.run_work_item(_deep_research_work_item_request())

    assert result.status == "succeeded"
    assert adapter.requests[0].max_tokens == 8_192


def test_truncated_work_item_is_not_persisted_as_a_successful_result() -> None:
    adapter = _ScriptedWorkItemChatAdapter(ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="length",
        response_text="partial-result-sentinel",
    ))
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
    )

    result = runtime.run_work_item(_deep_research_work_item_request())

    assert result.status == "failed"


def test_context_overflow_work_item_is_not_persisted_as_a_successful_result() -> None:
    adapter = _ScriptedWorkItemChatAdapter(ChatResult(
        provider="scripted",
        model="scripted-model",
        errors=[ChatProviderError(
            code="provider_context_overflow",
            message="context-overflow-detail-sentinel",
        )],
    ))
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
    )

    result = runtime.run_work_item(_deep_research_work_item_request())

    assert result.status == "failed"
    assert result.error_code == "provider_error"


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

    assert definition.descriptor.definition_version == "3"
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
