"""Controlled conflicting-receipts Environment."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from assistant_agent.tools.plugins.builtin.local_file_access.tool import (
    LocalFileReadTool,
)
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import AssertionResult
from evals.agent.environment_base import ControlledTaskEnvironment
from evals.agent.grading import rule_assertion
from evals.agent.task_support import build_controlled_registry


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


class FileConflictingReceiptsEnvironment(ControlledTaskEnvironment):
    """Read-only file evidence with one duplicate and one amount gap."""

    dependency_label = "controlled:conflicting-receipt-files"

    def setup(self) -> None:
        self._tempdir = TemporaryDirectory(prefix="agent-eval-receipt-conflict-")
        self._root = Path(self._tempdir.name)
        for name, content in RECEIPT_FILES.items():
            (self._root / name).write_text(content, encoding="utf-8")

    def required_successes(self) -> tuple[str, ...]:
        return ("file_read",)

    def task_validation_checks(
        self,
        registry: ToolRegistry,
    ) -> dict[str, AssertionResult]:
        del registry
        files_match = set(path.name for path in self._root.iterdir()) == set(
            RECEIPT_FILES
        ) and all(
            (self._root / name).read_text(encoding="utf-8") == content
            for name, content in RECEIPT_FILES.items()
        )
        return {
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

    def build_registry(self) -> ToolRegistry:
        return build_controlled_registry(
            replacements={"file_read": LocalFileReadTool(root=self._root)}
        )
