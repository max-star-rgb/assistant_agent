"""Controlled runtime environment for conversational response quality."""

from __future__ import annotations

from dataclasses import replace

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.trace_context import RuntimeTraceContext
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import (
    EnvironmentValidation,
    RunEvidence,
    TaskExecution,
    TaskSpec,
    ToolOutcomeExpectation,
)
from evals.agent.evidence import (
    available_tools,
    provider_result_kinds,
    tool_executions,
    validation_results,
)
from evals.agent.grading import environment_validation, rule_assertion
from evals.agent.provider_gate import validate_real_chat_config


_PRIOR_USER_TEXT = (
    "我在考虑把团队周报从每人写一份改成周五开半小时同步会。"
    "团队只有六个人，最近项目变化又比较快。"
)
_PRIOR_ASSISTANT_TEXT = (
    "两种方式各有取舍。书面周报便于异步留痕，同步会更适合快速对齐。"
    "结合团队规模不大、近期变化快，我更倾向先试同步会，但保留简短纪要。"
)


class ConversationStyleEnvironment:
    """Real Chat Provider with fixed history and no available tools."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter

    def describe(self) -> dict[str, object]:
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "conversation_history": "simulated:conversation_style_v1",
            "response_style": "conversation",
            "allowed_tools": [],
            "writes": False,
            "state_reset": "per_task_run",
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        expectations = self.tool_outcome_expectations()
        return environment_validation(
            {
                "isolated_empty_tool_registry": rule_assertion(
                    registry.sealed and registry.list() == [],
                    f"sealed={registry.sealed}, registered_tools={registry.list()}",
                    label="工具注册表为空且保持隔离",
                ),
                "outcome_contract_matches_registry": rule_assertion(
                    expectations == [] and registry.list() == [],
                    (
                        f"expectation_count={len(expectations)}, "
                        f"registered_tools={registry.list()}"
                    ),
                    label="空工具结果预期覆盖注册表",
                ),
                "fixed_conversation_context": rule_assertion(
                    bool(_PRIOR_USER_TEXT and _PRIOR_ASSISTANT_TEXT),
                    "固定合成上一轮包含用户问题和助手判断。",
                    label="合成对话上下文完整",
                ),
                "stateless_boundary": rule_assertion(
                    True,
                    "运行时使用 in-memory session/trace store 且无可用工具。",
                    label="任务保持无副作用且状态隔离",
                ),
            }
        )

    def tool_outcome_expectations(self) -> list[ToolOutcomeExpectation]:
        return []

    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest,
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution:
        self.validate().require_valid()
        resolved_request = UserRequest.model_validate(request)
        if resolved_request.response_style != "conversation":
            raise RuntimeError(
                "conversation_style_quality requires response_style=conversation."
            )
        config = self.config or ProviderConfig.from_env()
        if self.chat_adapter is None:
            validate_real_chat_config(config)
        isolated = replace(
            config,
            mem0_base_url=None,
            conversation_history_backend="memory",
            langgraph_checkpointer_backend="none",
            durable_tasks_enabled=False,
            durable_task_worker_enabled=False,
        )
        metadata = dict(resolved_request.metadata)
        metadata.update(
            {
                "conversation_history": [
                    {
                        "user_text": _PRIOR_USER_TEXT,
                        "assistant_text": _PRIOR_ASSISTANT_TEXT,
                        "run_id": "conversation-style-prior-run",
                        "trace_id": "conversation-style-prior-trace",
                    }
                ],
                "conversation_context_text": "",
                "conversation_context_recent_turns": 1,
                "conversation_context_recent_tokens": 0,
                "conversation_context_recent_token_budget": 0,
                "conversation_context_compacted_turns": 0,
                "conversation_context_compacted": False,
                "conversation_context_token_aware": False,
                "conversation_turn_index": 2,
            }
        )
        runtime_request = resolved_request.model_copy(
            update={"metadata": metadata},
            deep=True,
        )
        runtime = AgentGraphRuntime(
            config=isolated,
            registry=self._build_registry(),
            chat_adapter=self.chat_adapter,
            trace_store=InMemoryTraceStore(),
            session_store=InMemorySessionStore(),
        )
        try:
            state = runtime.run_state(
                runtime_request,
                trace_context=RuntimeTraceContext(
                    trace_id=trace_id,
                    parent_span_id=parent_span_id,
                ),
            )
            events = runtime.trace_store.list_by_run(state.run_id)
        finally:
            runtime.close()
        evidence = RunEvidence(
            task_id=task.id,
            run_id=state.run_id,
            trace_id=state.trace_id,
            terminal_status=state.status,
            response=(
                state.response.model_dump(mode="json")
                if state.response is not None
                else None
            ),
            available_tools=available_tools(state, events),
            tool_executions=tool_executions(events),
            validation_results=validation_results(events),
            initial_state={},
            final_state={},
            state_diff={
                "added": [],
                "modified": [],
                "deleted": [],
            },
            trace_event_names=[
                event.canonical_event
                for event in events
                if event.canonical_event is not None
            ],
            provider_result_kinds=provider_result_kinds(events),
        )
        return TaskExecution(evidence=evidence, trace_events=events)

    @staticmethod
    def _build_registry() -> ToolRegistry:
        registry = ToolRegistry()
        registry.seal()
        return registry
