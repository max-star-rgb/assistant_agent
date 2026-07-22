"""Minimal offline safety net for the assistant's stable runtime boundaries."""

from datetime import datetime, timedelta, timezone
import importlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.system_prompt_policy import SystemPromptProfile, render_system_instruction
from assistant_agent.config import ProviderConfig
from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.realtime.event_mapping import map_agent_event_stream
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.llm_events import LLMEvent, LLMEventAccumulator, LLMToolCallDelta
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
    MockChatAdapter,
    OpenAICompatibleChatAdapter,
    create_chat_adapter,
)
from assistant_agent.services.context.compactor import DeterministicContextCompactor, create_context_compactor
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.hooks import HookManager, HookTraceStore
from assistant_agent.services.identifiers import IdFactory, new_run_id, new_session_id, new_span_id, new_trace_id
from assistant_agent.services.langfuse_scores import LangfuseScoreTraceObserver
from assistant_agent.services.otel_exporter import TextOtelTraceObserver
from assistant_agent.services.otel_exporter import OtlpHttpTextExporterConfig
from assistant_agent.services.otel_mapping import build_text_otel_span_specs, langfuse_trace_id
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_conversation import (
    TraceConversationText,
    TraceConversationView,
    TraceLlmInput,
    get_default_trace_conversation_store,
)
from assistant_agent.services.trace_store import TraceEvent
from scripts.run_client import chat_response_error


def _offline_config() -> ProviderConfig:
    return ProviderConfig(langgraph_checkpointer_backend="none")


class ScriptedChatAdapter:
    """Small provider-boundary fake that returns complete public ChatResult values."""

    provider = "scripted"
    model = "scripted-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class CancelledToken:
    def is_cancelled(self) -> bool:
        return True


class TimeoutAwareLifecycleSink:
    def __init__(self) -> None:
        self.shutdown_timeout: float | None = None

    def export(self, _spans: object) -> None:
        return None

    def write_scores(self, _scores: object) -> None:
        return None

    def shutdown(self, *, timeout: float) -> bool:
        self.shutdown_timeout = timeout
        return True


def test_package_and_runtime_initialize_offline() -> None:
    package = importlib.import_module("assistant_agent")
    runtime = AgentGraphRuntime(config=_offline_config())
    specs = {spec.name: spec for spec in runtime.registry.list_specs()}

    assert package is not None
    assert "shopping_search" in runtime.registry.list()
    assert "reminder_create" not in runtime.registry.list()
    assert "render_3d" not in runtime.registry.list()
    assert "vision_understanding" in runtime.registry.list()
    assert "video_understanding" not in runtime.registry.list()
    assert {
        "image_ids",
        "video_ids",
        "video_ref",
    }.issubset(specs["vision_understanding"].input_schema["properties"])
    assert "memory" not in specs
    assert {
        specs[name].toolset
        for name in (
            "memory_retrieval",
            "memory_save",
            "memory_media_ingest",
            "memory_ingest_status",
        )
    } == {"memory"}
    assert specs["calendar_search"].toolset == "personal.calendar"
    assert specs["calendar_create"].toolset == "personal.calendar"
    assert runtime.chat_adapter.provider == "mock"


def test_shopping_config_uses_unified_provider() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_API_KEY": "test-key",
            "MULTIMODAL_AGENT_SHOPPING_PROVIDER": "haodanku",
            "MULTIMODAL_AGENT_PRODUCT_PROVIDER": "local_json",
            "PRODUCT_SEARCH_LOCAL_PATH": "/tmp/legacy-products.json",
            "MULTIMODAL_AGENT_PRICE_PROVIDER": "local",
        }
    )

    assert config.shopping_search_provider == "haodanku"
    assert config.shopping_search_local_path is None
    assert config.shopping_compare_provider == "haodanku"
    assert not hasattr(config, "shopping_provider")
    assert not hasattr(config, "product_search_provider")
    assert not hasattr(config, "price_compare_provider")


