"""Offline coverage for the missing-receipt foundational Agent Task."""

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


class _MissingReceiptChat:
    provider = "scripted"
    model = "missing-receipt"

    def __init__(self) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="read-expense-summary",
                            name="file_read",
                            arguments={"path": "expense-summary.txt"},
                        ),
                        NativeToolCall(
                            id="read-taxi-receipt",
                            name="file_read",
                            arguments={"path": "taxi-receipt.txt"},
                        ),
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=(
                        "材料还不完整。目前只有出租车行程单为135元提供了"
                        "凭证支持。汇总表列出酒店680元，但它明确不是发票或"
                        "付款凭证，因此酒店费用仍需补充酒店发票或合格付款凭证；"
                        "在补齐前不能把汇总中的815元都视为已有凭证支持。"
                    ),
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def test_missing_receipt_task_declares_one_foundational_capability() -> None:
    task = load_task("file_missing_receipt_clarification")

    assert task.capability == "missing_document_evidence_clarification"
    assert task.request.metadata == {}
    assert task.environment.endswith(
        ".file_missing_receipt_clarification.environment:FileMissingReceiptEnvironment"
    )
    assert task.grader.endswith(".file_missing_receipt_clarification.grader:grade")
    assert set(task.tags) == {
        "readonly",
        "file",
        "multi-document",
        "clarification",
    }


def test_missing_receipt_environment_is_readonly_isolated_and_complete() -> None:
    task = load_task("file_missing_receipt_clarification")
    environment = load_entrypoint(task.environment)()

    validation = environment.validate()
    description = environment.describe()
    expectations = {
        item.tool_name: item for item in environment.tool_outcome_expectations()
    }

    assert validation.passed is True
    assert set(validation.checks) >= {
        "registry_sealed",
        "controlled_receipt_fixture",
        "isolated_state_boundary",
    }
    assert len(expectations) == description["registered_tool_count"]
    assert {"web_search", "web_fetch"}.isdisjoint(expectations)
    assert expectations["file_read"].required is True
    assert expectations["file_read"].expected_result == "success"
    assert environment.describe()["writes"] is False


def test_missing_receipt_environment_runs_active_runtime_offline() -> None:
    task = load_task("file_missing_receipt_clarification")
    environment_type = load_entrypoint(task.environment)
    environment = environment_type(
        config=ProviderConfig(
            provider_mode="mock",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=_MissingReceiptChat(),
    )

    execution = environment.execute(
        task=task,
        request=task.request,
        trace_id=TRACE_ID,
        parent_span_id=PARENT_SPAN_ID,
    )

    assert execution.evidence.terminal_status == "completed"
    assert [item.name for item in execution.evidence.tool_executions] == [
        "file_read",
        "file_read",
    ]
    assert [item.input["path"] for item in execution.evidence.tool_executions] == [
        "expense-summary.txt",
        "taxi-receipt.txt",
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


def test_missing_receipt_calibration_distinguishes_evidence_gaps() -> None:
    task = load_task("file_missing_receipt_clarification")
    results = run_calibration(
        task,
        load_labeled_calibration_judge(task),
    )

    assert [item.fixture_id for item in results] == [
        "identifies_supported_amount_and_missing_receipt",
        "treats_summary_as_complete_evidence",
        "omits_supported_taxi_amount",
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
