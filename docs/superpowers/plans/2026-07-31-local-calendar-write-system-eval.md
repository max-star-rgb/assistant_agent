# 本地日历写入 Tool System Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增一个不启动 LLM 或 `AgentGraphRuntime`、但通过完整工具治理链验证真实本地 SQLite 日历写入与幂等回放的 system eval。

**Architecture:** `evals/system/tools/calendar_write.py` 负责隔离数据库、Tool Registry、validator/executor 调用、终态断言和 artifact；`scripts/run_system_calendar_write_eval.py` 只负责 CLI 参数、显式副作用授权、输出和退出码。runner 使用 run-scoped `LocalSQLiteCalendarAdapter`，不依赖当前已删除的通用 tool eval runner/cases。

**Tech Stack:** Python 3.12、Pydantic、SQLite、`ActionValidator`、`ToolExecutor`、`ToolRegistry`、现有 system eval artifact helper。

## Global Constraints

- 不调用 LLM，不构造或运行 `AgentGraphRuntime`，不经过 assistant loop。
- 每次显式 Tool 调用必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> CalendarCreateTool`。
- 使用真实 SQLite，但只写 `.data/evals/system/tools/calendar/<run>/calendar.sqlite3`。
- 不读取 `.env`，不要求 real Provider mode，不访问网络，不装配默认 Tool Registry。
- 未提供 `--allow-local-calendar-write` 时必须在创建数据库前失败。
- 不恢复或修改当前工作区已删除的 `evals/system/tools/runner.py` 和 `cases.json`。
- 不新增 pytest，不修改 `tests/core`；验证入口本身承担真实 system eval 验收。
- 不提交设计规格或实施计划，最终统一判断是否提交本任务源码与文档。

---

### Task 1: 独立日历写入 system eval runner 与 CLI

**Files:**
- Create: `evals/system/tools/calendar_write.py`
- Create: `scripts/run_system_calendar_write_eval.py`

**Interfaces:**
- Consumes: `LocalSQLiteCalendarAdapter`、`CalendarCreateTool`、`ToolRegistry`、`ActionValidator.validate()`、`ToolExecutor.run_tool()`、`create_run_dir()`、`write_json()`。
- Produces: `CalendarWriteEvalInput`、`CalendarWriteEvalArtifact`、`CalendarWriteEvalResult`、`CalendarWriteEvalAuthorizationError`、`run_local_calendar_write_eval(...) -> CalendarWriteEvalResult`、CLI `main(argv: Sequence[str] | None = None) -> int`。

- [ ] **Step 1: 运行缺失入口的 RED 验收**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_write_eval.py --dry-run
```

Expected: Python 以非零退出，提示脚本不存在；失败原因必须是新入口尚未实现。

- [ ] **Step 2: 实现 runner 的输入、结果和授权契约**

在 `calendar_write.py` 中定义：

```python
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".data" / "evals" / "system" / "tools" / "calendar"

class CalendarWriteEvalAuthorizationError(ValueError): ...

class CalendarWriteEvalInput(BaseModel):
    title: str
    start_time: str
    end_time: str | None = None
    timezone: str | None = "Asia/Shanghai"
    location: str | None = "system-eval"
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = "synthetic system eval event"

class CalendarWriteEvalArtifact(BaseModel):
    run_dir: Path
    database_path: Path
    summary_path: Path
    result_path: Path

class CalendarWriteEvalResult(BaseModel):
    schema_version: Literal["local_calendar_write_system_eval_v1"]
    passed: bool
    checks: dict[str, bool]
    failures: list[str]
    run_id: str
    event_id: str | None
    validation_codes: list[str]
    tool_call_statuses: list[str]
    artifact: CalendarWriteEvalArtifact
```

授权检查必须在 `create_run_dir()` 之前执行：

```python
if not allow_local_calendar_write:
    raise CalendarWriteEvalAuthorizationError(
        "Local calendar write eval requires --allow-local-calendar-write."
    )
```

- [ ] **Step 3: 实现完整治理链与真实 SQLite 断言**

`run_local_calendar_write_eval()`：