def test_provider_mode_is_the_single_mock_real_boundary() -> None:
    mock_config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "qwen",
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "http",
            "MULTIMODAL_AGENT_SHOPPING_PROVIDER": "haodanku",
            "QWEN_API_KEY": "must-not-be-used",
            "HAODANKU_API_KEY": "must-not-be-used",
        }
    )

    assert mock_config.provider_mode == "mock"
    assert mock_config.chat_provider == "mock"
    assert mock_config.vision_provider == "mock"
    assert mock_config.image_generation_provider == "mock"
    assert mock_config.search_provider == "mock"
    assert mock_config.shopping_search_provider == "mock"
    assert mock_config.shopping_compare_provider == "mock"

    with pytest.raises(ValueError, match="requires a non-mock"):
        ProviderConfig.from_env({"MULTIMODAL_AGENT_PROVIDER_MODE": "real"})


def test_real_mode_registry_never_loads_mock_provider_tools() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_API_KEY": "test-key",
        }
    )
    runtime = AgentGraphRuntime(config=config)

    assert config.chat_provider == "qwen"
    assert {
        "vision_understanding",
        "shopping_search",
        "weather",
        "calendar_search",
        "calendar_create",
        "contacts_search",
        "web_search",
        "web_fetch",
        "visual_image_search",
        "image_generation",
    }.isdisjoint(runtime.registry.list())


def test_trace_observer_close_propagates_timeout_budget() -> None:
    span_sink = TimeoutAwareLifecycleSink()
    score_sink = TimeoutAwareLifecycleSink()
    store = HookTraceStore(
        HookManager(
            [
                TextOtelTraceObserver(span_sink, enabled=True),
                LangfuseScoreTraceObserver(score_sink, enabled=True),
            ]
        )
    )

    assert store.close(timeout=7.5) is True
    assert span_sink.shutdown_timeout == 7.5
    assert score_sink.shutdown_timeout == 7.5


def test_external_trace_id_maps_to_stable_langfuse_trace_id() -> None:
    trace_id = langfuse_trace_id("trace_example")

    assert trace_id == "38a6c223d755e35a0593e9ea0b7fdb54"
    assert len(trace_id) == 32


def test_canonical_ids_use_uuid7_and_native_w3c_formats() -> None:
    factory = IdFactory(clock_ms=lambda: 1_721_526_400_123, randbits=lambda _bits: 0)

    first = factory.uuid7()
    second = factory.uuid7()

    assert first.version == 7
    assert first.variant == "specified in RFC 4122"
    assert first.int >> 80 == 1_721_526_400_123
    assert second.int > first.int
    run_id = new_run_id()
    session_id = new_session_id()
    trace_id = new_trace_id()
    span_id = new_span_id()

    assert run_id.startswith("run_") and len(run_id) == 36
    assert session_id.startswith("session_") and len(session_id) == 40
    assert UUID(hex=run_id.removeprefix("run_")).version == 7
    assert UUID(hex=session_id.removeprefix("session_")).version == 7
    assert len(trace_id) == 32 and trace_id == trace_id.lower() and int(trace_id, 16) != 0
    assert len(span_id) == 16 and span_id == span_id.lower() and int(span_id, 16) != 0


def test_native_w3c_trace_id_is_not_rehashed_for_langfuse() -> None:
    trace_id = "d7dd01a3493379032b4d5e926fe6e2af"

    assert langfuse_trace_id(trace_id) == trace_id


def test_interactive_provider_latency_controls_are_explicit() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_API_KEY": "test-key",
            "QWEN_CHAT_MODEL": "qwen3.6-flash",
        }
    )

    adapter = create_chat_adapter(config)
    compactor = create_context_compactor(config, adapter)

    assert config.agent_service_text_turn_timeout_seconds == 90.0
    assert config.chat_timeout_seconds == 75.0
    assert config.context_compactor_mode == "deterministic"
    assert config.qwen_chat_enable_thinking is False
    assert isinstance(adapter, OpenAICompatibleChatAdapter)
    assert adapter.timeout_seconds == 75.0
    assert adapter.enable_thinking is False
    assert isinstance(compactor, DeterministicContextCompactor)


