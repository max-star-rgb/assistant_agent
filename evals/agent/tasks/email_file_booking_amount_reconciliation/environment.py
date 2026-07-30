"""Controlled email/file booking reconciliation Environment."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.plugins.builtin.email_access.backend import (
    MockEmailBackend,
)
from assistant_agent.tools.plugins.builtin.email_access.models import (
    EmailProviderError,
    EmailReadRequest,
    EmailReadResult,
    EmailSearchMatch,
    EmailSearchRequest,
    EmailSearchResult,
)
from assistant_agent.tools.plugins.builtin.email_access.tools import (
    EmailReadTool,
    EmailSearchTool,
)
from assistant_agent.tools.plugins.builtin.local_file_access.tool import (
    LocalFileReadTool,
)
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import (
    EnvironmentValidation,
    TaskExecution,
    TaskSpec,
    ToolOutcomeExpectation,
)
from evals.agent.grading import environment_validation, rule_assertion
from evals.agent.task_support import (
    build_controlled_registry,
    execute_isolated_runtime,
    outcome_expectations,
)


INVOICE_PATH = "hotel-invoice.txt"
INVOICE_CONTENT = (
    "酒店电子发票\n"
    "订单号：HOTEL-2026-0722\n"
    "入住人：王晨\n"
    "住宿日期：2026-07-22 至 2026-07-25\n"
    "原始发票金额：720.00元\n"
)
EMAIL_MESSAGE_ID = "booking-adjustment-20260724"
EMAIL_CONTENT = (
    "Message ID: booking-adjustment-20260724\n"
    "Subject: 酒店订单调整\n"
    "From: booking@example.test\n"
    "Date: 2026-07-24T10:00:00+08:00\n\n"
    "--- BODY ---\n"
    "订单 HOTEL-2026-0722 原支付720.00元。"
    "因一晚房型降级，40.00元已原路退款，最终净支出为680.00元。"
)


class _BookingAdjustmentEmailBackend(MockEmailBackend):
    provider = "eval:booking-adjustment-v1"

    def search(self, request: EmailSearchRequest) -> EmailSearchResult:
        matches = (
            [
                EmailSearchMatch(
                    message_id=EMAIL_MESSAGE_ID,
                    thread_id="booking-adjustment-thread",
                )
            ]
            if "酒店订单调整" in request.query
            else []
        )
        return EmailSearchResult(
            success=True,
            query_used=request.query,
            matches=matches[: request.limit],
            summary=(
                "找到1封酒店订单调整邮件。"
                if matches
                else "未找到匹配邮件。"
            ),
            provider=self.provider,
            output_ref="eval://email/search/booking-adjustment",
        )

    def read(self, request: EmailReadRequest) -> EmailReadResult:
        if request.message_ids != [EMAIL_MESSAGE_ID]:
            return EmailReadResult(
                success=False,
                message_ids=request.message_ids,
                summary="指定邮件不存在于受控邮箱结果中。",
                provider=self.provider,
                output_ref="eval://email/read/not-found",
                errors=[
                    EmailProviderError(
                        code="message_not_found",
                        message="指定邮件不存在于受控邮箱结果中。",
                    )
                ],
            )
        return EmailReadResult(
            success=True,
            message_ids=request.message_ids,
            content=EMAIL_CONTENT[: request.max_total_chars],
            original_chars=len(EMAIL_CONTENT),
            truncated=len(EMAIL_CONTENT) > request.max_total_chars,
            summary="读取到酒店订单调整和退款信息。",
            provider=self.provider,
            output_ref="eval://email/read/booking-adjustment",
        )


class EmailFileBookingAmountEnvironment:
    """Read-only invoice plus controlled booking adjustment email."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._tempdir = TemporaryDirectory(
            prefix="agent-eval-email-file-reconciliation-"
        )
        self._root = Path(self._tempdir.name)
        (self._root / INVOICE_PATH).write_text(
            INVOICE_CONTENT,
            encoding="utf-8",
        )
        self._email_backend = _BookingAdjustmentEmailBackend()

    def describe(self) -> dict[str, Any]:
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": "controlled:booking-email-and-invoice",
            "tool_catalog": "default_complete_registry_without_local_web_access",
            "registered_tool_count": len(self._build_registry().list()),
            "writes": False,
            "state_reset": "per_task_run",
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        expectations = self.tool_outcome_expectations()
        search_result = self._email_backend.search(
            EmailSearchRequest(query='subject:"酒店订单调整"')
        )
        read_result = self._email_backend.read(
            EmailReadRequest(message_ids=[EMAIL_MESSAGE_ID])
        )
        return environment_validation(
            {
                "full_tool_registry": rule_assertion(
                    registry.sealed
                    and {"email_search", "email_read", "file_read"}
                    <= set(registry.list())
                    and {"web_search", "web_fetch"}.isdisjoint(
                        registry.list()
                    ),
                    (
                        f"sealed={registry.sealed}, "
                        f"registered_tools={registry.list()}"
                    ),
                    label="默认完整工具注册表已装配",
                ),
                "outcome_contract_matches_registry": rule_assertion(
                    {item.tool_name for item in expectations}
                    == set(registry.list()),
                    f"expectation_count={len(expectations)}",
                    label="工具结果预期覆盖注册表",
                ),
                "controlled_invoice_fixture": rule_assertion(
                    (self._root / INVOICE_PATH).read_text(encoding="utf-8")
                    == INVOICE_CONTENT,
                    f"invoice_path={INVOICE_PATH}",
                    label="受控酒店发票完整且未变",
                ),
                "controlled_email_fixture": rule_assertion(
                    search_result.success
                    and [item.message_id for item in search_result.matches]
                    == [EMAIL_MESSAGE_ID]
                    and read_result.success
                    and read_result.content == EMAIL_CONTENT,
                    (
                        f"message_ids={read_result.message_ids}, "
                        f"content_chars={len(read_result.content)}"
                    ),
                    label="受控订单调整邮件完整且未变",
                ),
                "isolated_state_boundary": rule_assertion(
                    self._root.is_dir(),
                    f"state_root={self._root.name}, writes=False",
                    label="任务状态按运行隔离",
                ),
            }
        )

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]:
        registry = self._build_registry()
        required_names = ("email_search", "email_read", "file_read")
        if available_tools is not None:
            subset = ToolRegistry()
            selected_names = list(
                dict.fromkeys([*available_tools, *required_names])
            )
            for name in selected_names:
                subset.register(
                    registry.get(name),
                    registry.registration_record(name),
                )
            subset.seal()
            registry = subset
        return outcome_expectations(
            registry,
            required_successes=required_names,
        )

    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest | dict[str, Any],
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution:
        self.validate().require_valid()
        return execute_isolated_runtime(
            task=task,
            request=UserRequest.model_validate(request),
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            config=self.config,
            registry=self._build_registry(),
            chat_adapter=self.chat_adapter,
            initial_state={},
        )

    def _build_registry(self) -> ToolRegistry:
        return build_controlled_registry(
            replacements={
                "email_search": EmailSearchTool(self._email_backend),
                "email_read": EmailReadTool(self._email_backend),
                "file_read": LocalFileReadTool(root=self._root),
            }
        )
