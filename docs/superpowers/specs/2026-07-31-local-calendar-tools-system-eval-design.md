# 本地日历 Tool System Eval 设计

## 目标与命名

正式 system eval 与实际被测 Tool 保持同名：

```text
calendar_create
calendar_search
```

不引入 `calendar_write` Tool、eval、schema、flag 或兼容别名。公共 Tool 协议仍为已有的
`calendar_create` / `calendar_search`。

两个 eval 均不调用 LLM、不构造 `AgentGraphRuntime`、不经过 assistant loop，只通过
`ActionValidator -> ToolExecutor -> ToolRegistry -> Tool` 验证真实本地 SQLite Tool 执行。

## 文件与入口

```text
evals/system/tools/calendar_create.py
evals/system/tools/calendar_search.py
scripts/run_system_calendar_create_eval.py
scripts/run_system_calendar_search_eval.py
```

两个 `evals/system/tools/*.py` 均支持 IDE 直接运行；`scripts/*.py` 是等价的稳定 CLI 薄入口。

创建命令：

```bash
python evals/system/tools/calendar_create.py
```

搜索命令：

```bash
python evals/system/tools/calendar_search.py
```

两者都支持 `--dry-run`。无参数时默认执行隔离的真实 SQLite eval，先输出 `status=running`，再输出
最终 JSON；`--dry-run` 只展示输入与目标路径，不创建 artifact 或数据库。

## 数据隔离

每次运行使用独立数据库：

```text
.data/evals/system/tools/calendar/create/<run>/calendar.sqlite3
.data/evals/system/tools/calendar/search/<run>/calendar.sqlite3
```

output root resolve 后必须位于对应 operation 根目录内，阻止 workspace、主目录、默认日历和 symlink
逃逸。artifact 保留 `calendar.sqlite3`、`summary.json`、`result.json`。

## calendar_create

Registry 只注册 `CalendarCreateTool`。runner 使用受信 `runtime_input` 注入 run-scoped
`idempotency_key`，两次完整经过 validator/executor：

1. 首次结果必须为 `committed`；
2. 第二次必须为 `idempotent_replay`；
3. 两次 event ID 相同；
4. ToolResult event ID 与 SQLite 落库 event ID 相同；
5. SQLite 最终只有一条字段完全匹配的事件，且无 duplicate group。

## calendar_search

搜索 eval 的 setup 使用 `LocalSQLiteCalendarAdapter.create()` 直接向本次隔离数据库预置一条合成
事件；该 setup 不计作被测 Tool call，也不宣称验证 `calendar_create`。

预置完成后：

1. 保存 before snapshot；
2. Registry 只注册 `CalendarSearchTool`；
3. 只执行一次 `calendar_search` 的 validator/executor 链；
4. 结果必须来自 `local_sqlite`，且只返回预置事件；
5. Tool call record 必须只有一个 `calendar_search`；
6. after snapshot 必须与 before 完全一致，证明搜索没有修改日历。

## 验证边界

这是具体 builtin Tool 的正式本地 system eval，不修改 core invariant、不新增 pytest、不调用真实
Provider 或网络。结果权威是两个显式 system eval 的结构化 checks、SQLite 终态和 artifact。