def test_qwen_chat_adapter_disables_thinking_in_provider_payload() -> None:
    captured: dict[str, object] = {}

    class Completions:
        def create(self, **payload: object) -> dict[str, object]:
            captured.update(payload)
            return {
                "model": "qwen3.6-flash",
                "choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}],
                "usage": {},
            }

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    adapter = OpenAICompatibleChatAdapter(
        provider="qwen",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="qwen3.6-flash",
        enable_thinking=False,
        client=client,
    )

    result = adapter.chat(ChatRequest(user_id="user", session_id="session", user_query="测试"))

    assert result.response_text == "完成"
    assert captured["extra_body"] == {"enable_thinking": False}


def test_media_client_surfaces_top_level_chat_failure() -> None:
    assert chat_response_error({"code": "FAIL", "message": "Gateway turn timed out"}) == (
        "Gateway turn timed out"
    )


def test_otel_content_export_requires_explicit_local_loopback_configuration() -> None:
    base = {
        "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "true",
        "ASSISTANT_AGENT_OTEL_INCLUDE_CONTENT": "true",
        "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT": "1",
    }

    local = OtlpHttpTextExporterConfig.from_env(
        {**base, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:3000/api/public/otel"}
    )
    remote = OtlpHttpTextExporterConfig.from_env(
        {**base, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://cloud.langfuse.com/api/public/otel"}
    )
    missing_local_opt_in = OtlpHttpTextExporterConfig.from_env(
        {
            **base,
            "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT": "0",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:3000/api/public/otel",
        }
    )

    assert local.include_content is True
    assert remote.include_content is False
    assert missing_local_opt_in.include_content is False


def test_langfuse_mapping_exposes_conversation_and_tool_diagnostics() -> None:
    created_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    common = {
        "trace_id": "trace-tool",
        "run_id": "run-tool",
        "user_id": "user-1",
        "session_id": "session-1",
        "created_at": created_at,
    }
    events = [
        TraceEvent(
            **common,
            node_name="runtime",
            event_type="observability",
            canonical_event="run.started",
            span_id="root-span",
            status="started",
        ),
        TraceEvent(
            **common,
            node_name="assistant",
            event_type="assistant_decision",
            canonical_event="react.decision",
            status="tool_call",
            tool_name="weather",
            attributes={"iteration": 1, "decision_type": "tool_call"},
            output_summary={"decision_type": "tool_call", "reason": "需要查询实时天气。"},
        ),
        TraceEvent(
            **common,
            node_name="tool_executor",
            event_type="observability",
            canonical_event="tool.finished",
            status="succeeded",
            tool_name="weather",
            latency_ms=12,
            input_summary={"field_count": 1, "prompt_length": 7},
            output_summary={"success": True, "result_count": 1, "output_ref": "weather://beijing"},
        ),
        TraceEvent(
            **common,
            node_name="assistant",
            event_type="tool_observation",
            canonical_event="tool.observation",
            status="succeeded",
            tool_name="weather",
            output_summary={"summary": "北京晴，最高温 30 摄氏度。", "output_ref": "weather://beijing"},
        ),
    ]
    conversation = TraceConversationView(
        trace_id="trace-tool",
        user=TraceConversationText(text="北京今天天气怎么样？", chars=10),
        assistant=TraceConversationText(text="北京今天晴。", chars=7),
    )

    spans = build_text_otel_span_specs(events, conversation=conversation)
    by_name = {span.name: span for span in spans}

    assert "北京今天天气怎么样？" in by_name["assistant.runtime"].attributes["langfuse.trace.input"]
    assert "北京今天晴。" in by_name["assistant.runtime"].attributes["langfuse.trace.output"]
    assert '"decision_type":"tool_call"' in by_name["react.decision"].attributes[
        "langfuse.observation.output"
    ]
    assert "gen_ai.tool.name" not in by_name["react.decision"].attributes
    assert '"tool_name":"weather"' in by_name["tool.execute"].attributes[
        "langfuse.observation.input"
    ]
    assert by_name["tool.execute"].attributes["gen_ai.tool.name"] == "weather"
    assert '"result_count":1' in by_name["tool.execute"].attributes[
        "langfuse.observation.output"
    ]
    assert "北京晴" in by_name["tool.observation"].attributes["langfuse.observation.output"]


def test_langfuse_mapping_builds_runtime_iteration_hierarchy_and_exact_local_llm_input() -> None:
    created_at = datetime(2026, 7, 22, tzinfo=timezone.utc)
    common = {
        "trace_id": "4e40c74a09733d59f1cfa5a9eea45fb3",
        "run_id": "run-hierarchy",
        "user_id": "10086",
        "session_id": "agent-service-session",
    }
    events = [
        TraceEvent(
            **common,
            node_name="runtime",
            event_type="observability",
            canonical_event="run.started",
            span_id="1111111111111111",
            status="started",
            created_at=created_at,
        ),
        TraceEvent(
            **common,
            node_name="assistant",
            event_type="observability",
            canonical_event="context.build.finished",
            span_id="2222222222222222",
            status="succeeded",
            latency_ms=5,
            attributes={"iteration": 1},
            output_summary={
                "context_report_v1": {
                    "schema_version": "context_report_v1",
                    "sections": {
                        "request": {
                            "chars": 4,
                            "included": True,
                            "source": "UserRequest.text",
                        }
                    },
                    "total_chars": 4,
                    "selected_tool_names": ["image_generation"],
                    "compression_stage": "none",
                }
            },
            created_at=created_at + timedelta(milliseconds=10),
        ),
        TraceEvent(
            **common,
            node_name="assistant",
            event_type="observability",
            canonical_event="llm.chat.finished",
            span_id="3333333333333333",
            status="succeeded",
            latency_ms=20,
            provider="qwen",
            model="qwen-test",
            attributes={"iteration": 1, "finish_reason": "stop", "wall_latency_ms": 20},
            output_summary={
                "schema_version": "llm_chat_output_v1",
                "response_kind": "assistant_text",
                "content_present": True,
                "content_chars": 42,
                "tool_calls": [],
                "refusal_present": False,
                "refusal_chars": 0,
                "finish_reason": "stop",
                "structured_output": {
                    "schema": "json_object",
                    "validation_status": "valid",
                },
                "error_count": 0,
                "error_codes": [],
            },
            created_at=created_at + timedelta(milliseconds=31),
        ),
        TraceEvent(
            **common,
            node_name="assistant",
            event_type="assistant_decision",
            canonical_event="react.decision",
            status="final_answer",
            attributes={"iteration": 1, "decision_type": "final_answer"},
            created_at=created_at + timedelta(milliseconds=32),
        ),
        TraceEvent(
            **common,
            node_name="runtime",
            event_type="observability",
            canonical_event="run.completed",
            status="completed",
            latency_ms=40,
            created_at=created_at + timedelta(milliseconds=40),
        ),
        TraceEvent(
            **common,
            node_name="agent_service",
            event_type="observability",
            canonical_event="agent_service.turn.finished",
            status="sent",
            latency_ms=45,
            created_at=created_at + timedelta(milliseconds=45),
        ),
        TraceEvent(
            **common,
            node_name="agent_service",
            event_type="observability",
            canonical_event="assistant.turn.summary",
            status="completed",
            output_summary={
                "turn_summary": {
                    "schema_version": "assistant_turn_summary_v1",
                    "trace_id": common["trace_id"],
                    "assistant_run_id": common["run_id"],
                    "gateway_run_id": "gateway-run",
                    "turn_id": "turn-1",
                    "user_id": common["user_id"],
                    "session_id": common["session_id"],
                    "session_turn": 3,
                    "client_type": "run_client",
                    "terminal_status": "completed",
                    "response_present": True,
                    "tool_count": 0,
                    "error_count": 0,
                }
            },
            created_at=created_at + timedelta(milliseconds=46),
        ),
    ]
    conversation = TraceConversationView(
        trace_id=common["trace_id"],
        user=TraceConversationText(text="生成图片", chars=4),
        assistant=TraceConversationText(text="完成", chars=2),
        llm_inputs=[
            TraceLlmInput(
                iteration=1,
                provider="qwen",
                model="qwen-test",
                request={
                    "messages": [
                        {"role": "system", "content": "system prompt"},
                        {"role": "user", "content": "compiled context"},
                    ],
                    "tools": [{"type": "function", "function": {"name": "image_generation"}}],
                },
            )
        ],
    )

    spans = build_text_otel_span_specs(events, conversation=conversation)
    runtime = next(span for span in spans if span.name == "assistant.runtime")
    iteration = next(span for span in spans if span.name == "react.iteration")
    context = next(span for span in spans if span.name == "context.build")
    generation = next(span for span in spans if span.name == "llm.chat")

    assert runtime.parent_span_id is None
    assert not any(span.name == "assistant.turn" for span in spans)
    assert iteration.parent_span_id == runtime.span_id
    assert context.parent_span_id == iteration.span_id
    assert generation.parent_span_id == iteration.span_id
    assert not any(span.name == "agent_service.turn" for span in spans)
    assert '"content":"compiled context"' in generation.attributes["langfuse.observation.input"]
    assert '"name":"image_generation"' in generation.attributes["langfuse.observation.input"]
    generation_output = json.loads(generation.attributes["langfuse.observation.output"])
    assert generation_output["schema_version"] == "llm_chat_output_v1"
    assert generation_output["response_kind"] == "assistant_text"
    assert generation_output["content_present"] is True
    assert generation_output["structured_output"]["validation_status"] == "valid"
    assert "content" not in generation_output
    assert '"context_report_v1"' in context.attributes["langfuse.observation.output"]
    assert '"selected_tool_names":["image_generation"]' in context.attributes[
        "langfuse.observation.output"
    ]
    assert runtime.attributes["assistant_agent.session_scope"] == "agent_service_connection"
    assert runtime.attributes["assistant_agent.turn_id"] == "turn-1"


def test_plain_text_run_completes() -> None:
    sink = ListEventSink()
    state = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=MockChatAdapter(),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    ).run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="你好"),
        event_sink=sink,
    )

    assert state.status == "completed"
    assert state.response is not None and state.response.message
    assert sink.events[0].type == "task_started"
    assert sink.events[-1].type == "final_response"


