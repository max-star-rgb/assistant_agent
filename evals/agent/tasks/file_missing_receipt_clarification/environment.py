"""Controlled missing-receipt Environment."""

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
    "expense-summary.txt": (
        "报销费用汇总\n"
        "出租车：135.00元\n"
        "酒店：680.00元\n"
        "合计：815.00元\n"
        "说明：本汇总由员工自行整理，不是发票或付款凭证。\n"
    ),
    "taxi-receipt.txt": (
        "出租车电子行程单\n"
        "凭证号：TAXI-2026-0720-135\n"
        "乘车日期：2026-07-20\n"
        "乘车人：王晨\n"
        "金额：135.00元\n"
    ),
}


class FileMissingReceiptEnvironment(ControlledTaskEnvironment):
    """Read-only evidence with one supported expense and one missing receipt."""

    dependency_label = "controlled:missing-receipt-files"

    def setup(self) -> None:
        self._tempdir = TemporaryDirectory(prefix="agent-eval-missing-receipt-")
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
        files_match = {path.name for path in self._root.iterdir()} == set(
            RECEIPT_FILES
        ) and all(
            (self._root / name).read_text(encoding="utf-8") == content
            for name, content in RECEIPT_FILES.items()
        )
        return {
            "controlled_receipt_fixture": rule_assertion(
                files_match,
                f"fixture_files={sorted(path.name for path in self._root.iterdir())}",
                label="受控报销材料完整且未变",
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
