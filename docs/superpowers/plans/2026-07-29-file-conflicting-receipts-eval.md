# 票据冲突基础评测实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `file_conflicting_receipts_resolution` 基础 Agent Task，验证 Agent 能读取三份受控材料，识别重复票据、付款金额差异和缺失凭证。

**Architecture:** Task 作为 `evals/agent/tasks/` 下的自包含案例，使用活动 `AgentGraphRuntime` 和默认完整离线工具目录，仅将 `file_read` 替换为指向临时隔离目录的 `LocalFileReadTool`。Environment 负责冻结三份材料和工具结果预期；Task-local Grader 只定义 `response_quality` rubric；通用评分继续产生四项独立 Score。

**Tech Stack:** Python 3.11、Pydantic、AgentGraphRuntime、LocalFileReadTool、pytest、Langfuse Agent eval contracts。

## Global Constraints

- Environment 未经用户批准前不得执行以下实现步骤。
- 默认使用 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- pytest 必须设置 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider、MCP 或 Memory。
- 不修改 `evals/agent/batch_cases.py` 等并行进程正在编辑的共享文件。
- 不为尚未存在的 `document_workflow` 或浏览器工具伪造 Environment。
- Task 只验证 `conflicting_document_evidence_resolution`，不扩展为完整报销 Mission。
- Git 中的 Task、Environment、Grader 和 calibration 是回归定义权威。
- 客观工具终态由 Environment oracle 判断；开放语义由固定三个 Judge criterion 判断。
- 本轮不提交、不 push、不修改 `AGENTS.md`。

---

### Task 1: 用失败测试锁定自包含 Task 契约

**Files:**
- Create: `tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py`
- Create later: `evals/agent/tasks/file_conflicting_receipts_resolution/task.json`
- Create later: `evals/agent/tasks/file_conflicting_receipts_resolution/__init__.py`

**Interfaces:**
- Consumes: `evals.agent.loader.load_task(task_id: str) -> TaskSpec`
- Produces: 可由现有 loader 发现的 `file_conflicting_receipts_resolution`

- [ ] **Step 1: 写 loader 失败测试**

```python
from evals.agent.loader import load_task


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
    assert set(task.tags) == {"readonly", "file", "multi-document", "conflict"}
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py
```

Expected: FAIL，错误包含 `Unknown Agent eval task: file_conflicting_receipts_resolution`。

- [ ] **Step 3: 添加最小 Task 定义**

创建空的 `__init__.py`，并创建：

```json
{
  "id": "file_conflicting_receipts_resolution",
  "description": "Agent 应核对多份票据材料，识别重复凭证、金额差异和仍需补充的证据。",
  "capability": "conflicting_document_evidence_resolution",
  "request": {
    "user_id": "eval-receipt-conflict-user",
    "session_id": "eval-receipt-conflict-session",
    "text": "请核对 invoice-original.txt、invoice-copy.txt 和 payment-record.txt，告诉我目前有凭证支持的可报销金额、哪些材料重复、还有什么金额差异或缺口。不确定的地方不要猜。"
  },
  "environment": "evals.agent.tasks.file_conflicting_receipts_resolution.environment:FileConflictingReceiptsEnvironment",
  "grader": "evals.agent.tasks.file_conflicting_receipts_resolution.grader:grade",
  "tags": ["readonly", "file", "multi-document", "conflict"]
}
```

- [ ] **Step 4: 运行测试并确认失败推进到 Environment 缺失**

Run 同 Step 2。

Expected: loader 断言通过；若测试加载 entrypoint，则下一失败应指向尚未创建的 `environment.py`，不能是 Task JSON schema 错误。

### Task 2: 用失败测试定义受控 Environment

**Files:**
- Modify: `tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py`
- Create: `evals/agent/tasks/file_conflicting_receipts_resolution/environment.py`

**Interfaces:**
- Consumes:
  - `build_controlled_registry(replacements: Mapping[str, Tool]) -> ToolRegistry`
  - `outcome_expectations(registry, required_successes=("file_read",))`
  - `execute_isolated_runtime(...) -> TaskExecution`
- Produces:
  - `FileConflictingReceiptsEnvironment.describe()`
  - `validate() -> EnvironmentValidation`
  - `tool_outcome_expectations(...) -> list[ToolOutcomeExpectation]`
  - `execute(...) -> TaskExecution`

- [ ] **Step 1: 写 Environment 失败测试**

```python
from evals.agent.loader import load_entrypoint, load_task


def test_receipt_conflict_environment_is_readonly_isolated_and_complete() -> None:
    task = load_task("file_conflicting_receipts_resolution")
    environment = load_entrypoint(task.environment)()

    validation = environment.validate()
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
    assert len(expectations) == 15
    assert {"web_search", "web_fetch"}.isdisjoint(expectations)
    assert expectations["file_read"].required is True
    assert expectations["file_read"].expected_result == "success"
    assert environment.describe()["writes"] is False
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py
```

Expected: FAIL，错误指向 Environment entrypoint 缺失。

- [ ] **Step 3: 实现最小 Environment**

Environment 使用 `TemporaryDirectory(prefix="agent-eval-receipt-conflict-")`，写入以下三个冻结文本：

```python
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
```

`validate()` 必须客观检查：

- registry 已 sealed、工具数量为 15 且不含本地 web 工具；
- outcome expectations 完整覆盖 registry；
- 三个文件存在且内容与 `RECEIPT_FILES` 完全一致；
- 临时根目录存在且 Environment 为只读任务。