def test_agent_runtime_system_prompt_is_channel_agnostic() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                message_kind="final_answer",
                response_text='{"response_type":"answer","answer":"你好。"}',
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )

    runtime.run_state(
        UserRequest(
            user_id="user-1",
            session_id="session-1",
            text="你好",
            metadata={"system_prompt_profile": "realtime_phone", "channel": "realtime_phone"},
        )
    )

    prompt = str(adapter.requests[0].messages[0]["content"])
    assert prompt == render_system_instruction(SystemPromptProfile.TEXT_DEFAULT)
    assert {profile.value for profile in SystemPromptProfile} == {"text_default"}
    for channel_term in ("电话", "通话", "口语", "口播", "挂断", "TTS", "WebSocket"):
        assert channel_term not in prompt


def test_unstructured_provider_draft_is_repaired_before_commit() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                message_kind="final_answer",
                response_text="先分析工具目录，再决定怎么回复用户。",
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                message_kind="final_answer",
                response_text='{"response_type":"clarification","answer":"你是想买牛奶吗？"}',
            ),
        ]
    )
    sink = ListEventSink()
    state = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    ).run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="我想麦牛奶"),
        event_sink=sink,
    )

    assert len(adapter.requests) == 2
    assert adapter.requests[1].tools == []
    assert state.response is not None and state.response.message == "你是想买牛奶吗？"
    public_text = "".join(event.text or "" for event in sink.events)
    assert "先分析工具目录" not in public_text
    assert "你是想买牛奶吗" in public_text


