"""Offline Runtime-to-evaluator vertical acceptance for Calendar."""

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.eval.contracts import evidence_from_runtime_state
from assistant_agent.eval.evaluators.calendar_closed_loop import (
    CalendarClosedLoopCase,
    CalendarEventExpectation,
    evaluate_calendar_closed_loop,
)
from assistant_agent.eval.fixtures.calendar import (
    CalendarEvalCreateTool,
    CalendarEvalEnvironment,
    EvalCalendarEvent,
)
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.tools.registry import ToolRegistry


class _ScriptedCalendarChatAdapter:
    provider = "scripted"
    model = "scripted-calendar"

    def __init__(self) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    usage={
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                    tool_calls=[
                        NativeToolCall(
                            id="calendar-eval-call",
                            name="calendar_create",
                            arguments={
                                "title": "洗牙",
                                "start_time": "2026-07-25T15:00:00+08:00",
                                "end_time": "2026-07-25T16:00:00+08:00",
                                "location": "静安牙科诊所",
                                "notes": "提前十分钟到",
                            },
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    usage={
                        "prompt_tokens": 8,
                        "completion_tokens": 3,
                        "total_tokens": 11,
                    },
                    response_text=(
                        "已创建洗牙，时间是 2026-07-25T15:00:00+08:00，"
                        "地点是静安牙科诊所。"
                    ),
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def test_runtime_rollout_is_scored_from_trace_and_final_state() -> None:
    environment = CalendarEvalEnvironment(
        [
            EvalCalendarEvent(
                event_id="existing-team-sync",
                title="团队同步",
                start_time="2026-07-25T10:00:00+08:00",
                end_time="2026-07-25T10:30:00+08:00",
                location="线上",
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(CalendarEvalCreateTool(environment))
    registry.seal()
    trace_store = InMemoryTraceStore()
    chat_adapter = _ScriptedCalendarChatAdapter()
    runtime = AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=chat_adapter,
        trace_store=trace_store,
        session_store=InMemorySessionStore(),
    )
    initial_state = environment.snapshot()
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="eval-calendar-user",
                session_id="eval-calendar-session",
                text="创建已确认的洗牙日历事件。",
                metadata={
                    "tool_visibility": {
                        "enabled_tools": ["calendar_create"],
                    },
                    "tool_confirmation": {
                        "confirmed": True,
                        "tool_name": "calendar_create",
                    },
                },
            )
        )
    finally:
        runtime.close()
    final_state = environment.snapshot()
    evidence = evidence_from_runtime_state(
        case_id="daily_simple_015_create_dentist_event",
        state=state,
        trace_events=trace_store.list_by_run(state.run_id),
        initial_state=initial_state,
        final_state=final_state,
        state_diff=environment.diff(initial_state, final_state),
    )
    case = CalendarClosedLoopCase(
        id=evidence.case_id,
        required_event=CalendarEventExpectation(
            title="洗牙",
            start_time="2026-07-25T15:00:00+08:00",
            end_time="2026-07-25T16:00:00+08:00",
            location="静安牙科诊所",
            notes="提前十分钟到",
        ),
        forbidden_tools=["calendar_search", "web_search"],
        response_facts=[
            "2026-07-25T15:00:00+08:00",
            "静安牙科诊所",
        ],
    )

    report = evaluate_calendar_closed_loop(case, evidence)

    assert state.status == "completed"
    assert len(chat_adapter.requests) == 2
    assert [call.tool_name for call in state.tool_calls] == ["calendar_create"]
    assert evidence.usage == {"input": 18, "output": 5, "total": 23}
    assert report.score("agent.strict_pass").value == 1.0, report.model_dump()
