"""Minimal offline safety net for the assistant's stable runtime boundaries."""

from datetime import datetime, timedelta, timezone
import importlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.system_prompt_policy import render_system_instruction
from assistant_agent.config import ProviderConfig
from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.gateway.runtime_event_mapping import map_agent_event_stream
from assistant_agent.runtime.decision_models import NativeToolCall
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.providers.llm_events import LLMEvent, LLMEventAccumulator, LLMToolCallDelta
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
    MockChatAdapter,
    OpenAICompatibleChatAdapter,
    create_chat_adapter,
)
from assistant_agent.context.compactor import LLMCompactor, create_context_compactor
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.hooks import HookManager, HookTraceStore
from assistant_agent.identifiers import IdFactory, new_run_id, new_session_id, new_span_id, new_trace_id
from assistant_agent.observability.langfuse_scores import (
    LangfuseScoreTraceObserver,
    LangfuseScoreWriterConfig,
)
from assistant_agent.observability.otel_exporter import TextOtelTraceObserver
from assistant_agent.observability.otel_exporter import OtlpHttpTextExporterConfig
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs, langfuse_trace_id
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.observability.trace_conversation import (
    TraceConversationText,
    TraceConversationView,
    TraceLlmInput,
    TraceToolObservation,
    get_default_trace_conversation_store,
)
from assistant_agent.observability.trace_store import TraceEvent
from scripts.run_client import chat_response_error


def _offline_config() -> ProviderConfig:
    return ProviderConfig(langgraph_checkpointer_backend="none")


def _contains_mapping_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(
            _contains_mapping_key(item, key) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_mapping_key(item, key) for item in value)
    return False


def test_default_runtime_policy_requires_missing_tool_inputs_to_be_clarified() -> None:
    assert "工具缺少地点、对象、时间等必要参数时，先向用户澄清" in render_system_instruction()


def test_runtime_policy_groups_dynamic_time_and_location_as_current_environment() -> None:
    instruction = render_system_instruction(
        current_time=datetime(2026, 7, 27, 15, 30, tzinfo=timezone(timedelta(hours=8))),
        current_location=" 上海市  浦东新区 ",
    )

    assert "# 当前环境" in instruction
    assert "本地时间：2026-07-27T15:30:00+08:00" in instruction
    assert "当前位置：上海市 浦东新区。" in instruction
    assert "用户明确指定目标地点时，以用户指定地点为准" in instruction
    assert "# 本地时间" not in instruction


def test_current_location_is_loaded_from_environment() -> None:
    config = ProviderConfig.from_env(
        {"MULTIMODAL_AGENT_CURRENT_LOCATION": " 杭州市 "}
    )

    assert config.current_location == "杭州市"


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


def test_runtime_injects_configured_location_into_system_prompt() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="完成。",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            current_location="北京市海淀区",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="你好")
    )

    assert state.status == "completed"
    assert "当前位置：北京市海淀区。" in adapter.requests[0].messages[0]["content"]


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
    assert set(specs["vision_understanding"].input_schema["properties"]) == {
        "question"
    }
    assert "memory" not in specs
    assert {"memory_search", "memory_get", "memory_save"}.isdisjoint(specs)
    assert (
        runtime.registry.registration_record("calendar_search").plugin_id
        == "personal_assistant_mcp"
    )
    assert (
        runtime.registry.registration_record("calendar_create").plugin_id
        == "personal_assistant_mcp"
    )
    assert (
        runtime.registry.registration_record("python_interpreter").plugin_id
        == "python_execution"
    )
    assert (
        runtime.registry.registration_record("vision_understanding").plugin_id
        == "vision_understanding"
    )
    assert (
        runtime.registry.registration_record("visual_image_search").plugin_id
        == "visual_image_search"
    )
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
        "contacts_search",
        "web_search",
        "web_fetch",
        "visual_image_search",
        "image_generation",
    }.isdisjoint(runtime.registry.list())
    assert {"calendar_search", "calendar_create"}.issubset(runtime.registry.list())


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
    assert config.context_compactor_mode == "off"
    assert config.qwen_chat_enable_thinking is False
    assert isinstance(adapter, OpenAICompatibleChatAdapter)
    assert adapter.timeout_seconds == 75.0
    assert adapter.enable_thinking is False
    assert compactor is None

    enabled_config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_API_KEY": "test-key",
            "QWEN_CHAT_MODEL": "qwen3.6-flash",
            "MULTIMODAL_AGENT_CONTEXT_COMPACTOR": "llm",
        }
    )
    enabled_compactor = create_context_compactor(
        enabled_config,
        create_chat_adapter(enabled_config),
    )
    assert isinstance(enabled_compactor, LLMCompactor)