def test_native_tool_call_loop_completes_with_observation() -> None:
    memory_store = InMemoryStore()
    memory_store.save(
        MemoryItem(
            memory_id="memory-1",
            user_id="user-1",
            memory_type="preference",
            content={"item": "黑色通勤包"},
            summary="用户喜欢黑色通勤包。",
            created_at=datetime.now(timezone.utc),
        )
    )
    tool_call = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="tool_calls",
        message_kind="tool_call",
        tool_calls=[
            NativeToolCall(
                id="call-1",
                name="memory_retrieval",
                arguments={"query": "通勤包"},
                raw={
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "memory_retrieval",
                        "arguments": '{"query":"通勤包"}',
                    },
                },
            )
        ],
    )
    final_answer = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="stop",
        message_kind="final_answer",
        response_text='{"response_type":"answer","answer":"已结合记忆完成推荐。"}',
    )
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=ScriptedChatAdapter([tool_call, final_answer]),
        memory_store=memory_store,
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="推荐一个通勤包")
    )

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls] == ["memory_retrieval"]
    assert "黑色通勤包" in str(runtime.chat_adapter.requests[1].messages)
    assert state.response is not None
    assert state.response.message == "已结合记忆完成推荐。"
    llm_events = [
        event
        for event in runtime.trace_store.list_by_run(state.run_id)
        if event.canonical_event == "llm.chat.finished"
    ]
    tool_output = llm_events[0].output_summary
    assert tool_output["schema_version"] == "llm_chat_output_v1"
    assert tool_output["response_kind"] == "tool_calls"
    assert tool_output["tool_calls"] == [
        {
            "id": "call-1",
            "name": "memory_retrieval",
            "arguments_summary": {
                "keys": ["query"],
                "field_count": 1,
                "redacted": True,
            },
        }
    ]
    assert "通勤包" not in json.dumps(tool_output, ensure_ascii=False)
    final_output = llm_events[1].output_summary
    assert final_output["response_kind"] == "assistant_text"
    assert final_output["content_present"] is True
    assert final_output["content_chars"] == len(final_answer.response_text)
    assert "已结合记忆完成推荐" not in json.dumps(final_output, ensure_ascii=False)


