"""Controlled missing-receipt Environment."""

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


class FileMissingReceiptEnvironment:
    """Read-only evidence with one supported expense and one missing receipt."""

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._tempdir = TemporaryDirectory(
            prefix="agent-eval-missing-receipt-"
        )
        self._root = Path(self._tempdir.name)
        for name, content in RECEIPT_FILES.items():
            (self._root / name).write_text(content, encoding="utf-8")

    def describe(self) -> dict[str, Any]:
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": "controlled:missing-receipt-files",
            "tool_catalog": "default_complete_registry_without_local_web_access",
            "registered_tool_count": len(self._build_registry().list()),
            "writes": False,
            "state_reset": "per_task_run",
        }

    def validate(self) -> EnvironmentValidation:
        registry = self._build_registry()
        expectations = self.tool_outcome_expectations()
        files_match = (
            {path.name for path in self._root.iterdir()} == set(RECEIPT_FILES)
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
                    label="受控报销材料完整且未变",
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
