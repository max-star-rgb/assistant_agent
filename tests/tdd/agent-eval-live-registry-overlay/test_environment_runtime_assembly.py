from __future__ import annotations

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import TaskSpec
from evals.agent.environment_base import ControlledTaskEnvironment
from evals.agent.registry_overlay import EvalToolReplacement


class ScriptedCalendarChatAdapter:
    provider = "scripted"
    model = "scripted-calendar"

    def __init__(self) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="calendar-search-call",
                            name="calendar_search",
                            arguments={"query": "today"},
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text="没有找到日历事件。",
                ),
            ]
        )

    def chat(self, request):
        del request
        return next(self._results)


class ProductionOverlayEnvironment(ControlledTaskEnvironment):
    dependency_label = "live-with-controlled-calendar"

    def __init__(self, **kwargs) -> None:
        self.replacement_hook_calls = 0
        super().__init__(**kwargs)

    def required_successes(self) -> tuple[str, ...]:
        return ("calendar_search",)

    def tool_replacements(
        self,
        production_registry: ToolRegistry,
    ) -> tuple[EvalToolReplacement, ...]:
        self.replacement_hook_calls += 1
        return (
            EvalToolReplacement(
                tool_name="calendar_search",
                tool=production_registry.get("calendar_search"),
                reason="stable calendar fixture",
                source_ref="tests:calendar-search",
            ),
        )


def _task() -> TaskSpec:
    return TaskSpec(
        id="production_overlay_probe",
        description="production overlay probe",
        capability="production registry overlay",
        request=UserRequest(
            user_id="eval-user",
            session_id="eval-session",
            text="查一下今天的日历。",
        ),
        environment="tests:ProductionOverlayEnvironment",
    )


def test_static_validation_does_not_build_or_transform_live_registry() -> None:
    environment = ProductionOverlayEnvironment(
        config=ProviderConfig(provider_mode="mock"),
    )

    validation = environment.validate()

    assert validation.passed is True
    assert environment.replacement_hook_calls == 0
    assert environment.runtime_assembly is None
    assert environment.describe()["registered_tool_count"] is None
    assert [
        item.model_dump() for item in environment.tool_outcome_expectations()
    ] == [
        {
            "tool_name": "calendar_search",
            "required": True,
            "expected_result": "success",
            "error_code": None,
        }
    ]


def test_execute_overlays_runtime_registry_and_records_runtime_assembly() -> None:
    environment = ProductionOverlayEnvironment(
        config=ProviderConfig(provider_mode="mock"),
        chat_adapter=ScriptedCalendarChatAdapter(),
    )

    execution = environment.execute(
        task=_task(),
        request=_task().request,
        trace_id="1" * 32,
        parent_span_id="2" * 16,
    )

    assert environment.replacement_hook_calls == 1
    assert environment.runtime_assembly is not None
    assert environment.runtime_assembly.registry.sealed is True
    assert environment.runtime_assembly.provenance[
        "calendar_search"
    ].dependency_mode == "controlled_replacement"
    assert execution.evidence.terminal_status == "completed"
    assert "calendar_search" in execution.evidence.available_tools
    assert set(execution.evidence.available_tools).issubset(
        environment.runtime_assembly.registry.list()
    )
    assert len(execution.evidence.tool_executions) == 1
    tool_execution = execution.evidence.tool_executions[0]
    assert tool_execution.name == "calendar_search"
    assert tool_execution.status == "succeeded"
    assert tool_execution.dependency_mode == "controlled_replacement"
    assert tool_execution.production_source_ref
    assert tool_execution.replacement_source_ref == "tests:calendar-search"


def test_runtime_registry_validation_happens_before_chat() -> None:
    class MissingToolEnvironment(ProductionOverlayEnvironment):
        def required_successes(self) -> tuple[str, ...]:
            return ("missing_tool",)

    environment = MissingToolEnvironment(
        config=ProviderConfig(provider_mode="mock"),
        chat_adapter=ScriptedCalendarChatAdapter(),
    )

    try:
        environment.execute(
            task=_task(),
            request=_task().request,
            trace_id="3" * 32,
            parent_span_id="4" * 16,
        )
    except RuntimeError as exc:
        assert "Environment validation failed" in str(exc)
    else:
        raise AssertionError("missing required runtime Tool must fail closed")

    assert environment.replacement_hook_calls == 1