def test_qwen_chat_adapter_disables_thinking_in_provider_payload() -> None:
    captured: dict[str, object] = {}
    observed: list[dict[str, object]] = []

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

    result = adapter.chat(
        ChatRequest(
            user_id="user",
            session_id="session",
            user_query="测试",
            provider_request_callback=observed.append,
        )
    )

    assert result.response_text == "完成"
    assert captured["extra_body"] == {"enable_thinking": False}
    assert observed == [captured]


def test_media_client_surfaces_top_level_chat_failure() -> None:
    assert chat_response_error({"code": "FAIL", "message": "Gateway turn timed out"}) == (
        "Gateway turn timed out"
    )


def test_otel_export_includes_original_content_without_extra_switches() -> None:
    base = {
        "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "true",
        "LANGFUSE_PUBLIC_KEY": "pk-local",
        "LANGFUSE_SECRET_KEY": "sk-local",
    }

    local = OtlpHttpTextExporterConfig.from_env(base)
    remote = OtlpHttpTextExporterConfig.from_env(
        {**base, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://cloud.langfuse.com/api/public/otel"}
    )
    disabled = OtlpHttpTextExporterConfig.from_env(
        {key: value for key, value in base.items() if key != "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED"}
    )

    assert local.endpoint == "http://localhost:3000/api/public/otel/v1/traces"
    assert local.headers == {"Authorization": "Basic cGstbG9jYWw6c2stbG9jYWw="}
    assert local.service_name == "assistant-agent-local"
    assert local.timeout_seconds == 5.0
    assert local.queue_capacity == 1024
    assert local.include_content is True
    assert remote.include_content is True
    assert disabled.include_content is False

    scores = LangfuseScoreWriterConfig.from_env(
        {
            "ASSISTANT_AGENT_LANGFUSE_SCORE_ENABLED": "true",
            "LANGFUSE_PUBLIC_KEY": "pk-local",
            "LANGFUSE_SECRET_KEY": "sk-local",
        }
    )
    assert scores.scores_url == "http://localhost:3000/api/public/scores"
    assert scores.timeout_seconds == 5.0
    assert scores.queue_capacity == 1024


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
            event_type="assistant_output",
            canonical_event="assistant.output",
            observation_scope="iteration",
            status="tool_call",
            tool_name="weather",
            attributes={"iteration": 1, "output_type": "tool_call"},
            output_summary={"output_type": "tool_call", "reason": "需要查询实时天气。"},
        ),
        TraceEvent(
            **common,
            node_name="tool_executor",
            event_type="observability",
            canonical_event="tool.finished",
            observation_type="span",
            observation_scope="iteration",
            status="succeeded",
            tool_name="weather",
            latency_ms=12,
            input_summary={"location": "Beijing", "days": 1},
            output_summary={
                "success": True,
                "output_ref": "weather://beijing",
                "model_observation": {
                    "location": "Beijing",
                    "forecast": [{"condition": "Clear", "high_c": 30}],
                },
                "data": {
                    "forecast": [{"condition": "Clear", "high_c": 30}],
                    "provider": "mcp:weather",
                    "debug_value": "api_key=test-dev-value",
                },
            },
        ),
        TraceEvent(
            **common,
            node_name="assistant",
            event_type="tool_observation",
            canonical_event="tool.observation",
            observation_type="event",
            observation_scope="iteration",
            status="succeeded",
            tool_name="weather",
            attributes={
                "observation_index": 1,
                "tool_call_id": "call-weather",
                "source_tool_span_id": "span-weather",
            },
            output_summary={"summary": "北京晴，最高温 30 摄氏度。", "output_ref": "weather://beijing"},
        ),
    ]
    conversation = TraceConversationView(
        trace_id="trace-tool",
        user=TraceConversationText(text="北京今天天气怎么样？", chars=10),
        assistant=TraceConversationText(text="北京今天晴。", chars=7),
        tool_observations=[
            TraceToolObservation(
                observation_index=1,
                tool_name="weather",
                observation={
                    "tool_name": "weather",
                    "status": "succeeded",
                    "summary": "北京晴，最高温 30 摄氏度。",
                    "output_ref": "weather://beijing",
                    "structured_output": {
                        "location": "Beijing",
                        "forecast": [{"condition": "Clear", "high_c": 30}],
                    },
                    "redacted": True,
                    "truncated": False,
                },
            )
        ],
    )

    spans = build_text_otel_span_specs(events, conversation=conversation)
    by_name = {span.name: span for span in spans}

    runtime_input = json.loads(
        by_name["agent.runtime"].attributes["langfuse.trace.input"]
    )
    runtime_output = json.loads(
        by_name["agent.runtime"].attributes["langfuse.trace.output"]
    )
    assert runtime_input["content"] == "北京今天天气怎么样？"
    assert runtime_output["content"] == "北京今天晴。"
    assert "assistant.output" not in by_name
    assert by_name["tool.execute"].attributes["gen_ai.tool.name"] == "weather"
    assert json.loads(
        by_name["tool.execute"].attributes["langfuse.observation.input"]
    ) == {"tool_name": "weather", "location": "Beijing", "days": 1}
    tool_output = json.loads(
        by_name["tool.execute"].attributes["langfuse.observation.output"]
    )
    assert tool_output["data"] == {
        "forecast": [{"condition": "Clear", "high_c": 30}],
        "provider": "mcp:weather",
        "debug_value": "api_key=test-dev-value",
    }
    assert "model_observation" not in tool_output
    assert json.loads(
        by_name["tool.observation"].attributes["langfuse.observation.input"]
    ) == {
        "tool_name": "weather",
        "tool_call_id": "call-weather",
        "source_tool_span_id": "span-weather",
    }
    assert (
        by_name["tool.observation"].attributes[
            "assistant_agent.source_tool_span_id"
        ]
        == "span-weather"
    )
    observation_output = json.loads(
        by_name["tool.observation"].attributes["langfuse.observation.output"]
    )
    assert observation_output["structured_output"] == {
        "location": "Beijing",
        "forecast": [{"condition": "Clear", "high_c": 30}],
    }
    assert observation_output["redacted"] is True