1. 创建独立 run 目录和 SQLite adapter；
2. 只注册并 seal `CalendarCreateTool(adapter)`；
3. 创建 foreground request/state；
4. 用不含 `idempotency_key` 的 `AssistantToolCall` 调用 validator；
5. 用 `runtime_input={"idempotency_key": idempotency_key}` 调用 executor；
6. 对相同输入和 key 重复完整流程；
7. 比较 before/after snapshot、diff、两次 ToolResult 和两条 state call record；
8. 写 `summary.json`、`result.json`，其中 `result.json` 排除 artifact 对象后再补充 snapshot、diff 和两次结构化结果。

检查键使用稳定名称：

```python
{
    "only_calendar_create_registered": ...,
    "calendar_create_is_write": ...,
    "validations_accepted": ...,
    "two_governed_tool_calls": ...,
    "tool_calls_succeeded": ...,
    "provider_is_local_sqlite": ...,
    "first_call_committed": ...,
    "second_call_idempotent_replay": ...,
    "same_non_empty_event_id": ...,
    "single_persisted_event": ...,
    "persisted_event_matches_input": ...,
    "persisted_idempotency_key_matches": ...,
    "diff_has_one_addition_only": ...,
    "no_duplicate_groups": ...,
}
```

- [ ] **Step 4: 实现薄 CLI**

CLI 支持：

```text
--dry-run
--allow-local-calendar-write
--output-root
--title
--start-time
--end-time
--timezone
--location
--notes
```

`--dry-run` 输出 `dry_run=true`、事件参数和 output root，不创建目录。未授权返回 `2`；eval 检查失败返回 `1`；通过返回 `0`。

- [ ] **Step 5: 运行 dry-run GREEN 验收**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_write_eval.py --dry-run
```

Expected: exit `0`，JSON 中 `dry_run=true`，且 `.data/evals/system/tools/calendar` 下没有因本命令新增的 run 目录。

- [ ] **Step 6: 验证未授权 fail-closed**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_write_eval.py
```

Expected: exit `2`，JSON error 为 `local_calendar_write_eval_not_authorized`，且不创建 run 目录。

- [ ] **Step 7: 运行真实本地 SQLite GREEN 验收**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_write_eval.py \
  --allow-local-calendar-write
```

Expected: exit `0`，`passed=true`，所有 checks 为 `true`，artifact 中存在 `calendar.sqlite3`、`summary.json` 和 `result.json`。

### Task 2: 文档同步与最终验证

**Files:**
- Modify: `evals/README.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: Task 1 的稳定 CLI 参数和 artifact 路径。
- Produces: system eval 边界、命令、副作用门禁和验证范围的权威说明。

- [ ] **Step 1: 更新 eval 权威说明**

在 `evals/README.md` 的 Tool 连通性章节加入本地日历写入命令，并明确：

- 不经过 LLM/`AgentGraphRuntime`；
- 仍经过工具治理链；
- 使用隔离的真实 SQLite；
- 不要求 real Provider mode；
- 必须显式提供 `--allow-local-calendar-write`。

- [ ] **Step 2: 更新脚本入口索引**

在 `scripts/README.md` 的 Eval and evidence 列表加入
`scripts/run_system_calendar_write_eval.py`，说明 dry-run、真实写入授权和 artifact 路径。

- [ ] **Step 3: 运行语法与格式检查**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m py_compile \
  evals/system/tools/calendar_write.py \
  scripts/run_system_calendar_write_eval.py

git diff --check -- \
  evals/system/tools/calendar_write.py \
  scripts/run_system_calendar_write_eval.py \
  evals/README.md \
  scripts/README.md
```

Expected: 两条命令均 exit `0`。

- [ ] **Step 4: 重新运行完整最小验收**

依次运行 Task 1 的 dry-run、未授权和显式写入三个命令，并检查各自退出码分别为 `0`、`2`、`0`。

- [ ] **Step 5: 核对 scope 与工作区**

Run:

```bash
git status --short
git diff --stat
```

Expected: 本任务只新增 runner/CLI/设计与计划，并修改两份 eval 索引文档；原有用户改动和已删除的 `runner.py/cases.json` 保持不变。