def test_local_trace_content_captures_compiled_provider_request(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                message_kind="final_answer",
                response_text='{"response_type":"answer","answer":"完成。"}',
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(
            user_id="local-user",
            session_id="local-session",
            text="生成一张图片\n\n使用柔和光线",
        )
    )
    content = get_default_trace_conversation_store().get(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=state.trace_id,
        limit=4000,
        include_llm_inputs=True,
    )

    assert content is not None
    assert len(content.llm_inputs) == 1
    request = content.llm_inputs[0].request
    assert request["messages"] == adapter.requests[0].model_dump(mode="json")["messages"]
    assert request["tools"] == adapter.requests[0].model_dump(mode="json")["tools"]
    assert "provider error" not in str(request["messages"])

    context_event = next(
        event
        for event in runtime.trace_store.list_by_run(state.run_id)
        if event.canonical_event == "context.build.finished"
    )
    report = context_event.output_summary["context_report_v1"]
    assert report["schema_version"] == "context_report_v1"
    assert report["sections"]["request"]["chars"] == len(state.request.text)


def test_real_adapter_uses_langgraph_and_finishes_without_tools_after_budget() -> None:
    tool_call = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="tool_calls",
        message_kind="tool_call",
        tool_calls=[
            NativeToolCall(
                id="call-budget-1",
                name="shopping_search",
                arguments={"query": "通勤耳机", "limit": 2},
                raw={
                    "id": "call-budget-1",
                    "type": "function",
                    "function": {
                        "name": "shopping_search",
                        "arguments": '{"query":"通勤耳机","limit":2}',
                    },
                },
            ),
            NativeToolCall(
                id="call-budget-2",
                name="shopping_search",
                arguments={"query": "降噪耳机", "limit": 2},
                raw={
                    "id": "call-budget-2",
                    "type": "function",
                    "function": {
                        "name": "shopping_search",
                        "arguments": '{"query":"降噪耳机","limit":2}',
                    },
                },
            ),
        ],
    )
    final_answer = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="stop",
        message_kind="final_answer",
        response_text='{"response_type":"answer","answer":"预算内搜索完成。"}',
    )
    adapter = ScriptedChatAdapter([tool_call, final_answer])
    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            agent_graph_mode="assistant_loop",
            max_tool_iterations=1,
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="帮我找通勤耳机"),
        event_sink=sink,
    )

    assert state.status == "completed"
    assert state.response is not None and state.response.message == "预算内搜索完成。"
    assert len(adapter.requests) == 2
    assert [call.tool_name for call in state.tool_calls] == ["shopping_search"]
    assert state.request.metadata["tool_calls_skipped_for_budget"] == 1
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].messages[0]["content"] == adapter.requests[0].messages[0]["content"]
    graph_nodes = [event.node_name for event in sink.events if event.type == "graph_node_started"]
    assert "agent_graph" in graph_nodes