`tool_outcome_expectations(available_tools=None)` 使用默认完整 registry；传入子集时，保留 Evidence 可见工具并强制加入 `file_read`，然后声明 `file_read` 必须成功、其他工具可选成功。

`execute()` 使用 `execute_isolated_runtime()`，并保持 `initial_state={}`。正确事实只存在于冻结文件；
不得把预期金额或结论放入 `RunEvidence.initial_state`，避免 Judge 看到工具 Evidence 之外的 oracle。

- [ ] **Step 4: 运行测试并确认 GREEN**

Run 同 Step 2。

Expected: Environment 契约测试 PASS。

### Task 3: 通过活动 Runtime 证明三份材料可被读取

**Files:**
- Modify: `tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py`

**Interfaces:**
- Consumes: `FileConflictingReceiptsEnvironment.execute(...)`
- Produces: 包含三个成功 `file_read` 生命周期的 `RunEvidence`

- [ ] **Step 1: 写 scripted runtime 失败测试**

测试内实现 `_ReceiptConflictChat`。第一次 Chat 返回三个 `NativeToolCall`：

```python
[
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
]
```

第二次 Chat 返回：

```text
目前发票凭证支持860元。invoice-copy.txt 与原件的发票号码、航班和金额相同，是重复副本，不能重复计入。支付记录为920元，比发票多60元；材料只说明包含服务费，没有单独服务费凭证或明细，因此这60元暂不能确认，需要补充服务费发票或费用明细。
```

断言：

```python
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
assert execution.evidence.state_diff == {
    "added": [],
    "modified": [],
    "deleted": [],
}
```

- [ ] **Step 2: 运行测试并确认 RED**

Expected: FAIL，直到 Environment 的执行 wiring 完整。

- [ ] **Step 3: 补齐最小 execute wiring**

不得把工具选择、调用顺序或最终回答移入 Environment；Environment 只把 scripted adapter 注入活动 Runtime。

- [ ] **Step 4: 运行测试并确认 GREEN**

Expected: runtime 测试 PASS，且不访问网络。

### Task 4: 添加 Task-local Grader 和校准样本

**Files:**
- Create: `evals/agent/tasks/file_conflicting_receipts_resolution/grader.py`
- Create: `evals/agent/tasks/file_conflicting_receipts_resolution/calibration.json`
- Modify: `tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py`

**Interfaces:**
- Consumes: `grade_case(evidence, judge, response_quality_rubric=...)`
- Produces: 固定三个 Judge dimension 的 `TaskJudgeResult`

- [ ] **Step 1: 写 Grader 与 calibration 失败测试**

```python
from evals.agent.calibration import (
    load_labeled_calibration_judge,
    run_calibration,
)


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
```

- [ ] **Step 2: 运行测试并确认 RED**

Expected: FAIL，错误指向 Grader 或 calibration 文件缺失。

- [ ] **Step 3: 实现 Grader**

`RESPONSE_QUALITY_RUBRIC` 明确以下通过条件：

1. 说明当前有发票凭证支持的金额为 860 元；
2. 识别两份发票号码相同，是重复材料且不能重复计入；
3. 指出支付总额 920 元与发票相差 60 元；
4. 说明 60 元只有“服务费”备注，仍需服务费发票或费用明细；
5. 清晰区分已证实事实与待补证信息，不猜测最终报销资格。

事实是否忠于文件 Evidence 只由 `grounding` 判断。

- [ ] **Step 4: 添加三个 Calibration v3 fixture**

- `resolves_duplicate_and_gap`：三份文件均读取，四项 Score 均为 `true`；
- `double_counts_duplicate_invoice`：三份文件均读取，但回答把两份同号发票合计为 1720 元；
  `tool_execution=true`、`tool_semantics=true`、`grounding=false`、`response_quality=false`；
- `omits_payment_gap`：只回答 860 元和重复关系，未处理支付记录的 60 元差异；
  `tool_execution=true`、`tool_semantics=true`、`grounding=true`、`response_quality=false`。

每个 fixture 的 `available_tools` 使用 `["file_read"]`，工具结果保存完整的结构化
`model_observation`，不得把 oracle 或 rubric 放入 Evidence。

- [ ] **Step 5: 运行测试并确认 GREEN**

Expected: 三个 calibration fixture 全部 `matched=true`。

### Task 5: 运行最小充分验证与离线 inspect

**Files:**
- Verify only; no production changes expected

**Interfaces:**
- Consumes: 新 Task 全部文件和现有 eval loader
- Produces: 可审计的离线验证证据

- [ ] **Step 1: 运行聚焦 pytest**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行 eval 故障域回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
tests/integration/eval
```

Expected: 新旧用例全部 PASS。

- [ ] **Step 3: 运行离线 inspect**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
scripts/run_agent_evals.py \
--inspect \
--task file_conflicting_receipts_resolution
```

Expected: exit 0，输出 Task ID、受控 Environment、完整默认工具目录和 `file_read` 必调预期；不读取 `.env`、不联网。

- [ ] **Step 4: 检查变更范围**

```bash
git status --short -- \
evals/agent/tasks/file_conflicting_receipts_resolution \
tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py
```

Expected: 只显示本 Task 与聚焦测试文件。不要提交并行进程的其他改动。

- [ ] **Step 5: 更新路线进度**

在用户确认案例设计与验证结果后，再单独决定是否更新
`docs/development/2026-07-29-complex-agent-mission-eval-roadmap.md` 的阶段进度。该文档受本地 exclude
保护，本步骤不得隐式提交。
