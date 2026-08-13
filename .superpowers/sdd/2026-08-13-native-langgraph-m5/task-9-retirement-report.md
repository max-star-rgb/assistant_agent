# Task 9 legacy retirement machine gate 报告

## 状态

- 实现完成并验证；retirement gate 产品接口可用。
- 真实 operator gate 当前为 `ready=false`，不得删除 legacy worker/runtime/execution、`LegacyDrainHost` 或 `run_work_item`。
- 本任务未创建或修改 operator manifest，未写入真实 Workflow SQLite 主库。

## 完成内容

- 新增严格、冻结的 `WorkflowRetirementStatus`，机器复算：
  - legacy 非终态数量；
  - 以探针 `now` 判断尚未过期的 active lease 数量；
  - `waiting_input|blocked` 数量；
  - manifest phase、revision、digest 与 rollback window；
  - 持久 retirement audit 是否与 manifest revision、digest、operator approval ref 完全一致。
- `ready=false` 使用固定、有界 reason code；状态不包含 workflow ID、owner 或用户正文，也不接受 caller boolean 代替 audit。
- 新增 content-free `WorkflowRetirementAudit` 业务事实及 memory/SQLite 持久化：仅在 audit 之外的全部前置成立时写入；同一 manifest 幂等，不同 revision/digest/approval ref 冲突。
- 同一 manifest 的并发 caller 由 Store 原子返回唯一持久 audit；竞争 loser 不返回自己未落库的时间戳。
- SQLite 新增显式 `open_read_only()`；只读探针不建目录、不初始化 schema、不切换 journal，mutation fail closed。旧库没有 audit 表时返回 audit missing。
- 同步当前 workflow owner authority，明确未 ready 不得移除 legacy execution path。

## TDD 与验证

首个 RED：

```text
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/tdd/native-langgraph-m5/test_workflow_cutover_manifest.py -k retirement
2 failed, 13 deselected
```

失败原因为 `WorkflowCutoverController.retirement_status` 尚不存在。approval-ref 精确绑定也独立观察到 RED：memory/SQLite 均错误返回 `ready=true`，修复后三元绑定 fail closed。

最终验证：

```text
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/tdd/native-langgraph-m5/test_workflow_cutover_manifest.py
24 passed

python -m ruff check <本任务 Python 文件>
All checks passed!

python -m ruff format --check <本任务 Python 文件>
5 files already formatted

PYTHONPATH=src python -m py_compile <本任务 Python 文件>
exit 0

python scripts/check_documentation_authority.py --repo-root .
valid=true, errors=[]

git diff --check
exit 0
```

Core invariant: unchanged。

Tests: 更新 `tests/tdd/native-langgraph-m5` 临时 RED/GREEN；用户可手动删除该 feature 目录，本任务未将其晋升为 core。

## 真实 gate（只读）

探针路径：`/home/lenovo1/pycharm_project/assistant_agent/.local/workflows/workflows.sqlite3`，通过 `SQLiteWorkflowStore.open_read_only()` 打开；探针前后 size 与 mtime_ns 一致。

```json
{
  "ready": false,
  "operator_manifest_available": false,
  "nonterminal_legacy_count": 2,
  "active_legacy_lease_count": 0,
  "waiting_legacy_count": 1,
  "legacy_status_counts": {
    "completed": 6,
    "failed": 2,
    "running": 1,
    "waiting_input": 1
  },
  "persisted_retirement_audit_count": 0,
  "database_metadata_unchanged": true
}
```

## Concerns / 后续门槛

- 当前仍有 `running=1` 与 `waiting_input=1`，非终态没有归零。
- 当前没有可用的 operator manifest，且真实库尚无 retirement audit；不得伪造 `retired` phase 或 audit。
- operator 完成 drain、关闭 rollback window、提供 immutable retired manifest 并经产品接口持久化 audit 后，必须重新运行同一只读 machine gate；只有 `ready=true` 才能另起变更删除 legacy execution path。