def test_langfuse_root_uses_delivered_response_without_rewriting_runtime_final() -> None:
    common = {
        "trace_id": "1234567890abcdef1234567890abcdef",
        "run_id": "run-delivered",
        "user_id": "user-delivered",
        "session_id": "session-delivered",
    }
    events = [
        TraceEvent(
            **common,
            node_name="compose_response",
            event_type="observability",
            canonical_event="response.final",
            observation_type="span",
            status="succeeded",
        ),
        TraceEvent(
            **common,
            node_name="realtime_backend",
            event_type="observability",
            canonical_event="response.delivered",
            observation_type="span",
            status="succeeded",
            attributes={"source": "shopping_detail_v1"},
        ),
    ]
    conversation = TraceConversationView(
        trace_id=common["trace_id"],
        user=TraceConversationText(text="上海", chars=2),
        assistant=TraceConversationText(text="模型最终回复", chars=6),
        delivered=TraceConversationText(text="购物详情", chars=4),
    )

    spans = build_text_otel_span_specs(events, conversation=conversation)
    by_name = {span.name: span for span in spans}

    runtime_output = json.loads(
        by_name["agent.runtime"].attributes["langfuse.trace.output"]
    )
    final_output = json.loads(
        by_name["response.final"].attributes["langfuse.observation.output"]
    )
    delivered_output = json.loads(
        by_name["response.delivered"].attributes["langfuse.observation.output"]
    )
    assert runtime_output["content"] == "购物详情"
    assert final_output["content"] == "模型最终回复"
    assert delivered_output["content"] == "购物详情"
    assert delivered_output["source"] == "shopping_detail_v1"


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
            observation_type="span",
            observation_scope="iteration",
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
            observation_type="generation",
            observation_scope="iteration",
            span_id="3333333333333333",
            status="succeeded",
            latency_ms=20,
            provider="qwen",
            model="qwen-test",
            attributes={"iteration": 1, "finish_reason": "stop", "wall_latency_ms": 20},
            created_at=created_at + timedelta(milliseconds=31),
        ),
        TraceEvent(
            **common,
            node_name="assistant",
            event_type="assistant_output",
            canonical_event="assistant.output",
            observation_type="event",
            observation_scope="iteration",
            status="text",
            attributes={"iteration": 1, "output_type": "text"},
            created_at=created_at + timedelta(milliseconds=32),
        ),
        TraceEvent(
            **common,
            node_name="memory",
            event_type="observability",
            canonical_event="memory.ingestion.finished",
            observation_type="span",
            observation_name="memory.turn_ingestion",
            span_id="4444444444444444",
            status="succeeded",
            created_at=created_at + timedelta(milliseconds=33),
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
    runtime = next(span for span in spans if span.name == "agent.runtime")
    iteration = next(span for span in spans if span.name == "react.iteration")
    context = next(span for span in spans if span.name == "context.build")
    generation = next(span for span in spans if span.name == "llm.chat")
    memory_ingestion = next(
        span for span in spans if span.name == "memory.turn_ingestion"
    )

    assert runtime.parent_span_id is None
    assert runtime.attributes["langfuse.trace.name"] == "assistant.turn"
    assert not any(span.name == "assistant.turn" for span in spans)
    assert iteration.parent_span_id == runtime.span_id
    assert context.parent_span_id == iteration.span_id
    assert generation.parent_span_id == iteration.span_id
    assert memory_ingestion.parent_span_id == runtime.span_id
    assert not any(span.name == "agent_service.turn" for span in spans)
    generation_input = json.loads(generation.attributes["langfuse.observation.input"])
    assert isinstance(generation_input, dict)
    assert generation_input["messages"][1]["content"] == "compiled context"
    assert generation_input["tools"][0]["function"]["name"] == "image_generation"
    assert not _contains_mapping_key(generation_input, "user_id")
    assert "langfuse.observation.output" not in generation.attributes
    context_output = json.loads(
        context.attributes["langfuse.observation.output"]
    )
    assert context_output["context_report_v1"]["selected_tool_names"] == [
        "image_generation"
    ]
    assert runtime.attributes["assistant_agent.session_scope"] == "agent_service_connection"
    assert runtime.attributes["assistant_agent.turn_id"] == "turn-1"


def test_langfuse_mapping_uses_declared_observation_contract_without_event_allowlist() -> None:
    event = TraceEvent(
        trace_id="1234567890abcdef1234567890abcdef",
        run_id="run-memory-contract",
        node_name="memory",
        event_type="observability",
        canonical_event="memory.daily.append.finished",
        observation_type="span",
        observation_name="memory.daily.append",
        status="succeeded",
        span_id="0123456789abcdef",
    )

    spans = build_text_otel_span_specs([event])

    observation = next(span for span in spans if span.name == "memory.daily.append")
    assert observation.attributes["langfuse.observation.type"] == "span"
    assert observation.attributes["assistant_agent.canonical_event"] == (
        "memory.daily.append.finished"
    )


def test_plain_text_run_completes() -> None:
    sink = ListEventSink()
    state = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=MockChatAdapter(),
        session_store=InMemorySessionStore(),
    ).run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="你好"),
        event_sink=sink,
    )

    assert state.status == "completed"
    assert state.response is not None and state.response.message
    assert sink.events[0].type == "task_started"
    assert sink.events[-1].type == "final_response"


