"""Offline coverage for the conflicting-receipts foundational Agent Task."""

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


class _ReceiptConflictChat:
    provider = "scripted"
    model = "receipt-conflict"

    def __init__(self) -> None:
        self._results = iter(
            [
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="read-original",
                            name="file_read",
                            arguments={"path": "invoice-original.txt"},
                        ),
                        NativeToolCall(
                            id="read-copy",
                            name="file_read",
                            arguments={"path": "invoice-copy.txt"},
                        ),
                        NativeToolCall(
                            id="read-payment",
                            name="file_read",
                            arguments={"path": "payment-record.txt"},
                        ),
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=(
                        "目前发票凭证支持860元。invoice-copy.txt 与原件的"
                        "发票号码、航班和金额相同，是重复副本，不能重复计入。"
                        "支付记录为920元，比发票多60元；材料只说明包含服务费，"
                        "没有单独服务费凭证或明细，因此这60元暂不能确认，"
                        "需要补充服务费发票或费用明细。"
                    ),
                ),
            ]
        )
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def test_receipt_conflict_task_declares_one_foundational_capability() -> None:
    task = load_task("file_conflicting_receipts_resolution")

    assert task.capability == "conflicting_document_evidence_resolution"
    assert task.request.metadata == {}
    assert task.environment.endswith(
        ".file_conflicting_receipts_resolution.environment:"
        "FileConflictingReceiptsEnvironment"
    )
    assert task.grader.endswith(
        ".file_conflicting_receipts_resolution.grader:grade"
    )
    assert set(task.tags) == {
        "readonly",
        "file",
        "multi-document",
        "conflict",
    }


def test_receipt_conflict_environment_is_readonly_isolated_and_complete() -> None:
    task = load_task("file_conflicting_receipts_resolution")
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
        "controlled_receipt_fixture",
        "isolated_state_boundary",
    }
    assert len(expectations) == description["registered_tool_count"]
    assert {"web_search", "web_fetch"}.isdisjoint(expectations)
    assert expectations["file_read"].required is True
    assert expectations["file_read"].expected_result == "success"
    assert environment.describe()["writes"] is False


def test_receipt_conflict_environment_runs_active_runtime_offline() -> None:
    task = load_task("file_conflicting_receipts_resolution")
    environment_type = load_entrypoint(task.environment)
    environment = environment_type(
        config=ProviderConfig(
            provider_mode="mock",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=_ReceiptConflictChat(),
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
        "file_read",
    ]
    assert [
        item.input["path"] for item in execution.evidence.tool_executions
    ] == [
        "invoice-original.txt",
        "invoice-copy.txt",
        "payment-record.txt",
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


def test_receipt_conflict_calibration_distinguishes_complete_and_wrong_answers() -> None:
    task = load_task("file_conflicting_receipts_resolution")
    results = run_calibration(
        task,
        load_labeled_calibration_judge(task),
    )

    assert [item.fixture_id for item in results] == [
        "resolves_duplicate_and_gap",
        "double_counts_duplicate_invoice",
        "omits_payment_gap",
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
