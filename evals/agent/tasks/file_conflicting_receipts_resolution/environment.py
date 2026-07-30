"""Controlled conflicting-receipts Environment."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.requests import UserRequest
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


RECEIPT_FILES = {
    "invoice-original.txt": (
        "电子发票\n"
        "发票号码：INV-2026-0718\n"
        "乘机人：王晨\n"
        "航班：CZ3102\n"
        "开票日期：2026-07-18\n"
        "金额：860.00元\n"
    ),
    "invoice-copy.txt": (
        "电子发票下载副本\n"
        "发票号码：INV-2026-0718\n"
        "乘机人：王晨\n"
        "航班：CZ3102\n"
        "开票日期：2026-07-18\n"
        "金额：860.00元\n"
    ),
    "payment-record.txt": (
        "支付记录\n"
        "订单：CZ3102-20260718\n"
        "支付日期：2026-07-18\n"
        "支付总额：920.00元\n"
        "备注：机票及服务费\n"
    ),
}

class FileConflictingReceiptsEnvironment:
    """Read-only file evidence with one duplicate and one amount gap."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._tempdir = TemporaryDirectory(
            prefix="agent-eval-receipt-conflict-"
        )
        self._root = Path(self._tempdir.name)
        for name, content in RECEIPT_FILES.items():
            (self._root / name).write_text(content, encoding="utf-8")

    def describe(self) -> dict[str, Any]:
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": "controlled:conflicting-receipt-files",
            "tool_catalog": "default_complete_registry_without_local_web_access",
            "registered_tool_count": len(self._build_registry().list()),
            "writes": False,
            "state_reset": "per_task_run",
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        expectations = self.tool_outcome_expectations()
        files_match = (
            set(path.name for path in self._root.iterdir())
            == set(RECEIPT_FILES)
            and all(
                (self._root / name).read_text(encoding="utf-8") == content
                for name, content in RECEIPT_FILES.items()
            )
        )
        return environment_validation(
            {
                "full_tool_registry": rule_assertion(
                    registry.sealed
                    and "file_read" in registry.list()
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
                "controlled_receipt_fixture": rule_assertion(
                    files_match,
                    f"fixture_files={sorted(path.name for path in self._root.iterdir())}",
                    label="受控票据材料完整且未变",
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
        if available_tools is not None:
            subset = ToolRegistry()
            selected_names = list(
                dict.fromkeys([*available_tools, "file_read"])
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
            required_successes=("file_read",),
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
            replacements={"file_read": LocalFileReadTool(root=self._root)}
        )