def test_provider_timeout_returns_terminal_retry_response() -> None:
    timeout = ChatResult(
        provider="scripted",
        model="scripted-model",
        errors=[
            ChatProviderError(
                code="provider_timeout",
                message="provider timed out",
                recoverable=True,
            )
        ],
    )
    state = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=ScriptedChatAdapter([timeout]),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    ).run_state(UserRequest(user_id="user-1", session_id="session-1", text="你好"))

    assert state.status == "completed"
    assert state.response is not None
    assert state.response.data["fallback_reason"] == "provider_timeout"
    assert state.response.message


def test_cancelled_run_terminates_without_final_response() -> None:
    sink = ListEventSink()
    state = AgentGraphRuntime(
        config=_offline_config(),
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    ).run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="你好"),
        event_sink=sink,
        cancel_token=CancelledToken(),
    )

    assert state.status == "cancelled"
    assert state.response is None
    assert sink.events[-1].type == "task_cancelled"


def test_core_event_contract_reaches_gateway_frame() -> None:
    accumulator = LLMEventAccumulator()
    accumulator.apply(
        LLMEvent(
            event_type="tool_call_delta",
            provider="scripted",
            tool_call_delta=LLMToolCallDelta(
                index=0,
                id="call-1",
                name_delta="shopping_search",
                arguments_delta='{"query":"耳机"}',
            ),
        )
    )
    assert accumulator.finalize_tool_calls()[0].arguments == {"query": "耳机"}

    realtime_events = map_agent_event_stream(
        AgentEvent(
            type="final_response",
            session_id="session-1",
            run_id="run-1",
            text="处理完成",
        )
    )
    frame = realtime_event_to_frame(
        realtime_events[0],
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
    )

    assert frame is not None
    assert frame["type"] == "stream.chunk"
    assert frame["session_id"] == "session-1"
    assert frame["run_id"] == "run-1"
    assert frame["payload"]["text"] == "处理完成"


def test_session_and_memory_identity_are_isolated() -> None:
    sessions = InMemorySessionStore()
    sessions.touch_run(
        user_id="user-1",
        session_id="shared-session",
        run_id="run-1",
        trace_id="trace-1",
        message_preview="first",
        status="completed",
    )
    sessions.touch_run(
        user_id="user-2",
        session_id="shared-session",
        run_id="run-2",
        trace_id="trace-2",
        message_preview="second",
        status="completed",
    )

    memories = InMemoryStore()
    memories.save(
        MemoryItem(
            memory_id="memory-1",
            user_id="user-1",
            session_id="shared-session",
            memory_type="preference",
            summary="喜欢蓝色",
            content={"preference": "蓝色"},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert sessions.get("user-1", "shared-session").last_run_id == "run-1"
    assert sessions.get("user-2", "shared-session").last_run_id == "run-2"
    assert memories.search(MemoryQuery(user_id="user-1", query="蓝色")).total == 1
    assert memories.search(MemoryQuery(user_id="user-2", query="蓝色")).total == 0