def test_provider_native_text_is_committed_without_repair_call() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="先分析工具目录，再决定怎么回复用户。",
            ),
        ]
    )
    sink = ListEventSink()
    state = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    ).run_state(
        UserRequest(user_id="user-1", session_id="session-1", text="我想麦牛奶"),
        event_sink=sink,
    )

    assert len(adapter.requests) == 1
    assert state.response is not None
    assert state.response.message == "先分析工具目录，再决定怎么回复用户。"
    assert state.response.followup_question is None
    public_text = "".join(event.text or "" for event in sink.events)
    assert "先分析工具目录" in public_text


def test_truncated_provider_text_is_not_committed_as_complete_answer() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="length",
                response_text="这是一段未完成的回答",
            )
        ]
    )

    state = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    ).run_state(UserRequest(user_id="user-1", session_id="session-1", text="详细回答"))

    assert state.response is not None
    assert state.response.message == "抱歉，刚才模型的回答被截断了，请缩短问题或让我分段回答。"
    assert "这是一段未完成的回答" not in state.response.message


def test_provider_request_fallback_is_captured_without_content_switch() -> None:
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="完成。",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=_offline_config(),
        chat_adapter=adapter,
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
        tool_calls=[
            NativeToolCall(
                id="call-budget-1",
                name="shopping_search",
                arguments={"query": "通勤耳机"},
                raw={
                    "id": "call-budget-1",
                    "type": "function",
                    "function": {
                        "name": "shopping_search",
                        "arguments": '{"query":"通勤耳机"}',
                    },
                },
            ),
            NativeToolCall(
                id="call-budget-2",
                name="shopping_search",
                arguments={"query": "降噪耳机"},
                raw={
                    "id": "call-budget-2",
                    "type": "function",
                    "function": {
                        "name": "shopping_search",
                        "arguments": '{"query":"降噪耳机"}',
                    },
                },
            ),
        ],
    )
    final_answer = ChatResult(
        provider="scripted",
        model="scripted-model",
        finish_reason="stop",
        response_text="预算内搜索完成。",
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


def test_session_and_mem0_identity_are_isolated() -> None:
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

    memory_identity_1 = bind_mem0_identity(
        RequestIdentity.for_user(
            user_id="user-1",
            session_id="shared-session",
        ),
        namespace="tests",
    )
    memory_identity_2 = bind_mem0_identity(
        RequestIdentity.for_user(
            user_id="user-2",
            session_id="shared-session",
        ),
        namespace="tests",
    )

    assert sessions.get("user-1", "shared-session").last_run_id == "run-1"
    assert sessions.get("user-2", "shared-session").last_run_id == "run-2"
    assert memory_identity_1.user_id != memory_identity_2.user_id
    assert memory_identity_1.agent_id == memory_identity_2.agent_id
