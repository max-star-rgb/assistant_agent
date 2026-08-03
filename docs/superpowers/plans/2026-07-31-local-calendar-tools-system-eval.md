# 本地日历 Tool System Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有日历写入 eval 收敛为实际 Tool 名 `calendar_create`，并新增并列的真实 SQLite `calendar_search` eval。

**Architecture:** create 与 search 使用独立 runner、CLI、artifact 根目录和 operator flag。create 通过治理链执行两次并验证幂等落盘；search 先直接预置隔离数据，再只通过治理链执行一次只读 Tool 并验证结果和无状态变更。

**Tech Stack:** Python 3.12、Pydantic、SQLite、`ActionValidator`、`ToolExecutor`、`ToolRegistry`。

## Global Constraints

- 公共 Tool 名保持 `calendar_create` / `calendar_search`，不新增 `calendar_write`。
- 不调用 LLM、`AgentGraphRuntime`、assistant loop、真实 Provider 或网络。
- 每个被测 Tool 调用必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> Tool`。
- SQLite 与 artifact 只能写入 operation-scoped system eval 根目录。
- 不恢复当前已删除的通用 `evals/system/tools/runner.py` 和 `cases.json`。
- Core invariant unchanged；不新增 pytest。

---

### Task 1: calendar_create 命名收敛

**Files:**
- Rename: `evals/system/tools/calendar_write.py` -> `evals/system/tools/calendar_create.py`
- Rename: `scripts/run_system_calendar_write_eval.py` -> `scripts/run_system_calendar_create_eval.py`

**Interfaces:**
- Produces: `CalendarCreateEvalInput`、`CalendarCreateEvalResult`、
  `run_local_calendar_create_eval()`、`validate_calendar_create_output_root()`。

- [ ] 运行 `python evals/system/tools/calendar_create.py --dry-run`，确认因新入口不存在而 RED。
- [ ] 重命名文件，并将所有 write eval class/function/schema/check/artifact 名收敛为
  `calendar_create` / `calendar/create`。
- [ ] 运行 dry-run 和无参数真实 SQLite 命令，预期退出码均为 0，真实运行所有 checks 为 true。

### Task 2: calendar_search 真实本地 Tool eval

**Files:**
- Create: `evals/system/tools/calendar_search.py`
- Create: `scripts/run_system_calendar_search_eval.py`

**Interfaces:**
- Produces: `CalendarSearchEvalInput`、`CalendarSearchEvalResult`、
  `run_local_calendar_search_eval()`、`validate_calendar_search_output_root()`。

- [ ] 运行 `python evals/system/tools/calendar_search.py --dry-run`，确认因入口不存在而 RED。
- [ ] 创建 run-scoped SQLite，以 adapter 直接预置唯一合成事件并保存 before snapshot。
- [ ] Registry 只注册 `CalendarSearchTool`，构造一次 `AssistantToolCall(tool_name="calendar_search")`，
  通过 validator/executor 执行。
- [ ] 断言唯一 tool call、local_sqlite provider、唯一匹配事件，以及 before/after snapshot 完全相同。
- [ ] CLI 支持 IDE 无参数直跑、`--dry-run`、立即 progress 和结构化退出码。
- [ ] 运行 dry-run 和无参数真实 SQLite 命令，预期退出码均为 0，真实运行所有 checks 为 true。

### Task 3: 文档与最终验证

**Files:**
- Modify: `evals/README.md`
- Modify: `scripts/README.md`

- [ ] 删除 `calendar_write` 入口说明，加入并列的 `calendar_create` / `calendar_search` IDE 与 CLI
  命令、flag、artifact 和 setup 边界。
- [ ] 运行 `rg -n "calendar_write" evals/system/tools scripts/run_system_calendar* evals/README.md scripts/README.md`，
  预期无结果。
- [ ] 对四个 Python 文件运行 `py_compile` 与 Ruff，对变更文档运行 `git diff --check`。
- [ ] 重新运行两个授权 system eval，确认 exit 0、`passed=true`、`failures=[]`。
