"""Offline coverage for the email/file reconciliation foundational Task."""

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.decision_models import NativeToolCall
from evals.agent.calibration import (
    load_labeled_calibration_judge,
    run_calibration,
)
from evals.agent.loader import load_entrypoint, load_task


TRACE_ID = "0123456789abcdef0123456789abcdef"
PARENT_SPAN_ID = "0123456789abcdef"


class _EmailFileReconciliationChat:
    provider = "scripted"
    model = "email-file-reconciliation"

    def __init__(self) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="search-adjustment-email",
                            name="email_search",
                            arguments={
                                "query": 'subject:"酒店订单调整"'
                            },
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="read-adjustment-email",
                            name="email_read",
                            arguments={
                                "message_ids": [
                                    "booking-adjustment-20260724"
                                ]
                            },
                        ),
                        NativeToolCall(
                            id="read-hotel-invoice",
                            name="file_read",
                            arguments={"path": "hotel-invoice.txt"},
                        ),
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=(
                        "本地发票记录原始金额720元。酒店订单调整邮件说明"
                        "因一晚房型降级已退款40元，因此最终净支出为680元。"
                        "三个数字分别代表调整前发票金额、退款金额和退款后的"
                        "实际净支出。"
                    ),
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class _WrongEmailQueryChat:
    provider = "scripted"
    model = "wrong-email-query"

    def __init__(self) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="search-wrong-subject",
                            name="email_search",
                            arguments={"query": 'subject:"普通通知"'},
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text="没有找到相关邮件。",
                ),
            ]
        )

    def chat(self, request: ChatRequest) -> ChatResult:
        del request
        return next(self._results)


class _WrongMessageIdChat:
    provider = "scripted"
    model = "wrong-message-id"

    def __init__(self) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="read-unsearched-email",
                            name="email_read",
                            arguments={"message_ids": ["guessed-message-id"]},
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text="无法读取该邮件。",
                ),
            ]
        )

    def chat(self, request: ChatRequest) -> ChatResult:
        del request
        return next(self._results)


def test_email_file_reconciliation_task_declares_one_capability() -> None:
    task = load_task("email_file_booking_amount_reconciliation")

    assert task.capability == "cross_source_evidence_reconciliation"
    assert task.request.metadata == {}
    assert task.environment.endswith(
        ".email_file_booking_amount_reconciliation.environment:"
        "EmailFileBookingAmountEnvironment"
    )
    assert task.grader.endswith(
        ".email_file_booking_amount_reconciliation.grader:grade"
    )
    assert set(task.tags) == {
        "readonly",
        "email",
        "file",
        "multi-tool",
        "reconciliation",
    }


def test_email_file_reconciliation_environment_controls_all_sources() -> None:
    task = load_task("email_file_booking_amount_reconciliation")
    environment = load_entrypoint(task.environment)()

    validation = environment.validate()
    description = environment.describe()
    expectations = {
        item.tool_name: item
        for item in environment.tool_outcome_expectations()
    }

    assert validation.passed is True
    assert set(validation.checks) == {
        "full_tool_registry",
        "outcome_contract_matches_registry",
        "controlled_invoice_fixture",
        "controlled_email_fixture",
        "isolated_state_boundary",
    }
    assert len(expectations) == description["registered_tool_count"]
    assert {"web_search", "web_fetch"}.isdisjoint(expectations)
    for tool_name in ("email_search", "email_read", "file_read"):
        assert expectations[tool_name].required is True
        assert expectations[tool_name].expected_result == "success"
    assert environment.describe()["writes"] is False


def test_email_file_reconciliation_runs_active_runtime_offline() -> None:
    task = load_task("email_file_booking_amount_reconciliation")
    environment_type = load_entrypoint(task.environment)
    environment = environment_type(
        config=ProviderConfig(
            provider_mode="mock",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=_EmailFileReconciliationChat(),
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    assert execution.evidence.terminal_status == "completed"
    assert [item.name for item in execution.evidence.tool_executions] == [
        "email_search",
        "email_read",
        "file_read",
    ]
    assert all(
        item.terminal_event == "tool.finished"
        for item in execution.evidence.tool_executions
    )
    assert execution.evidence.initial_state == {}
    assert execution.evidence.final_state == {}
    assert execution.evidence.state_diff == {
        "added": [],
        "modified": [],
        "deleted": [],
    }


def test_email_file_environment_returns_empty_for_unmatched_subject() -> None:
    task = load_task("email_file_booking_amount_reconciliation")
    environment_type = load_entrypoint(task.environment)
    environment = environment_type(
        config=ProviderConfig(
            provider_mode="mock",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=_WrongEmailQueryChat(),
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    search = execution.evidence.tool_executions[0]
    assert search.name == "email_search"
    assert search.terminal_event == "tool.finished"
    assert (
        search.output.get("model_observation", {}).get("matches", [])
        == []
    )


def test_email_file_environment_rejects_unsearched_message_id() -> None:
    task = load_task("email_file_booking_amount_reconciliation")
    environment_type = load_entrypoint(task.environment)
    environment = environment_type(
        config=ProviderConfig(
            provider_mode="mock",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=_WrongMessageIdChat(),
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    read = execution.evidence.tool_executions[0]
    assert read.name == "email_read"
    assert read.terminal_event == "tool.failed"
    assert read.output["data"]["errors"] == [
        {
            "code": "message_not_found",
            "message": "指定邮件不存在于受控邮箱结果中。",
            "recoverable": False,
        }
    ]


def test_email_file_reconciliation_calibration_separates_accuracy_and_completeness() -> None:
    task = load_task("email_file_booking_amount_reconciliation")
    results = run_calibration(
        task,
        load_labeled_calibration_judge(task),
    )

    assert [item.fixture_id for item in results] == [
        "reconciles_invoice_refund_and_net",
        "ignores_refund_email",
        "states_net_without_explanation",
    ]
    assert all(item.matched for item in results)
    assert results[0].dimensions == {
        "tool_execution": True,
        "tool_semantics": True,
        "grounding": True,
        "response_quality": True,
    }
    assert results[1].dimensions["grounding"] is False
    assert results[1].dimensions["response_quality"] is False
    assert results[2].dimensions["grounding"] is True
    assert results[2].dimensions["response_quality"] is False
